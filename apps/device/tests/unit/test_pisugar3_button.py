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
    daemon = ps.Daemon(
        bus,
        setup_flag=tmp_path / "setup-button",
        pair_flag=tmp_path / "pair-button",
        indicator=tmp_path / "indicator",
        clock=clock,
    )
    return clock, smbus, daemon, tmp_path


def leds(smbus):
    return [value for register, value in smbus.writes if register == 0xE0]


def test_stale_event_at_start_is_cleared_without_a_flag(rig):
    clock, smbus, daemon, tmp = rig
    smbus.tap(ps.LONG)
    daemon.start()
    assert smbus.regs[0x08] & 0x03 == 0
    assert not (tmp / "setup-button").exists() and daemon.firmware == "v1.2.7"


def test_press_and_hold_touch_their_flags_double_is_ignored(rig):
    clock, smbus, daemon, tmp = rig
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


def test_repeat_within_debounce_is_dropped(rig):
    clock, smbus, daemon, tmp = rig
    daemon.start()
    smbus.tap(ps.LONG)
    daemon.tick()
    (tmp / "setup-button").unlink()
    clock.sleep(1)
    smbus.tap(ps.LONG)
    daemon.tick()
    assert not (tmp / "setup-button").exists()
    clock.sleep(2)
    smbus.tap(ps.LONG)
    daemon.tick()  # A confirming hold after the debounce goes through.
    assert (tmp / "setup-button").exists()


def test_idle_writes_nothing_and_patterns_follow_the_indicator(rig):
    clock, smbus, daemon, tmp = rig
    daemon.start()
    for _ in range(4):
        daemon.tick()
        clock.sleep(0.5)
    assert smbus.writes == [] and daemon.interval == ps.TICK
    (tmp / "indicator").write_text("setup\n")
    for _ in range(4):
        daemon.tick()
        clock.sleep(0.5)
    assert leds(smbus) == [0x3F, 0x30, 0x3F, 0x30]  # bits 4-7 kept as read
    (tmp / "indicator").write_text("reset_armed\n")
    daemon.tick()
    assert daemon.interval == ps.FAST_TICK
    (tmp / "indicator").write_text("resetting\n")
    daemon.tick()
    daemon.tick()
    assert leds(smbus)[-2:] == [0x3F, 0x3F]
    count = len(smbus.writes)
    (tmp / "indicator").write_text("idle\n")
    daemon.tick()
    daemon.tick()
    assert len(smbus.writes) == count  # the firmware's battery gauge takes the LEDs back


def test_unknown_or_missing_indicator_is_idle(rig):
    clock, smbus, daemon, tmp = rig
    daemon.start()
    (tmp / "indicator").write_text("party\n")
    daemon.tick()
    assert daemon.pattern == "idle" and smbus.writes == []
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
        clock=clock,
    )
    daemon.start()
    (tmp_path / "indicator").write_text("reset_armed\n")
    for n in range(200):
        if n % 20 == 0:
            smbus.tap(ps.LONG if n % 40 else ps.SINGLE)
        daemon.tick()
        clock.sleep(daemon.interval)
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
