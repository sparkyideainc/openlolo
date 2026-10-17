"""PiSugar3 backend of the button daemon: top button and LED feedback.

Loaded by ``openlolo-button.py`` when ``OPENLOLO_BUTTON_BACKEND=pisugar3`` (Lolo Pocket: Radxa
ZERO 3W on a PiSugar3). One thread, one tick every 500 ms (250 ms while the LEDs flash fast).

- Top button: single press touches the pairing flag, long press touches the setup flag (the
  same flag the GPIO backend touches), double press is cleared and ignored. The API decides
  what a press means (phone search, setup mode, reset arm and confirm).
- LEDs: the API writes one word to ``/run/openlolo/indicator``; the daemon mirrors it on the
  four LEDs. ``idle`` stops writing, so the firmware's battery gauge shows again.

The power button keeps the PiSugar3 defaults: this daemon never writes ``0x02``, ``0x03`` or
``0x09``. Soft mode without the power-button code would leave the power button unable to turn
the box off.

Bench facts this relies on (see docs/hardware/pisugar3.md):
- Back-to-back I2C accesses fail ~18%; accesses are spaced 20 ms and retried 3 times 50 ms apart,
  and every write is unlocked (``0x0B = 0x29``) first.
- A stale top-button event can be latched at boot; it is cleared at start without acting on it.
- ``0xE0`` LED control is non-exclusive: the firmware also drives the LEDs, so a pattern is
  rewritten every tick.
"""

import os
import sys
import time
from pathlib import Path

ADDRESS = 0x57
UNLOCK_REG, UNLOCK = 0x0B, 0x29

TAP = 0x08  # bits 0-1: 1 single, 2 double, 3 long
LEDS = 0xE0  # bits 0-3: one LED each, 1 on
LED_BITS = 0x0F
FIRMWARE = 0xE2  # ASCII version string, one byte per register
FIRMWARE_MAX = 16

SINGLE, DOUBLE, LONG = 1, 2, 3

TICK = 0.5
FAST_TICK = 0.25
GAP = 0.02  # minimum time between two bus accesses
TRIES = 3
RETRY_DELAY = 0.05
DEBOUNCE = 2.0
OFFLINE_TICKS = 20  # every read failed for 10 s: the board stopped answering
RETRY_OFFLINE = 30.0

# Indicator word -> (LED frames cycled one per tick, tick seconds).
PATTERNS = {
    "setup": ([LED_BITS, 0], TICK),  # 1 Hz
    "reset_armed": ([LED_BITS, 0], FAST_TICK),  # 2 Hz
    "resetting": ([LED_BITS], TICK),  # all on
}


class I2CError(Exception):
    pass


def log(message: str, level: int = 6) -> None:
    # systemd reads the <N> prefix as the journal priority (3 error, 4 warning, 6 info).
    print(f"<{level}>{message}", flush=True)


class Bus:
    """Spaced, retried and verified access to the PiSugar3 MCU."""

    def __init__(self, smbus, address=ADDRESS, clock=time.monotonic, sleep=time.sleep):
        self.smbus, self.address = smbus, address
        self.clock, self.sleep = clock, sleep
        self.last = float("-inf")

    def _access(self, operation, *args):
        wait = self.last + GAP - self.clock()
        if wait > 0:
            self.sleep(wait)
        try:
            return operation(self.address, *args)
        finally:
            self.last = self.clock()

    def _retry(self, attempt, what):
        error = None
        for n in range(TRIES):
            if n:
                self.sleep(RETRY_DELAY)
            try:
                return attempt()
            except (OSError, I2CError) as exc:
                error = exc
        raise I2CError(f"{what} failed after {TRIES} tries: {error}")

    def read(self, register: int) -> int:
        return self._retry(
            lambda: self._access(self.smbus.read_byte_data, register), f"read 0x{register:02x}"
        )

    def _put(self, register: int, value: int) -> None:
        self._retry(
            lambda: self._access(self.smbus.write_byte_data, register, value),
            f"write 0x{register:02x}=0x{value:02x}",
        )

    def write(self, register: int, value: int, mask: int = 0xFF) -> None:
        """Unlock, write, read back; ``mask`` names the bits that must read back as written.

        Each access is retried on its own and the whole write up to 3 times on a mismatch: at
        the bench's 18% failure rate for back-to-back accesses a single unlock-write-read
        attempt fails almost half the time.
        """

        def attempt():
            self._put(UNLOCK_REG, UNLOCK)
            self._put(register, value)
            back = self.read(register)
            if back & mask != value & mask:
                raise I2CError(f"read back 0x{back:02x}")

        self._retry(attempt, f"write 0x{register:02x}=0x{value:02x}")

    def put(self, register: int, value: int) -> None:
        """Unlock and write without a read-back, for registers the firmware also writes."""
        self._put(UNLOCK_REG, UNLOCK)
        self._put(register, value)


def touch(flag: Path) -> None:
    flag.touch()
    os.utime(flag, None)


class Daemon:
    def __init__(self, bus: Bus, *, setup_flag: Path, pair_flag: Path, indicator: Path, clock=time.monotonic):
        self.bus, self.clock = bus, clock
        self.setup_flag, self.pair_flag, self.indicator = setup_flag, pair_flag, indicator
        self.firmware = None
        self.last_event: dict[int, float] = {}
        self.failed_ticks = 0
        self.indicator_stamp = None
        self.indicator_word = "idle"
        self.pattern = "idle"
        self.frame = 0
        self.led_high = 0  # bits 4-7 of 0xE0 as read when a pattern starts, written back unchanged

    # Start-up --------------------------------------------------------------------------------

    def start(self) -> None:
        """Raises I2CError when the board does not answer after the retries."""
        # A stale event is latched at boot (the power-on press); clear it without acting on it.
        tap = self.bus.read(TAP)
        if tap & 0x03:
            self.bus.write(TAP, tap & 0xFC, 0x03)
        self.firmware = self.read_firmware()
        self.failed_ticks = 0
        self.indicator_stamp = None
        self.pattern = "idle"
        log(f"PiSugar3 top button ready (firmware {self.firmware or 'unknown'})")

    def read_firmware(self):
        text = ""
        try:
            for offset in range(FIRMWARE_MAX):
                byte = self.bus.read(FIRMWARE + offset)
                if not 0x20 < byte < 0x7F:
                    break
                text += chr(byte)
        except I2CError:
            pass
        return text or None

    # Main loop -------------------------------------------------------------------------------

    @property
    def offline(self) -> bool:
        return self.failed_ticks >= OFFLINE_TICKS

    @property
    def interval(self) -> float:
        return PATTERNS[self.pattern][1] if self.pattern in PATTERNS else TICK

    def tick(self) -> None:
        now = self.clock()
        try:
            tap = self.bus.read(TAP)
        except I2CError:
            tap = None
        self.failed_ticks = 0 if tap is not None else self.failed_ticks + 1
        if tap is not None:
            try:
                self.handle_tap(tap, now)
            except I2CError as exc:
                log(f"button event not cleared: {exc}", 4)
        self.show()

    def handle_tap(self, tap: int, now: float) -> None:
        event = tap & 0x03
        if not event:
            return
        self.bus.write(TAP, tap & 0xFC, 0x03)
        if event == DOUBLE:
            return
        last = self.last_event.get(event)
        if last is not None and now - last < DEBOUNCE:
            return
        self.last_event[event] = now
        flag = self.pair_flag if event == SINGLE else self.setup_flag
        touch(flag)
        log(f"top button {'press' if event == SINGLE else 'hold'}; flag {flag} touched")

    # LEDs ------------------------------------------------------------------------------------

    def read_indicator(self) -> str:
        """The API's indicator word; re-read only when the file changed."""
        try:
            stamp = self.indicator.stat().st_mtime_ns
        except OSError:
            self.indicator_stamp, self.indicator_word = None, "idle"
            return "idle"
        if stamp != self.indicator_stamp:
            self.indicator_stamp = stamp
            try:
                word = self.indicator.read_text().strip()
            except OSError:
                word = "idle"
            self.indicator_word = word if word in PATTERNS else "idle"
        return self.indicator_word

    def show(self) -> None:
        pattern = self.read_indicator()
        if pattern != self.pattern:
            self.pattern, self.frame = pattern, 0
            if pattern in PATTERNS:
                try:
                    self.led_high = self.bus.read(LEDS) & ~LED_BITS & 0xFF
                except I2CError:
                    self.led_high = 0
            log(f"LEDs: {pattern}")
        if pattern not in PATTERNS:
            return  # idle: no writes, the firmware's battery gauge shows
        frames = PATTERNS[pattern][0]
        bits = frames[self.frame % len(frames)]
        self.frame += 1
        try:
            self.bus.put(LEDS, self.led_high | bits)
        except I2CError:
            pass  # The next tick rewrites the LEDs anyway.


def serve(open_bus, daemon_factory, stop, clock=time.monotonic, sleep=time.sleep) -> None:
    """Run until ``stop()``; a missing or silent board is logged once and retried every 30 s."""
    reported = False

    def pause(seconds: float) -> None:
        end = clock() + seconds
        while not stop() and clock() < end:
            sleep(min(TICK, end - clock()))

    while not stop():
        try:
            daemon = daemon_factory(open_bus())
            daemon.start()
        except (OSError, I2CError) as exc:
            if not reported:
                log(f"PiSugar3 not reachable ({exc}); retrying every {RETRY_OFFLINE:.0f} s", 4)
                reported = True
            pause(RETRY_OFFLINE)
            continue
        if reported:
            log("PiSugar3 reachable again")
        reported = False
        while not stop() and not daemon.offline:
            started = clock()
            daemon.tick()
            pause(max(0.0, daemon.interval - (clock() - started)))
        if daemon.offline:
            log(f"PiSugar3 stopped answering; retrying every {RETRY_OFFLINE:.0f} s", 4)
            reported = True
            pause(RETRY_OFFLINE)


def main(stop) -> int:
    try:
        from smbus2 import SMBus
    except ImportError:
        print("python3-smbus2 is not installed; PiSugar3 top button disabled", file=sys.stderr)
        return 0
    bus_number = int(os.environ.get("OPENLOLO_I2C_BUS", "3"))
    run_dir = Path(os.environ.get("OPENLOLO_RUN_DIR", "/run/openlolo"))
    opened = []

    def open_bus():
        while opened:
            opened.pop().close()
        smbus = SMBus(bus_number)
        opened.append(smbus)
        return Bus(smbus)

    def factory(bus):
        return Daemon(
            bus,
            setup_flag=Path(os.environ.get("OPENLOLO_BUTTON_FLAG", run_dir / "setup-button")),
            pair_flag=Path(os.environ.get("OPENLOLO_PAIR_FLAG", run_dir / "pair-button")),
            indicator=run_dir / "indicator",
        )

    try:
        serve(open_bus, factory, stop)
    finally:
        while opened:
            opened.pop().close()
    return 0
