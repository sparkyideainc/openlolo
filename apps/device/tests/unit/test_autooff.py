"""Auto sleep controller (application/autooff.py) against fake power, worker and coordinator."""

import asyncio
import json
import os
import types

import pytest
from support import raises_code

from openlolo.application.autooff import AutoOff, PowerClient, Settings
from openlolo.config import Config


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePower:
    def __init__(self):
        self.status = {"pisugar": True, "external_power": False, "battery_percent": 64, "daemon_id": "d1"}
        self.calls: list[dict] = []
        self.commit_reply = {"ok": True, "cut_delay": 45}

    async def call(self, request):
        self.calls.append(request)
        op = request["op"]
        if op == "status":
            return dict(self.status) if self.status else None
        if op == "prepare":
            return {"ok": True, "daemon_id": "d1"}
        if op == "cancel":
            return {"ok": True}
        return dict(self.commit_reply)

    def ops(self):
        return [c["op"] for c in self.calls if c["op"] != "status"]


class FakeDevice:
    def __init__(self):
        self.connection = {"state": "waiting_for_device", "searching": False}
        self.fail = False

    async def health(self):
        if self.fail:
            raise ConnectionError("worker down")
        return {"state": "unavailable", "connection": dict(self.connection)}


class FakeCoordinator:
    def __init__(self):
        self.lease = False
        self.current = None
        self.queue = asyncio.Queue()
        self.closing = False

    def live(self):
        return self.lease


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    config = Config(backend="simulated", state_dir=tmp_path, runtime_dir=tmp_path)
    power, device, coordinator = FakePower(), FakeDevice(), FakeCoordinator()
    provisioning = types.SimpleNamespace(active=False, state=types.SimpleNamespace(value="idle"))
    reset = types.SimpleNamespace(stage=None)
    offs = []

    async def poweroff():
        offs.append(clock())

    auto = AutoOff(
        config,
        device=device,
        coordinator=coordinator,
        provisioning=provisioning,
        reset=reset,
        power=power,
        poweroff=poweroff,
        clock=clock,
    )
    return types.SimpleNamespace(
        auto=auto,
        clock=clock,
        power=power,
        device=device,
        coordinator=coordinator,
        provisioning=provisioning,
        reset=reset,
        offs=offs,
        tmp=tmp_path,
    )


async def run_for(rig, seconds, tick=2.0):
    end = rig.clock() + seconds
    while rig.clock() < end:
        await rig.auto.step()
        rig.clock.advance(tick)
    await rig.auto.step()


async def test_idle_on_battery_warns_then_powers_off(rig):
    await run_for(rig, 297)  # Last check at +298 s.
    assert rig.power.ops() == [] and rig.auto.view()["state"] == "counting"
    assert 0 <= rig.auto.view()["seconds_left"] <= 2
    await run_for(rig, 2)
    assert rig.power.ops() == ["prepare"] and rig.auto.view()["state"] == "warning"
    await run_for(rig, 8)  # Warning until +310 s.
    assert rig.offs == []
    await run_for(rig, 2)
    assert rig.power.ops() == ["prepare", "commit"] and len(rig.offs) == 1
    assert rig.coordinator.closing is True and rig.auto.view()["state"] == "shutting_down"
    commit = [c for c in rig.power.calls if c["op"] == "commit"][0]
    prepare = [c for c in rig.power.calls if c["op"] == "prepare"][0]
    assert commit["attempt"] == prepare["attempt"] and commit["daemon_id"] == "d1"


async def test_activity_restarts_the_count_and_cancels_a_warning(rig):
    await run_for(rig, 200)
    rig.auto.activity("mcp")
    await run_for(rig, 200)
    assert rig.power.ops() == []  # 200 s since the activity: not yet.
    await run_for(rig, 102)
    assert rig.power.ops() == ["prepare"]
    rig.auto.keep_on()  # The page's Keep on, during the warning.
    await rig.auto.step()
    assert rig.power.ops() == ["prepare", "cancel"] and rig.auto.view()["state"] == "counting"
    await run_for(rig, 20)
    assert rig.offs == [] and rig.coordinator.closing is False


@pytest.mark.parametrize(
    "block",
    [
        "external_power",
        "unknown_power",
        "no_pisugar",
        "connected",
        "searching",
        "worker_down",
        "lease",
        "queued",
        "local_mode",
        "joining",
        "reset",
        "inhibitor",
        "off",
    ],
)
async def test_each_condition_keeps_the_box_on(rig, block):
    if block == "external_power":
        rig.power.status["external_power"] = True
    elif block == "unknown_power":
        rig.power.status["external_power"] = None
    elif block == "no_pisugar":
        rig.power.status = None
    elif block == "connected":
        rig.device.connection["state"] = "connected"
    elif block == "searching":
        rig.device.connection["searching"] = True
    elif block == "worker_down":
        rig.device.fail = True
    elif block == "lease":
        rig.coordinator.lease = True
    elif block == "queued":
        rig.coordinator.queue.put_nowait(object())
    elif block == "local_mode":
        rig.provisioning.active = True
    elif block == "joining":
        rig.provisioning.state.value = "joining"
    elif block == "reset":
        rig.reset.stage = "challenge"
    elif block == "inhibitor":
        (rig.tmp / "inhibit-sleep").write_text(f"{os.getpid()}\n")
    elif block == "off":
        rig.auto.set_minutes(0)
    await run_for(rig, 3600, tick=30)
    assert rig.power.ops() == [] and rig.offs == []


async def test_a_condition_failing_during_the_warning_cancels_it(rig):
    await run_for(rig, 300)
    assert rig.power.ops() == ["prepare"]
    rig.power.status["external_power"] = True  # Plugged in during the warning.
    await run_for(rig, 12)
    assert rig.power.ops() == ["prepare", "cancel"] and rig.offs == []


async def test_a_refused_commit_reopens_the_box(rig):
    rig.power.commit_reply = {"ok": False, "error": "EXTERNAL_POWER"}
    await run_for(rig, 312)
    assert rig.power.ops()[:3] == ["prepare", "commit", "cancel"]
    assert rig.offs == [] and rig.coordinator.closing is False and rig.auto.committed is False


async def test_the_setting_changes_the_deadline_and_survives_a_restart(rig):
    rig.auto.set_minutes(15)
    await run_for(rig, 600)
    assert rig.power.ops() == []
    await run_for(rig, 302)
    assert rig.power.ops() == ["prepare"]
    assert json.loads((rig.tmp / "power.json").read_text()) == {"auto_sleep_minutes": 15}
    assert Settings(rig.tmp / "power.json", 300).minutes() == 15
    assert oct((rig.tmp / "power.json").stat().st_mode & 0o777) == "0o600"
    with raises_code("INVALID_SETTING"):
        rig.auto.set_minutes(7)


def test_a_bad_settings_file_falls_back_to_config(tmp_path):
    path = tmp_path / "power.json"
    path.write_text("{not json")
    assert Settings(path, 300).minutes() == 5
    path.write_text('{"auto_sleep_minutes": 7}')
    assert Settings(path, 0).minutes() == 0


async def test_view_reports_support_and_battery(rig):
    await rig.auto.step()
    view = rig.auto.view()
    assert view["supported"] and view["on_battery"] and view["battery_percent"] == 64
    assert view["auto_sleep_minutes"] == 5 and view["choices"] == [0, 5, 15, 30, 60]
    rig.power.status = None
    await rig.auto.step()
    view = rig.auto.view()
    assert view["supported"] is False and view["state"] == "unsupported"


async def test_power_client_round_trip_and_missing_socket(tmp_path):
    path = tmp_path / "p.sock"
    assert await PowerClient(path).call({"op": "status"}) is None

    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        writer.write(json.dumps({"echo": request["op"]}).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, str(path))
    try:
        assert await PowerClient(path).call({"op": "status"}) == {"echo": "status"}
    finally:
        server.close()
        await server.wait_closed()
