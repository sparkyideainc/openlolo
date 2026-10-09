"""CoreDeviceManager: one bound phone, one active transport, one supervised persistent session.

Higher layers call ``capture``/``begin``/``report``/``release_all``/``list_apps``/``device_info``
and never learn whether the phone is on USB or Wi-Fi. A supervisor task discovers the phone,
establishes the tunnel and services, watches for loss, and reconnects with bounded backoff.

With the ``auto`` transport the supervisor tries USB first and Wi-Fi second on every attempt,
so a pulled cable moves the session to Wi-Fi as soon as the phone is found there. While on
Wi-Fi it keeps polling usbmux; when the bound phone is back on the cable it waits for an idle
moment (no input generation, no request holding the I/O lock) and moves the session to USB.
Requests that arrive during such a switch wait ``switch_hold_seconds`` for the session to come
back instead of failing. Only the bound UDID is ever considered on either path.
"""

import asyncio
import contextlib
import time

from openlolo.coredevice import hid
from openlolo.coredevice.hid import ReportGate
from openlolo.coredevice.session import DeviceSession
from openlolo.coredevice.state import OWNER_STATES, STATE_MESSAGES, Connection, State, classify
from openlolo.coredevice.transports import UsbTransport, WifiTransport, same_udid
from openlolo.coredevice.tunnel import Tunnel
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Binding


class Remount(Exception):
    """The developer disk image was just mounted; the RSD service list must be refreshed."""


# States a request waits through (bounded by ``switch_hold_seconds``) before giving up.
HOLD_STATES = {
    State.CONNECTING.value,
    State.RECONNECTING.value,
    State.SWITCHING.value,
    State.MOUNTING_DDI.value,
}


class CoreDeviceManager:
    def __init__(self, config, transports=None, tunnel_factory=Tunnel, session_factory=DeviceSession):
        self.config = config
        cache = config.state_dir / "device" / "lockdown"
        self.transports = transports or {
            "usb": UsbTransport(cache),
            "wifi": WifiTransport(config.discovery_seconds),
        }
        self.tunnel_factory, self.session_factory = tunnel_factory, session_factory
        self.binding: Binding | None = None
        self.connection = Connection()
        self.gate = ReportGate()
        self.tunnel = None
        self.session = None
        self.task: asyncio.Task | None = None
        self.watchdog: asyncio.Task | None = None
        self.lost = asyncio.Event()
        self.wake = asyncio.Event()
        self.mount_requested = False
        self.io_lock = asyncio.Lock()
        # Auto mode: a failed USB attempt disarms the move back to the cable until the phone
        # has been seen off the cable once, so a broken USB path cannot flap the session.
        self.usb_armed = True

    # ----- lifecycle -----------------------------------------------------------------

    async def start(self):
        if self.watchdog is None:
            self.watchdog = asyncio.create_task(self._watchdog())

    async def close(self):
        await self.disconnect()
        if self.watchdog:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
            self.watchdog = None

    async def connect(self, binding: Binding) -> dict:
        if binding.transport != "auto" and binding.transport not in self.transports:
            raise OpenLoloError("UNSUPPORTED_TRANSPORT", status=400)
        await self._stop_supervisor()
        self.binding = binding
        self.connection = Connection(transport=None if binding.transport == "auto" else binding.transport)
        self.connection.set(State.WAITING_FOR_DEVICE)
        self.lost.clear()
        self.usb_armed = True
        self.task = asyncio.create_task(self._supervise())
        return self.status()

    async def disconnect(self) -> dict:
        await self._stop_supervisor()
        self.binding = None
        self.connection = Connection()
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
        """Transports to try, in order. Auto mode prefers USB, except right after an attempt on
        one path failed: the other path is tried first so a broken cable session does not
        starve a working Wi-Fi one (or the reverse)."""
        assert self.binding is not None
        if not self._auto():
            return [self.binding.transport]
        return ["wifi", "usb"] if failed == "usb" else ["usb", "wifi"]

    async def _stop_supervisor(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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

    # ----- supervision ---------------------------------------------------------------

    async def _supervise(self):
        assert self.binding is not None
        udid, attempts = self.binding.udid, 0
        failed = None
        while True:
            remount = switching = False
            name = None
            try:
                name, transport = await self._wait_for_device(udid, failed)
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
            if remount or switching:
                continue
            if self._auto() and failed is not None and attempts == 1:
                continue  # First failure on one path: try the other path at once.
            owner_action = State(self.connection.state) in set(OWNER_STATES.values())
            delay = (
                self.config.reconnect_max_seconds
                if owner_action
                else min(
                    self.config.reconnect_min_seconds * 2 ** max(attempts - 1, 0),
                    self.config.reconnect_max_seconds,
                )
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), delay)
            self.wake.clear()

    async def _wait_for_device(self, udid, failed=None):
        """Return the name and object of the first candidate transport on which the bound phone
        is reachable. USB is checked before Wi-Fi, so the cable wins whenever both are available."""
        names = self._candidates(failed)
        while True:
            for name in names:
                transport = self.transports[name]
                if await transport.present(udid):
                    self.connection.device_present = True
                    return name, transport
            self.connection.device_present = False
            self._set(State.WAITING_FOR_DEVICE, transport=None if self._auto() else names[0])
            failed, names = None, self._candidates()
            if self._auto():
                paired = getattr(self.transports["wifi"], "paired", None)
                self.connection.wifi_paired = bool(paired(udid)) if callable(paired) else None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.wake.wait(), min(self.transports[name].poll_seconds for name in names)
                )
            self.wake.clear()

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
            upgrade = asyncio.create_task(self._usb_upgrade(udid))
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

    async def _usb_upgrade(self, udid):
        """Return once the bound phone is on USB and nothing is mid-flight. The presence check
        is repeated every poll so a cable pulled again before the idle moment cancels the move."""
        usb = self.transports["usb"]
        while True:
            await asyncio.sleep(usb.poll_seconds)
            if not await usb.present(udid):
                self.usb_armed = True
                continue
            if not self.usb_armed:
                continue
            for _ in range(max(int(usb.poll_seconds / 0.1), 1)):
                if self.gate.generation is None and not self.io_lock.locked():
                    return
                await asyncio.sleep(0.1)

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
        while True:
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

    async def _session(self):
        """The connected session. Right after a loss or during a transport switch the request
        waits for the supervisor to bring the session back, bounded by ``switch_hold_seconds``;
        once a reconnect attempt has failed the wait is skipped."""
        deadline = time.monotonic() + self.config.switch_hold_seconds
        while (
            self.connection.state in HOLD_STATES
            and self.connection.attempts == 0
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        if self.session is None or self.connection.state != State.CONNECTED.value:
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
        return self.gate.begin()

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
        deadline = time.monotonic() + (
            self.config.recover_wait_seconds if wait_seconds is None else wait_seconds
        )
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if self.connection.state == State.CONNECTED.value and self.session is not None:
                break
        return self.status()

    async def request_mount(self) -> dict:
        self.mount_requested = True
        self.lost.set()
        self.wake.set()
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
