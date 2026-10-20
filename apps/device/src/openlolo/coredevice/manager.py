"""CoreDeviceManager: one bound phone, one active transport, one supervised persistent session.

Higher layers call ``capture``/``begin``/``report``/``release_all``/``list_apps``/``device_info``
and never learn whether the phone is on USB or Wi-Fi. A supervisor task finds the phone,
establishes the tunnel and services, watches for loss, and reconnects.

The phone is found through events, never on a timer. usbmuxd pushes cable attach and detach
events over one Listen connection; an attach of the bound phone connects over USB at once. Wi-Fi
is searched only inside a bounded search window (``search_window_seconds``), and only these open
one: the boot search (once per OS boot, after the box has a network, since the worker can start
before Wi-Fi joins), the cable pulled, the top button (IPC ``search``), the box page's Reconnect
(``recover``) and a new binding or DDI mount request. Windows coalesce: while one is open every
trigger joins it. Nothing else looks for the phone: a request while disconnected fails at once, a
lost session waits for one of those triggers, and avahi advertisements of the bound phone only
refresh its cached address.

A locked phone refuses new Wi-Fi sessions (PHONE_LOCKED). Until the window that found it ends,
the connect is tried again every ``LOCKED_RETRY_SECONDS`` on the same address, so pressing the
top button and then unlocking connects; after that nothing retries until the next trigger.
Transient failures are retried with backoff only until the window that started the attempt
ends; after it only the cable is checked. No session is probed: the cable and the tunnel report
their own loss, and a Wi-Fi session that died silently is noticed by the next request.

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
from pathlib import Path

from openlolo.coredevice import announce, hid
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
# Restart avahi-browse this long after it exits (or is missing).
ANNOUNCE_RESTART = 300.0
# At boot the worker may start before Wi-Fi joins: the first search waits up to this long for a
# default route, checking every NETWORK_POLL seconds (boot only; a cable attach ends the wait).
BOOT_NETWORK_SECONDS, NETWORK_POLL = 90.0, 1.0
# A locked phone found by a window is tried again this often until the window's deadline.
LOCKED_RETRY_SECONDS = 2.0
# Touch-session idle check cadence while an input session is open.
HID_IDLE_TICK = 5.0
# Stream hold (the control lease's screen-recording indicator): longest accepted time to live,
# reopen delays after a failed open on a healthy session (then every HOLD_RETRY_LATER), and how
# often a held stream is checked when nothing else wakes the reconciler.
HOLD_TTL_MAX = 120.0
HOLD_RETRY = (2.0, 5.0, 10.0)
HOLD_RETRY_LATER = 30.0
HOLD_CHECK = 30.0


def _key(udid: str) -> str:
    return udid.replace("-", "").lower()


def default_route() -> bool:
    """Whether the box is on a network: a default route exists. In setup mode the radio is an
    access point without one; the boot search waits for a route."""
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return True
    return any(len(fields) > 1 and fields[1] == "00000000" for fields in (line.split() for line in lines))


class CoreDeviceManager:
    def __init__(
        self,
        config,
        transports=None,
        tunnel_factory=Tunnel,
        session_factory=DeviceSession,
        clock=time.monotonic,
        announcements=None,
        network_ok=default_route,
    ):
        self.config = config
        cache = config.state_dir / "device" / "lockdown"
        self.transports = transports or {
            "usb": UsbTransport(cache),
            "wifi": WifiTransport(
                config.discovery_seconds, config.state_dir / "device" / "wifi-address.json"
            ),
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
        # Deadline of the last window: a locked phone and transient failures are retried until
        # then; after it only the cable is checked until the next trigger.
        self.retry_until = float("-inf")
        self.network_ok = network_ok
        # avahi advertisements of RemotePairing; None disables following them.
        if announcements is None and "wifi" in self.transports and config.wifi_announcements:
            announcements = announce.announcements
        self.announcements = announcements
        self.announcer: asyncio.Task | None = None
        self.announced = 0
        # Set by ``connect``: the first search waits for the box to be on a network.
        self.booting = False
        # Whether the supervisor opens a window when it starts (the boot search, or a new binding);
        # False when this boot already had its search (a worker restart).
        self.first_search = True
        # The bound phone was pulled off the cable while its USB session was up: the search the
        # pull asks for runs once the supervisor has dropped that session.
        self.unplugged = False
        # Pulsed on every state, cable or window change; waiters take the current event.
        self._changed = asyncio.Event()
        # Stream hold for the control lease, owned by the API's coordinator and renewed while
        # its lease lives: an ID (never the lease token) and a deadline on ``clock``. The
        # reconciler keeps the media stream open while it is live.
        self.hold_id: str | None = None
        self.hold_expires = float("-inf")
        self.hold_attempts = 0
        self.hold_retry_at = float("-inf")
        self.hold_error: dict | None = None
        self.holding = None  # The session the reconciler marked held, if any.
        self.reconciler: asyncio.Task | None = None

    # ----- lifecycle -----------------------------------------------------------------

    async def start(self):
        if self.watchdog is None:
            self.watchdog = asyncio.create_task(self._watchdog())
        if self.listener is None and "usb" in self.transports:
            self.listener = asyncio.create_task(self._usb_events())
        if self.announcer is None and self.announcements is not None:
            self.announcer = asyncio.create_task(self._follow_announcements())
        if self.reconciler is None:
            self.reconciler = asyncio.create_task(self._reconcile())

    async def close(self):
        self.release_stream(None)
        for name in ("reconciler",):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.disconnect()
        for name in ("watchdog", "listener", "announcer"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def connect(self, binding: Binding, search: bool = True) -> dict:
        """Bind and supervise the phone. ``search`` opens one window once the box has a network;
        False only checks the cable (a worker restarted within a boot that already searched)."""
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
        self.unplugged = False
        self.booting = search
        self.first_search = search
        self.task = asyncio.create_task(self._supervise())
        return self.status()

    async def disconnect(self) -> dict:
        self.release_stream(None)
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
            # Cable pulled: look for the phone on Wi-Fi, unless the session already runs there. A
            # USB session still up is dropped first; the supervisor then runs the search.
            if not (self.connection.state == State.CONNECTED.value and self.connection.transport == "wifi"):
                if self.search() is None and self.binding is not None and self.task is not None:
                    self.unplugged = True
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
        deadline = self.clock() + self.config.search_window_seconds
        self.retry_until = deadline
        self.windows += 1
        self.connection.searching = True
        self.window = asyncio.create_task(self._search_window(self.binding.udid, deadline))
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
            self.connection.searched_at = time.time()
            self._pulse()

    def _found(self, name: str) -> str:
        """Hand the find to the supervisor. CONNECTING is set here so a request woken by the
        window's end holds for the connect instead of failing before the supervisor runs."""
        self.found = name
        self._set(State.CONNECTING, device_present=True, transport=name)
        return name

    # ----- advertisements ------------------------------------------------------------

    async def _follow_announcements(self):
        """Keep the bound phone's Wi-Fi address fresh from avahi's view of its advertisements, so
        the next search finds it fast. Never opens a window. If avahi-browse exits or is missing,
        try again after ``ANNOUNCE_RESTART``."""
        assert self.announcements is not None
        while True:
            try:
                async with contextlib.aclosing(self.announcements()) as items:
                    async for item in items:
                        self._announced(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("RemotePairing announcements failed (%s)", type(exc).__name__)
            await asyncio.sleep(ANNOUNCE_RESTART)

    def _announced(self, item) -> None:
        if self.binding is None or "wifi" not in self._candidates():
            return
        wifi: WifiTransport = self.transports["wifi"]  # ty: ignore[invalid-assignment]
        try:
            ours = wifi.matches(self.binding.udid, item.properties)
        except Exception:
            return
        if not ours:
            return
        wifi.remember(self.binding.udid, item.address, item.port)
        self.announced += 1

    async def _network_up(self, seconds: float) -> bool:
        """Wait until the box has a default route, at most ``seconds``. False when a cable
        attach, a search window or ``recover`` ended the wait first."""
        attaches, windows = self.attaches, self.windows
        deadline = self.clock() + seconds
        while not self.network_ok() and self.clock() < deadline:
            if self.wake.is_set() or self.attaches != attaches or self.windows != windows:
                return False
            await self._changes(min(NETWORK_POLL, deadline - self.clock()))
        return True

    async def _await_window(self, window: asyncio.Task) -> str | None:
        """The window's result without cancelling it when the caller is cancelled: other waiters
        may share it."""
        await asyncio.wait({window})
        return None if window.cancelled() or window.exception() is not None else window.result()

    # ----- supervision ---------------------------------------------------------------

    async def _supervise(self):
        assert self.binding is not None
        udid, attempts = self.binding.udid, 0
        failed: str | None = None
        # Whether the current attempt came from a trigger (boot, cable, button, page, binding):
        # only then may a retry open a Wi-Fi window. A lost session is not a trigger.
        triggered = self.first_search
        search: bool | str | None = triggered
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
                session.on_stream_end = self._pulse
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
                self.booting = False
                triggered = False
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
            search = triggered
            if remount or switching:
                search = False
                continue
            owner = self.connection.state in OWNER_STATE_VALUES
            locked = self.connection.state == State.PHONE_LOCKED.value
            if locked and failed is not None and await self._locked_retry():
                # Found but locked inside the window: try the same address again, so a press
                # and then an unlock connects without a second press.
                self.found = failed
                search = False
                continue
            if self._auto() and failed is not None and attempts == 1 and not locked:
                # First failure on one path: look on the other path at once. Not for a locked
                # phone: it refuses on every path until it is unlocked.
                continue
            if failed is None:
                # A session that was up was lost. Pause briefly so a flapping link cannot spin,
                # then check the cable; Wi-Fi is searched only if the loss was the cable pulled.
                await self._pause(self.config.reconnect_min_seconds)
                triggered, self.unplugged = self.unplugged, False
                search = triggered
                continue
            if owner or attempts > RETRY_LIMIT:
                # Only the owner or a new trigger can fix this.
                search = await self._event()
                triggered = True
                continue
            # Transient: back off and look again (Wi-Fi too only inside a triggered attempt).
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

    async def _locked_retry(self) -> bool:
        """Wait ``LOCKED_RETRY_SECONDS`` while the phone stays locked, with the status LED still
        searching. False when the window that found it has no time left for another try; a new
        window, a cable attach or ``recover`` ends the wait early."""
        if self.clock() + LOCKED_RETRY_SECONDS > self.retry_until:
            return False
        self.connection.searching = True
        try:
            await self._pause(LOCKED_RETRY_SECONDS)
        finally:
            self.connection.searching = False
            self.connection.searched_at = time.time()
        return True

    async def _event(self) -> str | None:
        """Wait for the next reason to look for the phone: an attach of the bound phone, a
        search window, or ``recover``/``request_mount`` (returns "wake", which opens a window).
        None means look without opening a window (an attach is a USB-only check, a window opened
        by someone else is joined)."""
        attaches, windows = self.attaches, self.windows
        while True:
            if self.wake.is_set():
                return "wake"
            if self.attaches != attaches or self.windows != windows:
                return None
            await self._changes()

    async def _wait_for_device(self, udid, failed=None, search: bool | str | None = True):
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
            if window is None and search and self.found is None and self.booting:
                self.booting = False
                if not await self._network_up(BOOT_NETWORK_SECONDS):
                    continue  # A cable attach, a window or recover ended the wait: look again.
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
            asyncio.create_task(self._hid_idle(session)),
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

    async def _hid_idle(self, session):
        """Close the touch/keyboard session after ``hid_idle_seconds`` unused. Sleeps on state
        changes while none is open, so an idle connection has no timer here."""
        while True:
            if not session.hid_active:
                await self._changes()
                continue
            await asyncio.sleep(min(HID_IDLE_TICK, self.config.hid_idle_seconds))
            try:
                await session.idle_check()
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
        """The connected session. During a transport switch or a connect already under way the
        request waits for it, bounded by ``switch_hold_seconds``; once a reconnect attempt has
        failed that wait is skipped. A request never searches: with no session it joins a window
        a trigger already opened, else it fails at once with a hint to press the top button."""
        await self._hold(HOLD_STATES, self.config.switch_hold_seconds, fresh_only=True)
        if not self._connected():
            window = self.window if self.window is not None and not self.window.done() else None
            if window is not None:
                await self._await_window(window)
            await self._hold(BUSY_STATES - {State.CONNECTED.value}, self.config.switch_hold_seconds)
        if not self._connected():
            state = State(self.connection.state)
            if state == State.PHONE_LOCKED:
                raise OpenLoloError("PHONE_LOCKED", STATE_MESSAGES[state])
            raise OpenLoloError("DEVICE_DISCONNECTED", STATE_MESSAGES[state])
        return self.session

    def status(self) -> dict:
        if self.session is not None:
            self.connection.hid_active = self.session.hid_active
        self.connection.indication = self._indication()
        self.connection.indication_error = (
            self.hold_error if self.connection.indication == "unavailable" else None
        )
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
            frame = await session.screenshot(binding, options, self.config.capture_deadline)
        self._pulse()  # A streamed screenshot may have opened the input stream: wake its idle timer.
        return frame

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
        self._invalidate(session, "input")
        self._pulse()  # Wakes the lease watchdog.
        return generation

    async def report(self, generation: str, kind: str, value: list):
        self.gate.accept(generation, kind, value)
        session = await self._session()
        self._invalidate(session, "input")
        try:
            async with self.io_lock:
                if kind == "touch":
                    await session.touch(value[0], value[1], value[2])
                elif kind == "keyboard":
                    await session.keyboard(value)
                else:
                    await session.button(value[0], value[1], value[2])
        finally:
            self._invalidate(session, "input")

    async def gesture(self, generation: str, points: list) -> dict:
        """One touch gesture sent from here on its own clock (see ``hid.pace``): the API hands
        over the whole path once instead of one report per round trip, so the finger moves at
        a steady rate and the watchdog is fed by every update."""
        hid.validate_points(points)
        self.gate.accept(generation, "touch", list(points[0][1:]))
        session = await self._session()
        self._invalidate(session, "input")
        started = time.monotonic()

        async def send(state: str, x: int, y: int):
            self.gate.accept(generation, "touch", [state, x, y])
            await session.touch(state, x, y)

        try:
            async with self.io_lock:
                sent = await hid.pace(points, send)
        finally:
            self._invalidate(session, "input")
        gaps = [round((b - a) * 1000) for a, b in zip(sent, sent[1:])]
        return {
            "reports": len(sent),
            "seconds": round(time.monotonic() - started, 3),
            "median_interval_ms": sorted(gaps)[len(gaps) // 2] if gaps else 0,
            "max_gap_ms": max(gaps) if gaps else 0,
        }

    async def _release(self):
        session = self.session
        # Nothing can be held without open input services; do not wait behind a capture.
        if session is not None and session.input_open:
            async with self.io_lock:
                await session.release_all()

    async def release_all(self):
        self.gate.release()
        await self._release()

    # ----- stream hold ----------------------------------------------------------------

    def hold_stream(self, hold_id: str, ttl: float) -> dict:
        """Create or renew the control lease's stream hold; returns at once, the reconciler opens
        the stream. A new ID replaces the old hold."""
        if not isinstance(hold_id, str) or not 8 <= len(hold_id) <= 128:
            raise OpenLoloError("INVALID_REQUEST", status=400)
        if not 1 <= ttl <= HOLD_TTL_MAX:
            raise OpenLoloError("INVALID_REQUEST", status=400)
        if hold_id != self.hold_id:
            self.hold_id = hold_id
            self.hold_attempts, self.hold_retry_at, self.hold_error = 0, float("-inf"), None
        self.hold_expires = self.clock() + ttl
        self._pulse()
        return {"held": True, "indication": self._indication()}

    def release_stream(self, hold_id: str | None) -> dict:
        """End the hold now (``None``: whatever hold exists). Unknown IDs change nothing. The hold
        is cleared before anything waits, so an open still in flight closes when it lands."""
        if self.hold_id is not None and hold_id in (None, self.hold_id):
            self.hold_id, self.hold_expires, self.hold_error = None, float("-inf"), None
            self._pulse()
        return {"released": True}

    def _held(self) -> bool:
        return (
            self.hold_id is not None
            and self.hold_expires > self.clock()
            and self.binding is not None
            and self.config.hold_stream_with_lease
            and self.config.screenshot_source == "stream"
        )

    def _indication(self) -> str:
        if not self._held():
            return "off"
        session = self.session
        if session is not None and session.stream_running:
            return "on"
        return "unavailable" if self.hold_error is not None else "opening"

    async def _reconcile(self):
        """Keep the media stream open while the hold lives, and close it when the hold ends.

        The only task that opens or closes a stream in the background. It takes ``io_lock``, so it
        never runs between a capture's stream stop and its PNG capture, and it never opens on a
        session that is not the published, connected one. A lost transport is left to the
        supervisor; a failed open on a healthy session is retried on ``HOLD_RETRY``."""
        while True:
            try:
                # Look again at once after acting: the hold may have changed during the open.
                if await self._reconcile_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stream hold reconcile failed")
            wait = HOLD_CHECK
            if self._held():
                wait = min(wait, max(0.0, self.hold_expires - self.clock()))
                session = self.session
                if session is not None and not session.stream_running:
                    wait = min(wait, max(0.0, self.hold_retry_at - self.clock()))
            await self._changes(wait if self.hold_id is not None or self.holding is not None else None)

    async def _reconcile_once(self) -> bool:
        """One step toward the wanted state; True when it opened or closed something."""
        session = self.session
        if self.holding is not None and self.holding is not session:
            self.holding.stream_held = False  # A session that went away; its teardown closed it.
            self.holding = None
            self.hold_attempts, self.hold_retry_at = 0, float("-inf")
        usable = (
            session is not None
            and self.connection.state == State.CONNECTED.value
            and not getattr(session, "lost", False)
            and not self.lost.is_set()  # The transport went; the supervisor rebuilds it.
        )
        if self._held() and usable:
            assert session is not None
            session.stream_held = True
            self.holding = session
            if session.stream_running or self.clock() < self.hold_retry_at:
                return False
            async with self.io_lock:
                if self.session is not session or not self._held() or session.stream_running:
                    return True
                started = time.monotonic()
                try:
                    await session.hold_stream()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = classify(exc)
                    attempt = self.hold_attempts
                    delay = HOLD_RETRY[attempt] if attempt < len(HOLD_RETRY) else HOLD_RETRY_LATER
                    self.hold_attempts += 1
                    self.hold_retry_at = self.clock() + delay
                    self.hold_error = error.as_dict()
                    log.warning("stream hold open failed (%s); retry in %.0f s", error.code, delay)
                else:
                    self.hold_attempts, self.hold_retry_at, self.hold_error = 0, float("-inf"), None
                    log.info("stream held, open in %.2fs", time.monotonic() - started)
            self._pulse()
            return True
        if self.holding is session and session is not None and not self._held():
            async with self.io_lock:
                if self._held():
                    return True  # Held again while waiting: the next step keeps it open.
                # Cleared under the lock, so a new hold cannot find the stream unprotected.
                session.stream_held = False
                self.holding = None
                if self.session is session and session.stream_running:
                    left = await session.stop_hid()
                    log.info(
                        "stream hold released (%s phone streams left)", "unknown" if left is None else left
                    )
            self._pulse()
            return True
        return False

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
        self._invalidate(session, "launch")
        try:
            async with self.io_lock:
                return await session.launch_app(bundle_id, url, kill_existing)
        finally:
            self._invalidate(session, "launch")

    async def clipboard_get(self, binding) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            return await session.clipboard_get()

    async def clipboard_set(self, binding, text: str) -> dict:
        self._check(binding)
        session = await self._session()
        self._invalidate(session, "clipboard")
        try:
            async with self.io_lock:
                return await session.clipboard_set(text)
        finally:
            self._invalidate(session, "clipboard")

    @staticmethod
    def _invalidate(session, reason: str) -> None:
        """Called before and after an effect: no foreground answer from before the effect, or
        from a query that overlapped it, counts once it is done."""
        observer = getattr(session, "foreground", None)
        if observer is not None:
            observer.invalidate(reason)

    async def foreground(self, binding, fresh: bool = False) -> dict:
        """The foreground app plus observer counters, for the bench; nothing is enforced."""
        self._check(binding)
        session = await self._session()
        observer = getattr(session, "foreground", None)
        if observer is None:
            raise OpenLoloError("FOREGROUND_DISABLED", "Set foreground_observer in box.toml")
        # Not under io_lock: a DVT query must not wait behind a paced gesture.
        return {"foreground": await observer.snapshot(fresh), "observer": observer.status()}

    async def device_info(self, binding) -> dict:
        self._check(binding)
        session = await self._session()
        async with self.io_lock:
            info = await session.device_info()
        return {**info, "transport": self.connection.transport or binding.transport}
