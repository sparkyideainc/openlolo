"""CoreDeviceManager state machine with fake transports, tunnels and sessions."""

import asyncio
import contextlib

import pytest
from support import raises_code

from openlolo.config import Config
from openlolo.coredevice.manager import CoreDeviceManager
from openlolo.coredevice.state import State
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Binding, CaptureOptions, Frame, FrameMetadata

BINDING = Binding(
    udid="00008110-AAAAAAAAAAAAAAAA", transport="usb", owner_confirmed=True, portrait_locked=True
)


class FakeTransport:
    poll_seconds = 0.01

    def __init__(self, present=True, error=None):
        self.is_present = present
        self.error = error
        self.opened = 0
        self.verifications = []

    async def present(self, udid):
        return self.is_present

    async def open(self, udid):
        if self.error:
            raise self.error
        self.opened += 1
        return f"provider-{self.opened}", [], {"product_version": "27.0"}

    async def verify(self, udid):
        self.verifications.append(udid)
        return {"present": self.is_present, "trusted": True}


class FakeRsd:
    udid = BINDING.udid
    product_version = "27.0"


class FakeTunnel:
    instances = []

    def __init__(self, provider, closers, mode="userspace"):
        self.provider = provider
        self.mode = mode
        self.closed = asyncio.Event()
        self.open_count = 0
        FakeTunnel.instances.append(self)

    async def open(self):
        self.open_count += 1
        return FakeRsd()

    async def wait_closed(self):
        await self.closed.wait()

    async def aclose(self):
        self.closed.set()


class FakeSession:
    prepare_results = []
    instances = []

    def __init__(self, rsd, config, on_lost):
        self.on_lost = on_lost
        self.events = []
        self.hid_active = False
        self.closed = False
        FakeSession.instances.append(self)

    async def prepare(self, auto_mount, on_state):
        if FakeSession.prepare_results:
            result = FakeSession.prepare_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"developer_mode": True, "ddi_mounted": True, "mounted_now": False}

    async def open(self):
        pass

    async def probe(self):
        pass

    async def idle_check(self):
        pass

    async def screenshot(self, binding, options, deadline):
        metadata = FrameMetadata(
            geometry_epoch="g",
            width=390,
            height=844,
            native_width=390,
            native_height=844,
            content_rect=(0, 0, 390, 844),
            received_at=0,
            request_seconds=0,
            media_type="image/png",
        )
        return Frame(metadata, b"\x89PNG")

    async def ensure_hid(self):
        self.hid_active = True

    async def ensure_buttons(self):
        pass

    async def touch(self, state, x, y):
        self.events.append(("touch", state, x, y))

    async def keyboard(self, usages):
        self.events.append(("keyboard", list(usages)))

    async def button(self, page, usage, state):
        self.events.append(("button", page, usage, state))

    async def release_all(self):
        self.events.append("release")

    async def close(self):
        self.closed = True

    async def list_apps(self):
        return [{"bundle_id": "com.apple.Preferences", "name": "Settings"}]

    async def device_info(self):
        return {"product_type": "iPhone14,5"}


@pytest.fixture
def manager(tmp_path):
    FakeTunnel.instances.clear()
    FakeSession.instances.clear()
    FakeSession.prepare_results.clear()
    config = Config(
        backend="simulated",
        state_dir=tmp_path,
        runtime_dir=tmp_path,
        probe_interval=1,
        reconnect_min_seconds=0.01,
        reconnect_max_seconds=0.05,
        recover_wait_seconds=1,
    )
    transports = {"usb": FakeTransport(), "wifi": FakeTransport(present=False)}
    return CoreDeviceManager(config, transports, FakeTunnel, FakeSession)


async def wait_state(manager, state, timeout=2):
    async with asyncio.timeout(timeout):
        while manager.connection.state != state.value:
            await asyncio.sleep(0.005)


async def test_connect_capture_input_and_transport_loss_recovery(manager):
    await manager.start()
    try:
        with raises_code("DEVICE_DISCONNECTED"):
            await manager.capture(BINDING, CaptureOptions())
        await manager.connect(BINDING)
        await wait_state(manager, State.CONNECTED)
        status = manager.status()
        assert status["state"] == "ready" and status["connection"]["transport"] == "usb"
        assert status["connection"]["product_version"] == "27.0"
        frame = await manager.capture(BINDING, CaptureOptions())
        assert frame.image == b"\x89PNG"
        generation = await manager.begin(BINDING)
        await manager.report(generation, "touch", ["contact", 100, 200])
        await manager.report(generation, "keyboard", [4])
        await manager.report(generation, "button", [12, 64, "down"])
        session = FakeSession.instances[-1]
        assert session.events[-3:] == [
            ("touch", "contact", 100, 200),
            ("keyboard", [4]),
            ("button", 12, 64, "down"),
        ]
        await manager.release_all()
        assert session.events[-1] == "release" and manager.gate.generation is None
        with raises_code("INPUT_INTERRUPTED"):
            await manager.report(generation, "keyboard", [])
        # USB unplug: the tunnel transport closes, the supervisor waits and then reconnects.
        manager.transports["usb"].is_present = False
        FakeTunnel.instances[-1].closed.set()
        await wait_state(manager, State.WAITING_FOR_DEVICE)
        assert session.closed
        with raises_code("DEVICE_DISCONNECTED"):
            await manager.capture(BINDING, CaptureOptions())
        manager.transports["usb"].is_present = True
        await wait_state(manager, State.CONNECTED)
        assert manager.transports["usb"].opened == 2 and manager.connection.attempts == 0
        assert (await manager.list_apps(BINDING))[0]["bundle_id"] == "com.apple.Preferences"
        assert (await manager.device_info(BINDING))["transport"] == "usb"
    finally:
        await manager.close()
    assert manager.connection.state == State.DISCONNECTED.value


async def test_owner_action_states_and_identity(manager):
    manager.transports["usb"].error = OpenLoloError("TRUST_REQUIRED", "pair first")
    await manager.start()
    try:
        await manager.connect(BINDING)
        await wait_state(manager, State.TRUST_REQUIRED)
        assert manager.connection.last_error["code"] == "TRUST_REQUIRED"
        with pytest.raises(OpenLoloError, match="owner pairing"):
            await manager.capture(BINDING, CaptureOptions())
        manager.transports["usb"].error = None
        FakeSession.prepare_results.append(OpenLoloError("DEVELOPER_MODE_REQUIRED"))
        manager.wake.set()
        await wait_state(manager, State.DEVELOPER_MODE_REQUIRED)
        manager.wake.set()
        await wait_state(manager, State.CONNECTED)
        other = BINDING.model_copy(update={"udid": "00008110-BBBBBBBBBBBBBBBB"})
        with raises_code("IDENTITY_MISMATCH"):
            await manager.capture(other, CaptureOptions())
        wifi = BINDING.model_copy(update={"transport": "wifi"})
        with raises_code("DEVICE_DISCONNECTED"):
            await manager.verify(wifi)
        assert (await manager.verify(BINDING))["transport"] == "usb"
    finally:
        await manager.close()


async def test_ddi_mount_refreshes_services_and_recover_forces_reconnect(manager):
    FakeSession.prepare_results.append({"developer_mode": True, "ddi_mounted": True, "mounted_now": True})
    await manager.start()
    try:
        await manager.connect(BINDING)
        await wait_state(manager, State.CONNECTED)
        assert manager.transports["usb"].opened == 2  # Remount after the DDI is mounted, no backoff.
        assert manager.connection.ddi_mounted is True
        before = manager.transports["usb"].opened
        result = await manager.recover(BINDING)
        assert result["state"] == "ready" and manager.transports["usb"].opened == before + 1
    finally:
        await manager.close()


async def test_watchdog_releases_after_api_death(manager):
    await manager.start()
    try:
        await manager.connect(BINDING)
        await wait_state(manager, State.CONNECTED)
        await manager.begin(BINDING)
        manager.gate.last_report -= 10
        async with asyncio.timeout(2):
            while manager.gate.generation is not None:
                await asyncio.sleep(0.01)
        assert FakeSession.instances[-1].events[-1] == "release"
    finally:
        await manager.close()


async def test_switching_transport_restarts_supervision(manager):
    await manager.start()
    try:
        await manager.connect(BINDING)
        await wait_state(manager, State.CONNECTED)
        wifi = BINDING.model_copy(update={"transport": "wifi"})
        await manager.connect(wifi)
        await wait_state(manager, State.WAITING_FOR_DEVICE)
        assert manager.connection.transport == "wifi"
        with raises_code("IDENTITY_MISMATCH"):
            await manager.capture(BINDING, CaptureOptions())
        manager.transports["wifi"].is_present = True
        await wait_state(manager, State.CONNECTED)
        assert manager.transports["wifi"].opened == 1
    finally:
        with contextlib.suppress(Exception):
            await manager.close()


AUTO = BINDING.model_copy(update={"transport": "auto"})


async def wait_transport(manager, transport, timeout=2):
    async with asyncio.timeout(timeout):
        while (manager.connection.transport, manager.connection.state) != (transport, State.CONNECTED.value):
            await asyncio.sleep(0.005)


class PairedFakeTransport(FakeTransport):
    def __init__(self, present=True, paired=True):
        super().__init__(present)
        self.is_paired = paired
        self.forgotten = 0

    def paired(self, udid):
        return self.is_paired

    def forget(self, udid=None):
        self.forgotten += 1


async def test_auto_moves_to_wifi_on_unplug_and_back_to_usb_when_idle(manager):
    usb, wifi = manager.transports["usb"], PairedFakeTransport(present=False)
    manager.transports["wifi"] = wifi
    await manager.start()
    try:
        await manager.connect(AUTO)
        await wait_state(manager, State.CONNECTED)
        assert manager.connection.transport == "usb" and usb.opened == 1
        # Cable pulled while the phone is on the LAN: the next attempt finds it over Wi-Fi.
        usb.is_present, wifi.is_present = False, True
        FakeTunnel.instances[-1].closed.set()
        await wait_transport(manager, "wifi")
        assert wifi.opened == 1
        assert (await manager.device_info(AUTO))["transport"] == "wifi"
        # Cable back while an input generation is open: the move waits for the idle moment.
        generation = await manager.begin(AUTO)
        usb.is_present = True
        await asyncio.sleep(0.15)
        assert manager.connection.transport == "wifi" and usb.opened == 1
        await manager.report(generation, "touch", ["contact", 1, 1])
        await manager.release_all()
        await wait_transport(manager, "usb")
        assert usb.opened == 2 and manager.connection.attempts == 0
        assert manager.connection.last_error is None
        assert FakeSession.instances[-2].closed
    finally:
        await manager.close()


async def test_auto_requests_wait_through_a_switch(manager):
    usb, wifi = manager.transports["usb"], PairedFakeTransport(present=True)
    manager.transports["wifi"] = wifi
    manager.config.switch_hold_seconds = 2
    await manager.start()
    try:
        await manager.connect(AUTO)
        await wait_state(manager, State.CONNECTED)
        usb.is_present = False
        FakeTunnel.instances[-1].closed.set()
        await wait_state(manager, State.RECONNECTING)
        # Issued right after the loss: held until the Wi-Fi session is up, then served.
        frame = await manager.capture(AUTO, CaptureOptions())
        assert frame.image == b"\x89PNG" and manager.connection.transport == "wifi"
        # Nowhere to go: the wait ends once the supervisor is waiting for the phone.
        wifi.is_present = False
        FakeTunnel.instances[-1].closed.set()
        await wait_state(manager, State.WAITING_FOR_DEVICE)
        assert manager.connection.transport is None and manager.connection.wifi_paired is True
        with raises_code("DEVICE_DISCONNECTED"):
            await manager.capture(AUTO, CaptureOptions())
    finally:
        await manager.close()


async def test_auto_skips_unpaired_wifi_and_reports_it(manager):
    usb, wifi = manager.transports["usb"], PairedFakeTransport(present=False, paired=False)
    manager.transports["wifi"] = wifi
    usb.is_present = False
    await manager.start()
    try:
        await manager.connect(AUTO)
        await wait_state(manager, State.WAITING_FOR_DEVICE)
        await asyncio.sleep(0.05)
        assert manager.connection.wifi_paired is False and wifi.opened == 0
        with raises_code("DEVICE_DISCONNECTED"):
            await manager.verify(AUTO)
        usb.is_present = True
        assert (await manager.verify(AUTO))["transport"] == "usb"
        await wait_state(manager, State.CONNECTED)
        assert manager.connection.transport == "usb"
        # Unbind forgets every record on both transports and leaves nothing bound.
        result = await manager.unbind()
        assert result["connection"]["state"] == "disconnected" and manager.binding is None
        assert wifi.forgotten == 1
    finally:
        await manager.close()


async def test_auto_falls_back_to_wifi_when_the_usb_session_fails(manager):
    usb, wifi = manager.transports["usb"], PairedFakeTransport(present=True)
    manager.transports["wifi"] = wifi
    usb.error = OpenLoloError("TRUST_REQUIRED", "pair first")
    await manager.start()
    try:
        await manager.connect(AUTO)
        await wait_transport(manager, "wifi")
        assert usb.opened == 0 and wifi.opened == 1 and manager.connection.last_error is None
        # The cable is still there, but a failed USB path must not flap the Wi-Fi session.
        await asyncio.sleep(0.2)
        assert manager.connection.transport == "wifi" and wifi.opened == 1
        usb.error = None
        await asyncio.sleep(0.2)
        assert manager.connection.transport == "wifi" and wifi.opened == 1
        # Only once the phone has been off the cable is the move to USB attempted again.
        usb.is_present = False
        await asyncio.sleep(0.05)
        usb.is_present = True
        await wait_transport(manager, "usb")
        assert usb.opened == 1
    finally:
        await manager.close()
