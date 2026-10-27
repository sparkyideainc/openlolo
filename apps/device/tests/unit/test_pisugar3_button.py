"""PiSugar3 top button and LED daemon (deploy/openlolo_pisugar3.py) against a fake, flaky SMBus."""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
spec = importlib.util.spec_from_file_location("openlolo_pisugar3", DEPLOY / "openlolo_pisugar3.py")
assert spec and spec.loader
ps = importlib.util.module_from_spec(spec)
sys.modules["openlolo_pisugar3"] = ps
spec.loader.exec_module(ps)

ADDRESS = 0x57
POWER_REGISTERS = {0x02, 0x03, 0x09}  # power button, soft mode, cut delay: never written


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


class FakeLed:
    """Records each value written to the sysfs brightness file; ``broken`` makes writes fail."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "brightness"
        self.broken = False
        self.values: list[str] = []

    def write_text(self, value):
        if self.broken:
            raise PermissionError(13, "Permission denied")
        self.values.append(value)


class FakeSMBus:
    """Register file of the PiSugar3 MCU (bench snapshot) that fails a share of accesses."""

    def __init__(self, clock, failure=0.0, seed=0):
        self.clock = clock
        self.failure, self.random = failure, random.Random(seed)
        self.regs = {0x02: 0x7C, 0x03: 0x40, 0x08: 0x00, 0x09: 0x00, 0x0B: 0x00, 0xE0: 0x30}
        self.regs |= {0xE2 + i: ord(c) for i, c in enumerate("v1.2.7")}
        self.writes: list[tuple[int, int]] = []  # landed writes, unlock excluded
        self.accesses: list[float] = []
        self.missing = False
        self.relocks = 0

    def _access(self, address):
        assert address == ADDRESS
        if self.missing:
            raise OSError(121, "Remote I/O error")
        if self.accesses:
            assert self.clock() - self.accesses[-1] >= 0.02 - 1e-9, "accesses closer than 20 ms"
        self.accesses.append(self.clock())
        if self.random.random() < self.failure:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, address, register):
        self._access(address)
        return self.regs.get(register, 0)

    def write_byte_data(self, address, register, value):
        self._access(address)
        if register == 0x0B:
            self.regs[0x0B] = 1 if value == 0x29 else 0
            self.relocks += value == 0x00
            return
        if self.regs[0x0B] != 1:
            return  # write protected: ignored
        self.regs[0x0B] = 0  # the daemon unlocks before every write; make that necessary
        self.regs[register] = value
        self.writes.append((register, value))

    def tap(self, event):
        self.regs[0x08] = (self.regs[0x08] & 0xFC) | event

    def close(self):
        pass


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    smbus = FakeSMBus(clock)
    bus = ps.Bus(smbus, clock=clock, sleep=clock.sleep)
    led = FakeLed(tmp_path)
    status = ps.StatusLed(led.path)
    status.path = led
    daemon = ps.Daemon(
        bus,
        setup_flag=tmp_path / "setup-button",
        pair_flag=tmp_path / "pair-button",
        indicator=tmp_path / "indicator",
        led=status,
        clock=clock,
    )
    return clock, smbus, daemon, tmp_path, led


def test_stale_event_at_start_is_cleared_without_a_flag(rig):
    clock, smbus, daemon, tmp, led = rig
    smbus.tap(ps.LONG)
    daemon.start()
    assert smbus.regs[0x08] & 0x03 == 0
    assert not (tmp / "setup-button").exists() and daemon.firmware == "v1.2.7"


def test_press_and_hold_touch_their_flags_double_is_ignored(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    smbus.tap(ps.SINGLE)
    daemon.tick()
    assert (tmp / "pair-button").exists() and not (tmp / "setup-button").exists()
    clock.sleep(3)
    smbus.tap(ps.LONG)
    daemon.tick()
    assert (tmp / "setup-button").exists()
    (tmp / "pair-button").unlink()
    clock.sleep(3)
    smbus.tap(ps.DOUBLE)
    daemon.tick()
    assert smbus.regs[0x08] & 0x03 == 0 and not (tmp / "pair-button").exists()


def test_repeat_within_debounce_is_dropped_and_logged(rig, capsys):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    smbus.tap(ps.LONG)
    daemon.tick()
    (tmp / "setup-button").unlink()
    clock.sleep(1)
    smbus.tap(ps.LONG)
    daemon.tick()
    assert not (tmp / "setup-button").exists()
    assert "top button hold dropped" in capsys.readouterr().out
    clock.sleep(2)
    smbus.tap(ps.LONG)
    daemon.tick()  # A confirming hold after the debounce goes through.
    assert (tmp / "setup-button").exists()


def test_board_led_follows_the_indicator_and_the_pisugar_leds_are_never_written(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    for _ in range(4):
        daemon.tick()
        clock.sleep(0.5)
    assert led.values == ["0"]  # off once at start, then quiet
    (tmp / "indicator").write_text("searching\n")
    for _ in range(4):
        daemon.tick()
        clock.sleep(0.5)
    assert led.values[1:] == ["1", "0", "1", "0"]  # slow blink, 1 Hz
    (tmp / "indicator").write_text("setup\n")
    for _ in range(4):
        daemon.tick()
        clock.sleep(0.25)
    assert led.values[-4:] == ["1", "0", "1", "0"]  # fast blink, 2 Hz
    (tmp / "indicator").write_text("connected\n")
    daemon.tick()
    clock.sleep(0.5)
    daemon.tick()
    assert led.values[-1] == "1" and led.values[-2] != "1"  # steady on: written once
    (tmp / "indicator").write_text("searching\n")
    daemon.tick()
    count = len(led.values)
    for _ in range(3):
        daemon.tick()
        clock.sleep(ps.TICK)
    assert len(led.values) <= count + 1  # 100 ms polls inside a 500 ms frame: at most one write
    (tmp / "indicator").write_text("idle\n")
    daemon.tick()
    count = len(led.values)
    daemon.tick()
    assert led.values[-1] == "0" and len(led.values) == count
    assert not any(register == 0xE0 for register, _ in smbus.writes)


def test_unwritable_led_does_not_stop_the_button(rig, capsys):
    clock, smbus, daemon, tmp, led = rig
    led.broken = True
    daemon.start()
    (tmp / "indicator").write_text("searching\n")
    for _ in range(3):
        daemon.tick()
        clock.sleep(0.5)
    smbus.tap(ps.LONG)
    daemon.tick()
    assert (tmp / "setup-button").exists()
    assert capsys.readouterr().out.count("status LED not written") == 1
    led.broken = False
    clock.sleep(0.5)
    daemon.tick()
    clock.sleep(0.5)
    daemon.tick()
    assert led.values in (["1", "0"], ["0", "1"])  # recovers on the next writes


def test_unknown_or_missing_indicator_is_idle(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    (tmp / "indicator").write_text("party\n")
    daemon.tick()
    assert daemon.pattern == "idle" and led.values == ["0"]
    (tmp / "indicator").write_text("setup\n")
    daemon.tick()
    (tmp / "indicator").unlink()
    daemon.tick()
    assert daemon.pattern == "idle"


def test_never_writes_power_registers_on_a_flaky_bus(tmp_path):
    clock = Clock()
    smbus = FakeSMBus(clock, failure=0.18, seed=7)
    bus = ps.Bus(smbus, clock=clock, sleep=clock.sleep)
    daemon = ps.Daemon(
        bus,
        setup_flag=tmp_path / "setup-button",
        pair_flag=tmp_path / "pair-button",
        indicator=tmp_path / "indicator",
        led=ps.StatusLed(tmp_path / "brightness"),
        clock=clock,
    )
    daemon.start()
    (tmp_path / "indicator").write_text("setup\n")
    for n in range(200):
        if n % 20 == 0:
            smbus.tap(ps.LONG if n % 40 else ps.SINGLE)
        daemon.tick()
        clock.sleep(ps.TICK)
    assert not {register for register, _ in smbus.writes} & POWER_REGISTERS
    assert not daemon.offline


def test_serve_retries_a_missing_board_then_runs(tmp_path):
    clock = Clock()
    smbus = FakeSMBus(clock)
    smbus.missing = True
    ticks = []

    def open_bus():
        return ps.Bus(smbus, clock=clock, sleep=clock.sleep)

    def factory(bus):
        daemon = ps.Daemon(
            bus,
            setup_flag=tmp_path / "s",
            pair_flag=tmp_path / "p",
            indicator=tmp_path / "i",
            led=ps.StatusLed(tmp_path / "brightness"),
            clock=clock,
        )
        original = daemon.tick

        def tick():
            ticks.append(clock())
            original()

        daemon.tick = tick
        return daemon

    def stop():
        if clock() > 1000 + ps.RETRY_OFFLINE + 1:
            smbus.missing = False
        return len(ticks) >= 3

    ps.serve(open_bus, factory, stop, clock=clock, sleep=clock.sleep)
    assert len(ticks) == 3 and ticks[0] >= 1000 + ps.RETRY_OFFLINE


def test_offline_after_ten_seconds_of_failed_reads(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    smbus.missing = True
    started = clock()
    while clock() - started < ps.OFFLINE_SECONDS - 1:
        daemon.tick()
        clock.sleep(ps.TICK)
    assert not daemon.offline
    clock.sleep(1)
    daemon.tick()
    assert daemon.offline


def events_rig(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.events = ps.EventLog(tmp / "button-events", clock=clock)
    daemon.start()
    return clock, smbus, daemon, tmp, led


def logged(tmp):
    return [line.split()[:2] for line in (tmp / "button-events").read_text().splitlines()]


def test_event_log_keeps_every_event_in_order(rig):
    clock, smbus, daemon, tmp, led = events_rig(rig)
    session = daemon.events.session
    for event in (ps.SINGLE, ps.SINGLE, ps.DOUBLE, ps.LONG, ps.LONG):
        smbus.tap(event)
        daemon.tick()
        clock.sleep(0.3)  # Quicker than the flag debounce: the log still keeps them all.
    assert logged(tmp) == [
        ["0", "start"],
        ["1", "single"],
        ["2", "single"],
        ["3", "double"],
        ["4", "long"],
        ["5", "long"],
    ]
    assert (tmp / "button-events").read_text().split()[2] == session


def test_event_log_is_capped_and_keeps_its_session(rig):
    clock, smbus, daemon, tmp, led = events_rig(rig)
    for _ in range(ps.EVENT_LOG_MAX + 3):
        daemon.events.append("single")
    lines = logged(tmp)
    assert lines[0] == ["0", "start"] and len(lines) <= ps.EVENT_LOG_KEEP + 4
    seqs = [int(seq) for seq, _ in lines[1:]]
    assert seqs == list(range(seqs[0], ps.EVENT_LOG_MAX + 4))  # No gap in what is kept


def test_offline_board_logs_a_lost_event(tmp_path):
    clock = Clock()
    smbus = FakeSMBus(clock)
    events = ps.EventLog(tmp_path / "button-events", clock=clock)
    runs = []

    def factory(bus):
        runs.append(1)
        return ps.Daemon(
            bus,
            setup_flag=tmp_path / "s",
            pair_flag=tmp_path / "p",
            indicator=tmp_path / "i",
            led=ps.StatusLed(tmp_path / "brightness"),
            events=events,
            clock=clock,
        )

    def stop():
        if len(runs) == 1 and clock() > 1001:
            smbus.missing = True
        return len(runs) >= 2

    ps.serve(
        lambda: ps.Bus(smbus, clock=clock, sleep=clock.sleep), factory, stop, clock=clock, sleep=clock.sleep
    )
    assert logged(tmp_path)[-1] == ["1", "lost"]


def test_reset_patterns_on_the_board_led(rig):
    clock, smbus, daemon, tmp, led = events_rig(rig)
    status = daemon.led
    (tmp / "indicator").write_text("challenge\n")
    daemon.show()
    assert status.on is False  # Challenge: dark until a press is seen
    smbus.tap(ps.DOUBLE)
    daemon.tick()
    clock.sleep(ps.FLASH / 2)
    seen = []
    for _ in range(8):
        daemon.show()
        seen.append(status.on)
        clock.sleep(ps.FLASH)
    assert seen[:5] == [True, False, True, False, False]  # Two flashes for a double, then dark

    (tmp / "indicator").write_text("armed\n")
    daemon.show()
    clock.sleep(ps.FLASH / 2)  # Sample mid-frame
    frames = []
    for _ in range(26):
        daemon.show()
        frames.append(status.on)
        clock.sleep(ps.FLASH)
    assert frames[:13] == [True, False, True, False, True] + [False] * 8  # Triple flash, 2 s
    assert frames[13:] == frames[:13]


# ----- auto sleep: the power socket and the power cut -------------------------------------


def started(rig):
    clock, smbus, daemon, tmp, led = rig
    daemon.start()
    smbus.writes.clear()
    return clock, smbus, daemon


def test_status_reports_the_power_source_and_battery(rig):
    clock, smbus, daemon = started(rig)
    smbus.regs[0x2A] = 64
    status = daemon.power_request({"op": "status"})
    assert status == {
        "pisugar": True,
        "daemon_id": daemon.id,
        "attempt": None,
        "committed": False,
        "external_power": False,
        "battery_percent": 64,
    }
    smbus.regs[0x02] |= 0x80
    assert daemon.power_request({"op": "status"})["external_power"] is False  # Cached 2 s.
    clock.sleep(2)
    assert daemon.power_request({"op": "status"})["external_power"] is True
    assert smbus.writes == []


def test_prepare_then_commit_arms_the_cut_in_order(rig):
    clock, smbus, daemon = started(rig)
    reply = daemon.power_request({"op": "prepare", "attempt": "a1", "seconds": 10})
    assert reply == {"ok": True, "daemon_id": daemon.id}
    daemon.show()
    assert daemon.pattern == "shutdown"
    assert smbus.writes == []  # The warning writes nothing to the board.
    reply = daemon.power_request({"op": "commit", "attempt": "a1", "daemon_id": daemon.id})
    assert reply == {"ok": True, "cut_delay": ps.CUT_DELAY}
    assert smbus.writes == [(0x09, ps.CUT_DELAY), (0x03, 0x40), (0x02, 0x5C)]
    assert smbus.regs[0x03] & 0x10 == 0 and smbus.regs[0x02] & 0x20 == 0
    assert smbus.relocks == 1  # Writes locked again once the cut is armed.
    assert daemon.power_request({"op": "commit", "attempt": "a1", "daemon_id": daemon.id})["ok"] is False
    daemon.show()
    assert daemon.pattern == "resetting"  # Steady on until the power goes.


def test_commit_needs_a_live_prepare_from_this_daemon(rig):
    clock, smbus, daemon = started(rig)

    def commit(attempt="a1", daemon_id=None):
        request = {"op": "commit", "attempt": attempt, "daemon_id": daemon_id or daemon.id}
        return daemon.power_request(request)

    assert commit()["error"] == "STALE_ATTEMPT"  # Nothing prepared.
    daemon.power_request({"op": "prepare", "attempt": "a1", "seconds": 10})
    assert commit(daemon_id="other")["error"] == "WRONG_DAEMON"
    assert commit(attempt="a2")["error"] == "STALE_ATTEMPT"
    assert daemon.power_request({"op": "prepare", "attempt": "a2", "seconds": 10})["error"] == "BUSY"
    daemon.power_request({"op": "cancel", "attempt": "a1"})
    assert commit()["error"] == "STALE_ATTEMPT"  # Cancelled.
    daemon.power_request({"op": "prepare", "attempt": "a3", "seconds": 10})
    clock.sleep(10 + ps.PREPARE_GRACE)
    daemon.show()
    assert commit(attempt="a3")["error"] == "STALE_ATTEMPT"  # Expired.
    assert smbus.writes == [] and not daemon.committed


def test_commit_refuses_on_external_power(rig):
    clock, smbus, daemon = started(rig)
    daemon.power_request({"op": "prepare", "attempt": "a1", "seconds": 10})
    smbus.regs[0x02] |= 0x80  # Plugged in during the warning.
    reply = daemon.power_request({"op": "commit", "attempt": "a1", "daemon_id": daemon.id})
    assert reply["error"] == "EXTERNAL_POWER" and smbus.writes == [] and not daemon.committed


def test_a_failed_write_leaves_the_output_on(rig):
    clock, smbus, daemon = started(rig)
    write = smbus.write_byte_data

    def broken(address, register, value):
        if register == 0x03:
            raise OSError(121, "Remote I/O error")
        write(address, register, value)

    smbus.write_byte_data = broken
    daemon.power_request({"op": "prepare", "attempt": "a1", "seconds": 10})
    reply = daemon.power_request({"op": "commit", "attempt": "a1", "daemon_id": daemon.id})
    assert reply["error"] == "I2C_FAILED" and not daemon.committed
    assert smbus.regs[0x02] & 0x20  # The output switch was never cleared.


def test_an_output_write_that_may_have_landed_reports_maybe_armed(rig):
    clock, smbus, daemon = started(rig)
    write = smbus.write_byte_data

    def lands_then_bus_dies(address, register, value):
        write(address, register, value)
        if register == 0x02:
            smbus.missing = True  # The write landed; every later access fails.

    smbus.write_byte_data = lands_then_bus_dies
    daemon.power_request({"op": "prepare", "attempt": "a1", "seconds": 10})
    reply = daemon.power_request({"op": "commit", "attempt": "a1", "daemon_id": daemon.id})
    assert reply["error"] == "I2C_FAILED" and reply["maybe_armed"] is True
    assert daemon.committed and smbus.regs[0x02] & 0x20 == 0


def test_power_socket_round_trip_and_offline_reply():
    import json
    import socket
    import tempfile

    path = Path(tempfile.mkdtemp(dir="/tmp")) / "power.sock"  # AF_UNIX paths are short.
    server = ps.PowerServer(path)
    try:
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))
        client.sendall(b'{"op": "status"}\n')
        server.poll(ps.offline_request)
        assert json.loads(client.recv(4096)) == {"pisugar": False}
        client.close()
        server.poll(ps.offline_request)  # Nothing pending: returns at once.
    finally:
        server.close()
    assert not path.exists()
