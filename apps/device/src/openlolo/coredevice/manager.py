"""CoreDeviceManager: one bound phone, one active transport, one supervised persistent session.

Higher layers call ``capture``/``begin``/``report``/``release_all``/``list_apps``/``device_info``
and never learn whether the phone is on USB or Wi-Fi. A supervisor task finds the phone,
establishes the tunnel and services, watches for loss, and reconnects.

Nothing polls for the phone in the background. usbmuxd pushes cable attach and detach events
over one Listen connection; an attach of the bound phone connects over USB at once. Wi-Fi is
searched only inside a bounded search window (``search_window_seconds``) opened by an event:
boot, the cable pulled, a Wi-Fi session lost, the top button (IPC ``search``), or a request that
needs the phone. Windows coalesce: while one is open every trigger joins it. When a window
closes without the phone the worker waits for the next event and does no work.

With the ``auto`` transport USB wins whenever the bound phone is on the cable. While on Wi-Fi
an attach of the bound phone waits for an idle moment (no input generation, no request holding
the I/O lock) and moves the session to USB. Requests that arrive during such a switch wait
``switch_hold_seconds`` for the session to come back instead of failing. Only the bound UDID is
ever considered on either path.
"""

import asyncio
import contextlib
import logging
import time

from openlolo.coredevice import hid
from openlolo.coredevice.hid import ReportGate
from openlolo.coredevice.session import DeviceSession
from openlolo.coredevice.state import OWNER_STATES, STATE_MESSAGES, Connection, State, classify
from openlolo.coredevice.transports import UsbTransport, WifiTransport, same_udid
from openlolo.coredevice.tunnel import Tunnel
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Binding

log = logging.getLogger("openlolo.coredevice")


class Remount(Exception):
    """The developer disk image was just mounted; the RSD service list must be refreshed."""


# States a request waits through (bounded by ``switch_hold_seconds``) before giving up.
HOLD_STATES = {
    State.CONNECTING.value,
    State.RECONNECTING.value,
    State.SWITCHING.value,
    State.MOUNTING_DDI.value,
}
# States in which the supervisor is busy with a session; a search window has nothing to find.
BUSY_STATES = {
    State.CONNECTING.value,
    State.CONNECTED.value,
    State.SWITCHING.value,
    State.MOUNTING_DDI.value,
}
OWNER_STATE_VALUES = {state.value for state in OWNER_STATES.values()}
# Consecutive transient failures retried with backoff before the supervisor waits for an event.
RETRY_LIMIT = 3
# Listen retry backoff while the presence-only fallback poll runs.
LISTEN_RETRY_MIN, LISTEN_RETRY_MAX = 2.0, 60.0


def _key(udid: str) -> str:
    return udid.replace("-", "").lower()


class CoreDeviceManager:
    def __init__(
        self,
        config,
        transports=None,
        tunnel_factory=Tunnel,
        session_factory=DeviceSession,
        clock=time.monotonic,
    ):
        self.config = config
        cache = config.state_dir / "device" / "lockdown"
        self.transports = transports or {
            "usb": UsbTransport(cache),
            "wifi": WifiTransport(config.discovery_seconds),
        }
        self.tunnel_factory, self.session_factory = tunnel_factory, session_factory
        self.clock = clock
        self.binding: Binding | None = None
        self.connection = Connection()
        self.gate = ReportGate()
        self.tunnel = None
        self.session = None
        self.task: asyncio.Task | None = None
        self.watchdog: asyncio.Task | None = None
        self.listener: asyncio.Task | None = None
        self.lost = asyncio.Event()
        self.wake = asyncio.Event()
        self.mount_requested = False
        self.io_lock = asyncio.Lock()
        # Auto mode: a failed USB attempt disarms the move back to the cable until the phone
        # has been seen off the cable once, so a broken USB path cannot flap the session.
        self.usb_armed = True
        # Cable state from usbmuxd Listen (or the fallback poll): serials on the cable, a counter
        # of changes, and a counter of attaches of the bound phone.
        self.cable: set[str] = set()
        self.usb_changes = 0
        self.attaches = 0
        self.listening = False
        # The current (or last) search window and counters for status and tests.
        self.window: asyncio.Task | None = None
        self.found: str | None = None  # Where the last window found the phone, until used.
        self.windows = 0
        self.browses = 0
        # Pulsed on every state, cable or window change; waiters take the current event.
        self._changed = asyncio.Event()

    # ----- lifecycle -----------------------------------------------------------------

    async def start(self):
        if self.watchdog is None:
            self.watchdog = asyncio.create_task(self._watchdog())
        if self.listener is None and "usb" in self.transports:
            self.listener = asyncio.create_task(self._usb_events())

    async def close(self):
        await self.disconnect()
        for name in ("watchdog", "listener"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def connect(self, binding: Binding) -> dict:
        if binding.transport != "auto" and binding.transport not in self.transports:
            raise OpenLoloError("UNSUPPORTED_TRANSPORT", status=400)
        await self._stop_supervisor()
        self.binding = binding
        self.connection = Connection(transport=None if binding.transport == "auto" else binding.transport)
        self.connection.set(State.WAITING_FOR_DEVICE)
        self.lost.clear()
        self.wake.clear()
        self.usb_armed = True
        self.found = None
        self.task = asyncio.create_task(self._supervise())
        return self.status()

    async def disconnect(self) -> dict:
        await self._stop_supervisor()
        self.binding = None
        self.connection = Connection()
        self._pulse()
        return self.status()

    async def unbind(self) -> dict:
        """Disconnect and forget every pairing record; the next phone needs explicit setup."""
        await self.disconnect()
        for transport in self.transports.values():
            forget = getattr(transport, "forget", None)
            if forget is not None:
                forget()
        return self.status()

    def _auto(self) -> bool:
        return self.binding is not None and self.binding.transport == "auto"

    def _candidates(self, failed: str | None = None) -> list[str]:
        """Transports to look on. Auto mode uses both, except right after an attempt on USB
        failed: then only Wi-Fi is searched, so a broken cable session does not starve a working
        Wi-Fi one. USB comes back with the next attach of the bound phone."""
        assert self.binding is not None
        if not self._auto():
            return [self.binding.transport]
        return ["wifi"] if failed == "usb" else ["usb", "wifi"]

    async def _stop_supervisor(self):
        window, self.window = self.window, None
        task, self.task = self.task, None
        for running in (task, window):
            if running is not None:
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running
        self.connection.searching = False
        await self._teardown()

    async def _teardown(self):
        self.gate.release()
        session, self.session = self.session, None
        tunnel, self.tunnel = self.tunnel, None
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.close(), 10)
        if tunnel is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tunnel.aclose(), 10)
        self.connection.hid_active = False

    def _set(self, state: State, **changes):
        self.connection.set(state, **changes)
        self._pulse()

    def _pulse(self):
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _changes(self, timeout: float | None = None) -> None:
        """Wait for the next pulse (state, cable or window change), at most ``timeout``."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)

    # ----- cable events --------------------------------------------------------------

    def _bound_on_cable(self) -> bool:
        return self.binding is not None and _key(self.binding.udid) in {_key(s) for s in self.cable}

    def _usb_update(self, serials) -> None:
        current = set(serials)
        if current == self.cable:
            return
        before = self._bound_on_cable()
        self.cable = current
        self.usb_changes += 1
        after = self._bound_on_cable()
        if after and not before:
            self.attaches += 1
        elif before and not after:
            self.usb_armed = True
            # Cable pulled: look for the phone on Wi-Fi, unless the session already runs there.
            if not (self.connection.state == State.CONNECTED.value and self.connection.transport == "wifi"):
                self.search()
        self._pulse()

    async def _usb_events(self):
        """Follow usbmuxd attach and detach events. If Listen fails, poll only USB presence
        every ``poll_seconds`` (never Bonjour) and retry Listen with a bounded backoff."""
        usb: UsbTransport = self.transports["usb"]  # ty: ignore[invalid-assignment]
        retry, warned = LISTEN_RETRY_MIN, False
        while True:
            try:
                async with contextlib.aclosing(usb.watch()) as events:
                    async for serials in events:
                        if not self.listening:
                            self.listening, retry, warned = True, LISTEN_RETRY_MIN, False
                        self._usb_update(serials)
                raise ConnectionError("usbmuxd closed the Listen connection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.listening = False
                if not warned:
                    warned = True
                    log.warning("usbmuxd Listen failed (%s); polling USB presence", type(exc).__name__)
            until = self.clock() + retry
            while self.clock() < until:
                with contextlib.suppress(Exception):
                    self._usb_update(await usb.serials())
                await asyncio.sleep(usb.poll_seconds)
            retry = min(retry * 2, LISTEN_RETRY_MAX)

    async def usb_events(self, since: int | None, timeout: float) -> dict:
        """Long poll for the setup-mode auto-pairing: the serials on the cable once the change
        counter differs from ``since`` (at once when ``since`` is None), or at ``timeout``."""
        deadline = self.clock() + timeout
        while since == self.usb_changes and self.clock() < deadline:
            await self._changes(deadline - self.clock())
        return {"changes": self.usb_changes, "serials": sorted(self.cable), "listening": self.listening}

    # ----- search windows ------------------------------------------------------------

    def search(self) -> asyncio.Task | None:
        """Open a search window, or return the one already open (never extended by repeats).
        None when nothing is bound or a session is up or being established."""
        if self.binding is None or self.task is None:
            return None
        if self.window is not None and not self.window.done():
            return self.window
        if self.connection.state in BUSY_STATES:
            return None
        self.windows += 1
        self.connection.searching = True
        self.window = asyncio.create_task(
            self._search_window(self.binding.udid, self.clock() + self.config.search_window_seconds)
        )
        self._pulse()
        return self.window

    async def _search_window(self, udid: str, deadline: float) -> str | None:
        """Return the transport the bound phone was found on, or None at the deadline.

        USB is checked once; then Bonjour browses run back to back. A browse never starts if it
        would end more than one second past the deadline, and an attempt that returns early
        (an mDNS error) still takes its full slot, so the attempts per window are bounded."""
        names = self._candidates("usb" if self._auto() and not self.usb_armed else None)
        try:
            if "usb" in names and await self.transports["usb"].present(udid):
                return self._found("usb")
            if "wifi" not in names:
                return None
            wifi = self.transports["wifi"]
            paired = getattr(wifi, "paired", None)
            if callable(paired) and not paired(udid):
                self.connection.wifi_paired = False
                return None
            browse = self.config.discovery_seconds
            while self.clock() + browse <= deadline + 1:
                started = self.clock()
                self.browses += 1
                if await wifi.present(udid):
                    return self._found("wifi")
                rest = started + browse - self.clock()
                if rest > 0:
                    await asyncio.sleep(rest)
            return None
        finally:
            self.connection.searching = False
            self._pulse()

    def _found(self, name: str) -> str:
        """Hand the find to the supervisor. CONNECTING is set here so a request woken by the
        window's end holds for the connect instead of failing before the supervisor runs."""
        self.found = name
        self._set(State.CONNECTING, device_present=True, transport=name)
        return name

    async def _await_window(self, window: asyncio.Task) -> str | None:
        """The window's result without cancelling it when the caller is cancelled: other waiters
        may share it."""
        await asyncio.wait({window})
        return None if window.cancelled() or window.exception() is not None else window.result()

    # ----- supervision ---------------------------------------------------------------

    async def _supervise(self):
        assert self.binding is not None
        udid, attempts = self.binding.udid, 0
        failed, search = None, True
        while True:
            remount = switching = False
            name = None
            try:
                name, transport = await self._wait_for_device(udid, failed, search)
                failed = None
                self._set(State.CONNECTING, device_present=True, transport=name)
                provider, closers, details = await transport.open(udid)
                tunnel = self.tunnel_factory(provider, closers, mode=self.config.tunnel_mode)
                rsd = await tunnel.open()
                self.tunnel = tunnel
                self.connection.product_version = details.get("product_version") or getattr(
                    rsd, "product_version", None
                )
                if rsd.udid and not same_udid(str(rsd.udid), udid):
                    raise OpenLoloError("IDENTITY_MISMATCH", "The reachable phone is not the bound phone")
                session = self.session_factory(rsd, self.config, self.lost.set)
                prepared = await session.prepare(
                    self.config.auto_mount_ddi or self.mount_requested, self._set
                )
                self.mount_requested = False
                self.connection.developer_mode = prepared.get("developer_mode")
                self.connection.ddi_mounted = prepared.get("ddi_mounted")
                if prepared.get("mounted_now"):
                    remount = True
                    raise Remount()
                await session.open()
                self.session = session
                attempts = 0
                self._set(State.CONNECTED, attempts=0, last_error=None)
                switching = await self._watch(tunnel, session, name, udid)
                if switching:
                    self._set(State.SWITCHING, last_error=None)
                else:
                    self._set(State.RECONNECTING, last_error={"code": "DEVICE_DISCONNECTED"})
            except asyncio.CancelledError:
                raise
            except Remount:
                pass
            except Exception as exc:
                error = classify(exc)
                attempts += 1
                failed = name
                if name == "usb":
                    self.usb_armed = False
                self._set(OWNER_STATES.get(error.code, State.RECONNECTING), last_error=error.as_dict())
                self.connection.attempts = attempts
            finally:
                await self._teardown()
            search = True
            if remount or switching:
                search = False
                continue
            if self._auto() and failed is not None and attempts == 1:
                continue  # First failure on one path: look on the other path at once.
            if failed is None:
                # A session that was up was lost. Pause briefly so a flapping link cannot spin.
                await self._pause(self.config.reconnect_min_seconds)
                continue
            if self.connection.state in OWNER_STATE_VALUES or attempts > RETRY_LIMIT:
                # Only the owner (or a new event) can fix this: no timer, no polling.
                search = await self._event()
                continue
            await self._pause(
                min(
                    self.config.reconnect_min_seconds * 2 ** max(attempts - 1, 0),
                    self.config.reconnect_max_seconds,
                )
            )

    async def _pause(self, seconds: float) -> None:
        """Back off, ending early on ``recover``/``request_mount`` or a new search window."""
        windows, deadline = self.windows, self.clock() + seconds
        while not self.wake.is_set() and self.windows == windows:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            await self._changes(remaining)

    async def _event(self) -> bool:
        """Wait, without any timer, for the next reason to look for the phone: an attach of the
        bound phone, a search window, or ``recover``/``request_mount``. Returns whether to open a
        window (only for ``recover``; an attach is a USB-only check)."""
        attaches, windows = self.attaches, self.windows
        while True:
            if self.wake.is_set():
                return True
            if self.attaches != attaches or self.windows != windows:
                return False
            await self._changes()

    async def _wait_for_device(self, udid, failed=None, search=True):
        """Return the name and object of the transport on which the bound phone is reachable.

        USB presence is checked first, so the cable wins whenever both are available. Wi-Fi is
        only looked for through a search window: one is opened when ``search`` is set, and one
        opened by anyone else is joined. Between windows nothing runs until the next event."""
        names = self._candidates(failed)
        while True:
            self.wake.clear()
            if "usb" in names and await self.transports["usb"].present(udid):
                self.found = None
                self.connection.device_present = True
                return "usb", self.transports["usb"]
            window = self.window if self.window is not None and not self.window.done() else None
            if window is None and search and self.found is None:
                window = self.search()
            search = False
            if window is not None:
                await self._await_window(window)
            found, self.found = self.found, None
            if found is not None and found in names:
                return found, self.transports[found]
            # Nothing found: keep an owner-action state from the last failure so its message stays.
            self.connection.device_present = False
            if not (failed is not None and self.connection.state in OWNER_STATE_VALUES):
                self._set(State.WAITING_FOR_DEVICE, transport=None if self._auto() else names[0])
            failed, names = None, self._candidates()
            if self._auto():
                paired = getattr(self.transports["wifi"], "paired", None)
                self.connection.wifi_paired = bool(paired(udid)) if callable(paired) else None
            search = await self._event()

    async def _watch(self, tunnel, session, name, udid) -> bool:
        """Block until the session is lost or, in auto mode on Wi-Fi, until the bound phone is
        back on the cable and the session is idle. Returns True for that planned switch."""
        self.lost.clear()
        waiters = [
            asyncio.create_task(tunnel.wait_closed()),
            asyncio.create_task(self.lost.wait()),
            asyncio.create_task(self._probe(session)),
        ]
        upgrade = None
        if self._auto() and name != "usb" and "usb" in self.transports:
            upgrade = asyncio.create_task(self._usb_upgrade())
            waiters.append(upgrade)
        try:
            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            return upgrade is not None and upgrade in done and upgrade.exception() is None
        finally:
            for waiter in waiters:
                waiter.cancel()
            for waiter in waiters:
                with contextlib.suppress(BaseException):
                    await waiter

    async def _usb_upgrade(self):
        """Return once the bound phone is on the cable (and USB is armed) and nothing is
        mid-flight. Driven by cable events; the 0.1 s idle check runs only while the phone is on
        the cable and input is in progress, and a cable pulled again cancels the move."""
        while True:
            if self._bound_on_cable() and self.usb_armed:
                if self.gate.generation is None and not self.io_lock.locked():
                    return
                await self._changes(0.1)
            else:
                await self._changes()

    async def _probe(self, session):
        while True:
            await asyncio.sleep(self.config.probe_interval)
            try:
                await session.idle_check()
                async with self.io_lock:
                    await session.probe()
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            self.connection.hid_active = session.hid_active

    async def _watchdog(self):
        """Release a held input generation when its lease expires. Sleeps on state changes while
        no generation is open, so an idle worker has no 0.5 s timer."""
        while True:
            if self.gate.generation is None:
                await self._changes()
                continue
            await asyncio.sleep(0.5)
            if self.gate.expired():
                self.gate.release()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._release(), 1)

    # ----- operations ----------------------------------------------------------------

    def _check(self, binding: Binding):
        if self.binding is None:
            raise OpenLoloError("DEVICE_DISCONNECTED", STATE_MESSAGES[State.DISCONNECTED])
        if binding != self.binding:
            raise OpenLoloError("IDENTITY_MISMATCH", "The request does not match the bound phone")

    def _connected(self) -> bool:
        return self.session is not None and self.connection.state == State.CONNECTED.value

    async def _hold(self, states, seconds: float, fresh_only: bool = False) -> None:
        deadline = self.clock() + seconds
        while (
            self.connection.state in states
            and not (fresh_only and self.connection.attempts)
            and self.clock() < deadline
        ):
            await self._changes(deadline - self.clock())

    async def _session(self):
        """The connected session. Right after a loss or during a transport switch the request
        waits for the supervisor to bring the session back, bounded by ``switch_hold_seconds``;
        once a reconnect attempt has failed that wait is skipped. If the phone is not connected
        the request opens (or joins) one search window, waits for it, and holds for a connect
        that started inside it; concurrent requests share the window."""
        await self._hold(HOLD_STATES, self.config.switch_hold_seconds, fresh_only=True)
        if not self._connected():
            window = self.search()
            if window is not None:
                await self._await_window(window)
            await self._hold(BUSY_STATES - {State.CONNECTED.value}, self.config.switch_hold_seconds)
        if not self._connected():
            raise OpenLoloError("DEVICE_DISCONNECTED", STATE_MESSAGES[State(self.connection.state)])
        return self.session

    def status(self) -> dict:
        if self.session is not None:
            self.connection.hid_active = self.session.hid_active
        return {
            "state": "ready" if self.connection.state == State.CONNECTED.value else "unavailable",
            "connection": self.connection.as_dict(),
        }

    async def capture(self, binding, options):
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            # Historically the authentication media stream wedged the screenshot service, so the
            # input session was closed before every screenshot and reopened by the next action.
            # With one RemoteXPC connection per request that stall may be gone; the stop is kept
            # only when hid_stop_before_capture is set so coexistence can be measured.
            png = self.config.screenshot_source == "png"
            if png and session.hid_active and self.config.hid_stop_before_capture:
                await session.stop_hid()
                self.connection.hid_active = False
            return await session.screenshot(binding, options, self.config.capture_deadline)

    async def begin(self, binding, kinds=None) -> str:
        """Open an input generation. The media-gated touch/keyboard session is opened only when
        the caller declares it needs it; button-only actions never start a stream."""
        self._check(binding)
        session = await self._session()
        await self._release()
        needs_stream = kinds is None or bool(set(kinds) & {"touch", "keyboard"})
        async with self.io_lock:
            if needs_stream:
                await session.ensure_hid()
            else:
                await session.ensure_buttons()
        self.connection.hid_active = session.hid_active
        generation = self.gate.begin()
        self._pulse()  # Wakes the lease watchdog.
        return generation

    async def report(self, generation: str, kind: str, value: list):
        self.gate.accept(generation, kind, value)
        session = await self._session()
        async with self.io_lock:
            if kind == "touch":
                await session.touch(value[0], value[1], value[2])
            elif kind == "keyboard":
                await session.keyboard(value)
            else:
                await session.button(value[0], value[1], value[2])

    async def gesture(self, generation: str, points: list) -> dict:
        """One touch gesture sent from here on its own clock (see ``hid.pace``): the API hands
        over the whole path once instead of one report per round trip, so the finger moves at
        a steady rate and the watchdog is fed by every update."""
        hid.validate_points(points)
        self.gate.accept(generation, "touch", list(points[0][1:]))
        session = await self._session()
        started = time.monotonic()

        async def send(state: str, x: int, y: int):
            self.gate.accept(generation, "touch", [state, x, y])
            await session.touch(state, x, y)

        async with self.io_lock:
            sent = await hid.pace(points, send)
        gaps = [round((b - a) * 1000) for a, b in zip(sent, sent[1:])]
        return {
            "reports": len(sent),
            "seconds": round(time.monotonic() - started, 3),
            "median_interval_ms": sorted(gaps)[len(gaps) // 2] if gaps else 0,
            "max_gap_ms": max(gaps) if gaps else 0,
        }

    async def _release(self):
        session = self.session
        # Nothing can be held without an open input session; do not wait behind a capture.
        if session is not None and session.hid_active:
            async with self.io_lock:
                await session.release_all()

    async def release_all(self):
        self.gate.release()
        await self._release()

    async def verify(self, binding) -> dict:
        if binding.transport == "auto":
            for name in ("usb", "wifi"):
                result = await self.transports[name].verify(binding.udid)
                if result.get("present"):
                    self._require_trust(name, result)
                    return {**result, "transport": name}
            raise OpenLoloError("DEVICE_DISCONNECTED", "The phone is not reachable over USB or Wi-Fi")
        transport = self.transports.get(binding.transport)
        if transport is None:
            raise OpenLoloError("UNSUPPORTED_TRANSPORT", status=400)
        result = await transport.verify(binding.udid)
        self._require_trust(binding.transport, result)
        if not result.get("present"):
            raise OpenLoloError(
                "DEVICE_DISCONNECTED", "The phone is not reachable on the requested transport"
            )
        return {**result, "transport": binding.transport}

    @staticmethod
    def _require_trust(name: str, result: dict):
        if not result.get("trusted"):
            code = "TRUST_REQUIRED" if name == "usb" else "WIFI_PAIRING_REQUIRED"
            raise OpenLoloError(code, STATE_MESSAGES[OWNER_STATES[code]])

    async def recover(self, binding, wait_seconds: float | None = None) -> dict:
        self._check(binding)
        self.gate.release()
        self.lost.set()
        self.wake.set()
        self._pulse()
        deadline = self.clock() + (self.config.recover_wait_seconds if wait_seconds is None else wait_seconds)
        # Let the supervisor act on the wake (drop the session) before testing for a new one.
        await asyncio.sleep(0)
        while self.clock() < deadline:
            await self._changes(deadline - self.clock())
            if self._connected():
                break
        return self.status()

    async def request_mount(self) -> dict:
        self.mount_requested = True
        self.lost.set()
        self.wake.set()
        self._pulse()
        return self.status()

    async def find(self) -> dict:
        """Open (or join) one search window without waiting for it: the top button and IPC
        ``search``. A no-op while connected or with nothing bound."""
        self.search()
        return self.status()

    async def list_apps(self, binding) -> list[dict]:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            return await session.list_apps()

    async def launch_app(
        self, binding, bundle_id: str, url: str | None = None, kill_existing: bool = False
    ) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            return await session.launch_app(bundle_id, url, kill_existing)

    async def clipboard_get(self, binding) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            return await session.clipboard_get()

    async def clipboard_set(self, binding, text: str) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            return await session.clipboard_set(text)

    async def device_info(self, binding) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            info = await session.device_info()
        return {**info, "transport": self.connection.transport or binding.transport}
