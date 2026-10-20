"""Which app is in front, observed through the DVT instruments service (needs the DDI).

Observation only: nothing here authorizes or refuses input (docs/plans/app-permissions.md).

``DeviceInfo.proclist()`` marks each process ``foregroundRunning``; on the iOS 27 bench it was
right for launches, icon taps, links into Safari and Home, but took 460 ms (p50) to 1.2 s for
~340 processes over Wi-Fi. The ``mobilenotifications`` channel pushes app state changes cheaply
but also reports background wakes as "Running", so a notification only invalidates the last
answer; it never stands in for one.

Every answer carries the invalidation sequence and session epoch it started under. It counts
only while both still match and it is younger than ``max_age`` measured from the query's start,
so a query that overlapped an action or a notification cannot vouch for the screen after it.
The DVT connection opens on the first request and closes after ``idle_seconds`` without one:
an idle box sends nothing.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from openlolo.domain.errors import OpenLoloError

logger = logging.getLogger(__name__)

# Always reported in the foreground on the bench (the Home screen and the keyboard host). They
# are ignored when deciding which app is in front; that is not a grant for what they show.
INFRASTRUCTURE = frozenset({"com.apple.springboard", "com.apple.InputUI"})
EVENT_LIMIT = 64


@dataclass(frozen=True)
class Snapshot:
    apps: tuple[str, ...]  # Foreground applications other than INFRASTRUCTURE, sorted.
    infrastructure: tuple[str, ...]
    unidentified: int  # Foreground applications that reported no bundle identifier.
    started: float  # Monotonic time the query was sent.
    finished: float
    sequence: int  # Invalidation sequence when the query was sent.
    epoch: str

    @property
    def context(self) -> str:
        """Context: "app" = exactly one identified app in front; "system" = none (Home screen or a system
        surface); "ambiguous" = several or unidentified ones, e.g. during a switch."""
        if self.unidentified or len(self.apps) > 1:
            return "ambiguous"
        return "app" if self.apps else "system"

    def as_dict(self, now: float) -> dict:
        return {
            "context": self.context,
            "apps": list(self.apps),
            "infrastructure": list(self.infrastructure),
            "unidentified": self.unidentified,
            "query_seconds": round(self.finished - self.started, 3),
            "age_seconds": round(now - self.started, 3),
        }


def classify(processes, started: float, finished: float, sequence: int, epoch: str) -> Snapshot:
    apps, infrastructure, unidentified = set(), set(), 0
    for process in processes or []:
        if not isinstance(process, dict) or not process.get("foregroundRunning"):
            continue
        if not process.get("isApplication"):
            continue
        bundle = process.get("bundleIdentifier")
        if not isinstance(bundle, str) or not bundle:
            unidentified += 1
        elif bundle in INFRASTRUCTURE:
            infrastructure.add(bundle)
        else:
            apps.add(bundle)
    return Snapshot(
        tuple(sorted(apps)), tuple(sorted(infrastructure)), unidentified, started, finished, sequence, epoch
    )


class DvtSource:
    """One DVT connection over the session's RSD: the process list plus app state events."""

    def __init__(self, rsd):
        self.rsd = rsd
        self.stack: contextlib.AsyncExitStack | None = None
        self.info = None
        self.notifications = None

    async def open(self):
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.notifications import Notifications

        stack = contextlib.AsyncExitStack()
        try:
            dvt = await stack.enter_async_context(DvtProvider(self.rsd))
            self.info = await stack.enter_async_context(DeviceInfo(dvt))
            self.notifications = await stack.enter_async_context(Notifications(dvt))
        except BaseException:
            await stack.aclose()
            raise
        self.stack = stack

    async def processes(self) -> list:
        assert self.info is not None
        return await self.info.proclist()

    async def events(self):
        assert self.notifications is not None
        async for event in self.notifications:
            yield event

    async def close(self):
        stack, self.stack = self.stack, None
        if stack is not None:
            await stack.aclose()


@dataclass
class Counters:
    queries: int = 0
    superseded: int = 0  # Answers dropped because an invalidation arrived while they ran.
    invalidations: dict = field(default_factory=dict)
    failures: int = 0
    opens: int = 0


class ForegroundObserver:
    def __init__(self, source_factory, config, clock=time.monotonic):
        self.source_factory = source_factory
        self.mode = config.foreground_observer
        self.max_age = config.foreground_max_age
        self.poll_seconds = config.foreground_poll_seconds
        self.idle_seconds = config.foreground_idle_seconds
        self.timeout = config.foreground_query_timeout
        self.clock = clock
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.last: Snapshot | None = None
        self.source = None
        self.demanded = 0.0
        self.events: deque = deque(maxlen=EVENT_LIMIT)
        self.counters = Counters()
        self.lock = asyncio.Lock()
        self.tasks: list[asyncio.Task] = []  # Listener, poller and idle timer of the open source.
        self.closed = False
        self.idle_task: asyncio.Task | None = None

    # ----- invalidation --------------------------------------------------------------

    def invalidate(self, reason: str) -> None:
        self.sequence += 1
        self.counters.invalidations[reason] = self.counters.invalidations.get(reason, 0) + 1

    def valid(self, snapshot: Snapshot | None) -> bool:
        return (
            snapshot is not None
            and snapshot.epoch == self.epoch
            and snapshot.sequence == self.sequence
            and self.clock() - snapshot.started <= self.max_age
        )

    # ----- queries -------------------------------------------------------------------

    async def snapshot(self, fresh: bool = False) -> dict:
        """The current answer, querying when the last one no longer counts. Waits for a query
        in flight rather than sending a second one. ``fresh`` (bench) drops the last answer."""
        if fresh:
            self.invalidate("fresh")
        if self.closed:
            raise OpenLoloError("FOREGROUND_UNAVAILABLE", "The foreground observer is closed")
        asked = self.clock()
        self.demanded = asked
        queried = False
        # A query that an invalidation overtook does not count; one retry covers a single event.
        for _ in range(2):
            if self.valid(self.last):
                break
            async with self.lock:
                if self.valid(self.last):
                    break
                await self._query_locked()
                queried = True
        self._schedule()
        now = self.clock()
        if not self.valid(self.last):
            raise OpenLoloError("FOREGROUND_UNVERIFIED", "The foreground app changed during every query")
        assert self.last is not None
        return {**self.last.as_dict(now), "waited_seconds": round(now - asked, 3), "queried": queried}

    async def _query_locked(self) -> None:
        if self.source is None:
            await self._open_locked()
        source = self.source
        assert source is not None
        sequence, started = self.sequence, self.clock()
        try:
            processes = await asyncio.wait_for(source.processes(), self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.counters.failures += 1
            await self._close_source()
            raise OpenLoloError(
                "FOREGROUND_UNAVAILABLE", "The phone did not report its foreground app"
            ) from exc
        self.counters.queries += 1
        snapshot = classify(processes, started, self.clock(), sequence, self.epoch)
        if sequence != self.sequence:
            self.counters.superseded += 1
        self.last = snapshot

    async def _open_locked(self) -> None:
        source = self.source_factory()
        try:
            await asyncio.wait_for(source.open(), self.timeout * 2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.counters.failures += 1
            with contextlib.suppress(Exception):
                await source.close()
            raise OpenLoloError(
                "FOREGROUND_UNAVAILABLE", "The DVT instruments service is unavailable"
            ) from exc
        self.counters.opens += 1
        self.source = source
        self._spawn(self._listen(source))
        if self.mode == "poll":
            self._spawn(self._poll())

    # ----- background tasks ----------------------------------------------------------

    def _spawn(self, coroutine) -> None:
        self.tasks = [task for task in self.tasks if not task.done()]
        self.tasks.append(asyncio.create_task(coroutine))

    def _schedule(self) -> None:
        if self.source is not None and (self.idle_task is None or self.idle_task.done()):
            self.idle_task = asyncio.create_task(self._idle())
            self.tasks.append(self.idle_task)

    async def _listen(self, source) -> None:
        try:
            async for event in source.events():
                if not (isinstance(event, tuple) and event and event[0] == "applicationStateNotification:"):
                    continue
                details = event[1][0] if len(event) > 1 and event[1] and isinstance(event[1][0], dict) else {}
                self.events.append(
                    (round(self.clock(), 3), details.get("appName"), details.get("state_description"))
                )
                self.invalidate("notification")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("foreground notifications stopped", exc_info=True)
            self.invalidate("observer_error")

    async def _poll(self) -> None:
        """Start-to-start polling while requests keep coming (``poll`` mode only)."""
        while self.source is not None and self.clock() - self.demanded < self.idle_seconds:
            started = self.clock()
            if not self.lock.locked():
                with contextlib.suppress(OpenLoloError):
                    async with self.lock:
                        await self._query_locked()
            await asyncio.sleep(max(0.0, self.poll_seconds - (self.clock() - started)))

    async def _idle(self) -> None:
        """Close the DVT connection once nothing asked for ``idle_seconds``."""
        while self.source is not None:
            remaining = self.idle_seconds - (self.clock() - self.demanded)
            if remaining <= 0:
                async with self.lock:
                    if self.clock() - self.demanded >= self.idle_seconds:
                        await self._close_source()
                return
            await asyncio.sleep(remaining)

    async def _close_source(self) -> None:
        source, self.source = self.source, None
        self.invalidate("closed")
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        self.tasks = []
        if source is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(source.close(), 2)

    async def close(self) -> None:
        self.closed = True
        await self._close_source()

    def status(self) -> dict:
        now = self.clock()
        return {
            "mode": self.mode,
            "open": self.source is not None,
            "last": self.last.as_dict(now) if self.last is not None else None,
            "valid": self.valid(self.last),
            "queries": self.counters.queries,
            "superseded": self.counters.superseded,
            "failures": self.counters.failures,
            "opens": self.counters.opens,
            "invalidations": dict(self.counters.invalidations),
            "events": [list(e) for e in self.events],
        }
