"""PiSugar3 backend of the button daemon: top button and status LED.

Loaded by ``openlolo-button.py`` when ``OPENLOLO_BUTTON_BACKEND=pisugar3`` (Lolo Pocket: Radxa
ZERO 3W on a PiSugar3). One thread, one tick every 100 ms.

- Top button: the firmware reports a hold only on release, with no held-now bit (bench
  2026-10-18), so the box acts on release; the fast poll makes that feel immediate. A single
  press touches the pairing flag, long press touches the setup flag (the same flag the GPIO
  backend touches). The API decides what a press means (a phone search, or leave Local mode; a
  hold opens Local mode).
- Event log: every event, double presses included and never debounced, is also appended to
  ``/run/openlolo/button-events`` as ``<seq> <single|double|long> <monotonic time>``, after a
  ``0 start <session>`` line written at start. The page's reset challenge counts presses there
  (the flags merge quick presses into one mtime). ``<seq> lost`` is written when the board stops
  answering, so an open challenge never counts across an outage.
- Status LED: the API writes one word to ``/run/openlolo/indicator``; the daemon mirrors it on
  the Radxa board's green LED (sysfs, ``OPENLOLO_STATUS_LED``): ``setup`` fast blink,
  ``searching`` slow blink, ``connected`` steady on, ``idle`` off; for a reset from the page
  ``challenge`` off with one short flash per press seen (two for a double), ``armed`` a triple
  flash every 2 s, ``resetting`` steady on. The PiSugar3's four LEDs stay the firmware's battery gauge: its charge animation overwrites ``0xE0`` between
  writes, so a pattern there was unreadable on USB power (bench 2026-10-18).

- Auto sleep: the API asks over ``/run/openlolo/power.sock`` (one JSON line each way) for the
  power source (``status``), a warning on the LED (``prepare``, fast flicker, expires on its
  own), ``cancel``, and ``commit``: re-read external power, then set the cut delay ``0x09``, turn
  soft mode off (``0x03`` bit 4) and clear the output switch (``0x02`` bit 5). The board cuts
  power ``OPENLOLO_CUT_DELAY`` seconds later while the API shuts the OS down. A commit needs the
  attempt and daemon IDs of a live ``prepare``, runs at most once per daemon, and a failed write
  turns the output switch back on.

Outside a commit the power button keeps the PiSugar3 defaults: this daemon never writes
``0x02``, ``0x03`` or ``0x09``. Soft mode without the power-button code would leave the power
button unable to turn the box off.

Bench facts this relies on (see design/hardware/pisugar3.md):
- Back-to-back I2C accesses fail ~18%; accesses are spaced 20 ms and retried 3 times 50 ms apart,
  and every write is unlocked (``0x0B = 0x29``) first.
- A stale top-button event can be latched at boot; it is cleared at start without acting on it.
"""

import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

ADDRESS = 0x57
UNLOCK_REG, UNLOCK = 0x0B, 0x29

TAP = 0x08  # bits 0-1: 1 single, 2 double, 3 long
FIRMWARE = 0xE2  # ASCII version string, one byte per register
FIRMWARE_MAX = 16

POWER, SOFT, CUT_DELAY_REG, BATTERY = 0x02, 0x03, 0x09, 0x2A
EXTERNAL_BIT = 0x80  # 0x02 bit 7: external power present (read only)
OUTPUT_BIT = 0x20  # 0x02 bit 5: 5 V output to the host; clearing it cuts power after 0x09 s
SOFT_BIT = 0x10  # 0x03 bit 4: soft shutdown mode; must be off before the cut (stuck state)
CUT_DELAY = 30  # seconds; Radxa bench shutdown took ~11 s on 2026-10-26 (OPENLOLO_CUT_DELAY)
STATUS_CACHE = 2.0  # seconds a power reading is reused
PREPARE_GRACE = 5.0  # a prepared attempt outlives its warning by this long, then expires

SINGLE, DOUBLE, LONG = 1, 2, 3
KINDS = {SINGLE: "single", DOUBLE: "double", LONG: "long"}

TICK = 0.1  # top-button poll: a hold is reported on release, so this is the felt latency
GAP = 0.02  # minimum time between two bus accesses
TRIES = 3
RETRY_DELAY = 0.05
DEBOUNCE = {1: 0.5, 3: 2.0}  # per event: presses may come quickly, a hold takes seconds
OFFLINE_SECONDS = 10.0  # every read failed this long: the board stopped answering
RETRY_OFFLINE = 30.0
EVENT_LOG_MAX = 256  # lines; past it the log is rewritten keeping the newest EVENT_LOG_KEEP
EVENT_LOG_KEEP = 64
FLASH = 0.15  # challenge: one flash per press seen

# The Radxa board LED; openlolo-board.sh turns its heartbeat off and makes it group-writable.
STATUS_LED = "/sys/class/leds/green:heartbeat/brightness"

# Indicator word -> (LED frames, seconds per frame).
PATTERNS = {
    "setup": ([True, False], 0.25),  # setup mode, own Wi-Fi on air: fast blink, 2 Hz
    "searching": ([True, False], 0.5),  # looking for the phone: slow blink, 1 Hz
    "connected": ([True], 1.0),  # phone connected: steady on
    "challenge": ([False], 1.0),  # reset challenge: off; each press seen flashes (see show())
    "armed": ([True, False, True, False, True] + [False] * 8, FLASH),  # triple flash, 2 s
    "resetting": ([True], 1.0),
    "shutdown": ([True, False], 0.125),  # auto sleep warning: fast flicker, 4 Hz; then steady
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


class StatusLed:
    """One sysfs LED (``.../brightness``); writes only on change, logs a failure once."""

    def __init__(self, path: Path):
        self.path = path
        self.on: bool | None = None
        self.failed = False

    def set(self, on: bool) -> None:
        if on == self.on:
            return
        try:
            self.path.write_text("1" if on else "0")
        except OSError as exc:
            if not self.failed:
                log(f"status LED not written ({exc}); LED feedback off", 4)
                self.failed = True
            self.on = None
            return
        self.on = on
        self.failed = False


def touch(flag: Path) -> None:
    flag.touch()
    os.utime(flag, None)


class EventLog:
    """Append-only button event log for the API's reset challenge, one per daemon process.

    Each line is written in one ``write`` call, so a reader sees whole lines; the rare
    rewrite past EVENT_LOG_MAX lines replaces the file atomically. A failure is logged once and
    the button keeps working through the flags."""

    def __init__(self, path: Path, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.session = secrets.token_hex(6)
        self.seq = 0
        self.lines: list[str] = []
        self.failed = False
        self._replace([f"0 start {self.session}"])

    def append(self, kind: str) -> None:
        self.seq += 1
        line = f"{self.seq} {kind} {self.clock():.3f}"
        if len(self.lines) >= EVENT_LOG_MAX:
            self._replace([self.lines[0], *self.lines[-(EVENT_LOG_KEEP - 1) :], line])
            return
        self.lines.append(line)
        try:
            with self.path.open("a") as log_file:
                log_file.write(line + "\n")
        except OSError as exc:
            self._failed(exc)

    def _replace(self, lines: list[str]) -> None:
        self.lines = lines
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text("".join(line + "\n" for line in lines))
            temporary.replace(self.path)
        except OSError as exc:
            self._failed(exc)

    def _failed(self, exc: OSError) -> None:
        if not self.failed:
            log(f"button event log not written ({exc}); reset from the page unavailable", 4)
            self.failed = True


class Daemon:
    def __init__(
        self,
        bus: Bus,
        *,
        setup_flag: Path,
        pair_flag: Path,
        indicator: Path,
        led: StatusLed,
        events: EventLog | None = None,
        clock=time.monotonic,
    ):
        self.bus, self.led, self.clock = bus, led, clock
        self.events = events
        self.flash_start = self.flash_until = 0.0  # challenge: presses seen, flashed in turn
        self.setup_flag, self.pair_flag, self.indicator = setup_flag, pair_flag, indicator
        self.firmware = None
        self.last_event: dict[int, float] = {}
        self.failed_since: float | None = None
        self.indicator_stamp = None
        self.indicator_word = "idle"
        self.pattern = "idle"
        self.pattern_start = 0.0
        # Auto sleep (see the module docstring).
        self.id = secrets.token_hex(6)
        self.cut_delay = CUT_DELAY
        self.attempt: str | None = None
        self.attempt_until = 0.0
        self.committed = False
        self.power_cache: tuple[float, dict] | None = None

    # Start-up --------------------------------------------------------------------------------

    def start(self) -> None:
        """Raises I2CError when the board does not answer after the retries."""
        # A stale event is latched at boot (the power-on press); clear it without acting on it.
        tap = self.bus.read(TAP)
        if tap & 0x03:
            self.bus.write(TAP, tap & 0xFC, 0x03)
        self.firmware = self.read_firmware()
        self.failed_since = None
        self.indicator_stamp = None
        self.pattern = "idle"
        self.led.set(False)
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
        return self.failed_since is not None and self.clock() - self.failed_since >= OFFLINE_SECONDS

    def tick(self) -> None:
        now = self.clock()
        try:
            tap = self.bus.read(TAP)
        except I2CError:
            tap = None
        if tap is not None:
            self.failed_since = None
        elif self.failed_since is None:
            self.failed_since = now
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
        # Flag first, log second: a reader that sees the log line also sees the flag, so the
        # API's reset challenge can consume the flag of a press it counted.
        self.flash(event, self.clock())
        if event == DOUBLE:
            self.log_event(event)
            return
        name = "press" if event == SINGLE else "hold"
        last = self.last_event.get(event)
        if last is not None and now - last < DEBOUNCE[event]:
            log(f"top button {name} dropped: {now - last:.1f} s after the previous one")
        else:
            self.last_event[event] = now
            flag = self.pair_flag if event == SINGLE else self.setup_flag
            touch(flag)
            log(f"top button {name}; flag {flag} touched")
        self.log_event(event)

    def log_event(self, event: int) -> None:
        if self.events is not None:
            self.events.append(KINDS[event])

    def flash(self, event: int, now: float) -> None:
        """Challenge feedback: one short flash per press seen, two for a double."""
        if event not in (SINGLE, DOUBLE) or self.read_indicator() != "challenge":
            return
        length = 2 * FLASH * (2 if event == DOUBLE else 1)
        if now >= self.flash_until:
            self.flash_start, self.flash_until = now, now + length
        else:
            self.flash_until += length  # Queued after the flash still running.

    # Status LED ------------------------------------------------------------------------------

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
        now = self.clock()
        if self.attempt is not None and now >= self.attempt_until:
            log("auto sleep warning expired")
            self.attempt = None
        if self.committed:
            pattern = "resetting"  # steady on until the power goes
        elif self.attempt is not None:
            pattern = "shutdown"
        if pattern != self.pattern:
            self.pattern, self.pattern_start = pattern, now
            log(f"status LED: {pattern}")
        if pattern not in PATTERNS:
            self.led.set(False)
            return
        if pattern == "challenge" and now < self.flash_until:
            self.led.set(int((now - self.flash_start) / FLASH) % 2 == 0)
            return
        frames, seconds = PATTERNS[pattern]
        self.led.set(frames[int((now - self.pattern_start) / seconds) % len(frames)])

    # Auto sleep ------------------------------------------------------------------------------

    def power_status(self) -> dict:
        now = self.clock()
        if self.power_cache is None or now - self.power_cache[0] >= STATUS_CACHE:
            power = self.bus.read(POWER)
            try:
                percent = self.bus.read(BATTERY)
            except I2CError:
                percent = None
            reading = {
                "external_power": bool(power & EXTERNAL_BIT),
                "battery_percent": percent if percent is not None and 0 <= percent <= 100 else None,
            }
            self.power_cache = (now, reading)
        return {
            "pisugar": True,
            "daemon_id": self.id,
            "attempt": self.attempt,
            "committed": self.committed,
            **self.power_cache[1],
        }

    def power_request(self, request: dict) -> dict:
        op = request.get("op")
        try:
            if op == "status":
                return self.power_status()
            if self.committed:
                return {"ok": False, "error": "COMMITTED"}
            if op == "prepare":
                return self.prepare(request)
            if op == "cancel":
                if request.get("attempt") == self.attempt and self.attempt is not None:
                    self.attempt = None
                    log("auto sleep warning cancelled")
                return {"ok": True}
            if op == "commit":
                return self.commit(request)
        except I2CError as exc:
            return {"ok": False, "error": "I2C_FAILED", "message": str(exc)}
        return {"ok": False, "error": "UNKNOWN_OPERATION"}

    def prepare(self, request: dict) -> dict:
        attempt, seconds = request.get("attempt"), request.get("seconds")
        if not isinstance(attempt, str) or not attempt or not isinstance(seconds, (int, float)):
            return {"ok": False, "error": "INVALID_REQUEST"}
        if not 0 < seconds <= 60:
            return {"ok": False, "error": "INVALID_REQUEST"}
        if self.attempt is not None and self.attempt != attempt:
            return {"ok": False, "error": "BUSY"}
        self.attempt, self.attempt_until = attempt, self.clock() + seconds + PREPARE_GRACE
        log(f"auto sleep warning: power off in {seconds:.0f} s unless cancelled")
        return {"ok": True, "daemon_id": self.id}

    def commit(self, request: dict) -> dict:
        if request.get("daemon_id") != self.id:
            return {"ok": False, "error": "WRONG_DAEMON"}
        if (
            self.attempt is None
            or request.get("attempt") != self.attempt
            or self.clock() >= self.attempt_until
        ):
            return {"ok": False, "error": "STALE_ATTEMPT"}
        self.attempt = None
        if self.bus.read(POWER) & EXTERNAL_BIT:
            self.power_cache = None
            return {"ok": False, "error": "EXTERNAL_POWER"}
        try:
            self.bus.write(CUT_DELAY_REG, self.cut_delay)
            soft = self.bus.read(SOFT)
            self.bus.write(SOFT, soft & ~SOFT_BIT & 0xFF, SOFT_BIT)
            power = self.bus.read(POWER)
            self.bus.write(POWER, power & ~OUTPUT_BIT & 0xFF, OUTPUT_BIT)
        except I2CError as exc:
            self.restore_output()
            log(f"auto sleep: power cut not armed ({exc})", 3)
            return {"ok": False, "error": "I2C_FAILED", "message": str(exc)}
        self.committed = True
        log(f"auto sleep: power cut armed, output off in {self.cut_delay} s", 4)
        return {"ok": True, "cut_delay": self.cut_delay}

    def restore_output(self) -> None:
        try:
            power = self.bus.read(POWER)
            if not power & OUTPUT_BIT:
                self.bus.write(POWER, power | OUTPUT_BIT, OUTPUT_BIT)
        except I2CError as exc:
            log(f"auto sleep: output switch not restored ({exc}); the box may turn off", 3)

    def lost(self) -> None:
        """The board stopped answering: presses may be missed, so a reset challenge must fail."""
        if self.events is not None:
            self.events.append("lost")


OFFLINE_REPLY = {"pisugar": False}


def offline_request(request: dict) -> dict:
    """The power socket while the board does not answer: no PiSugar, so no auto sleep."""
    if request.get("op") == "status":
        return dict(OFFLINE_REPLY)
    return {"ok": False, "error": "PISUGAR_OFFLINE"}


class PowerServer:
    """``power.sock``: polled once per tick, one JSON line in and one out per connection."""

    def __init__(self, path: Path):
        self.path = path
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.sock.bind(str(path))
        finally:
            os.umask(old)
        self.sock.listen(4)
        self.sock.setblocking(False)

    def poll(self, handler) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            with conn:
                try:
                    conn.settimeout(0.5)
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 4096:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data or b"{}")
                    reply = handler(request if isinstance(request, dict) else {})
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except (OSError, ValueError) as exc:
                    log(f"power socket request dropped ({type(exc).__name__})", 4)

    def close(self) -> None:
        self.sock.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def serve(open_bus, daemon_factory, stop, clock=time.monotonic, sleep=time.sleep, power=None) -> None:
    """Run until ``stop()``; a missing or silent board is logged once and retried every 30 s."""
    reported = False

    def pause(seconds: float) -> None:
        end = clock() + seconds
        while not stop() and clock() < end:
            if power is not None:
                power.poll(offline_request)
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
            if power is not None:
                power.poll(daemon.power_request)
            rest = max(0.0, TICK - (clock() - started))
            end = clock() + rest
            while not stop() and clock() < end:
                sleep(min(TICK, end - clock()))
        if daemon.offline:
            if daemon.committed:
                return  # The power is going: nothing left to do.
            log(f"PiSugar3 stopped answering; retrying every {RETRY_OFFLINE:.0f} s", 4)
            daemon.lost()
            reported = True
            pause(RETRY_OFFLINE)


def main(stop) -> int:
    try:
        from smbus2 import SMBus
    except ImportError:
        print("python3-smbus2 is not installed; PiSugar3 top button disabled", file=sys.stderr)
        return 0
    bus_number = int(os.environ.get("OPENLOLO_I2C_BUS", "3"))
    led = StatusLed(Path(os.environ.get("OPENLOLO_STATUS_LED", STATUS_LED)))
    run_dir = Path(os.environ.get("OPENLOLO_RUN_DIR", "/run/openlolo"))
    events = EventLog(run_dir / "button-events")
    cut_delay = int(os.environ.get("OPENLOLO_CUT_DELAY", str(CUT_DELAY)))
    try:
        power = PowerServer(run_dir / "power.sock")
    except OSError as exc:
        log(f"power socket not opened ({exc}); auto sleep off", 4)
        power = None
    opened = []

    def open_bus():
        while opened:
            opened.pop().close()
        smbus = SMBus(bus_number)
        opened.append(smbus)
        return Bus(smbus)

    def factory(bus):
        daemon = Daemon(
            bus,
            setup_flag=Path(os.environ.get("OPENLOLO_BUTTON_FLAG", run_dir / "setup-button")),
            pair_flag=Path(os.environ.get("OPENLOLO_PAIR_FLAG", run_dir / "pair-button")),
            indicator=run_dir / "indicator",
            led=led,
            events=events,
        )
        daemon.cut_delay = max(10, min(255, cut_delay))
        return daemon

    try:
        serve(open_bus, factory, stop, power=power)
    finally:
        if power is not None:
            power.close()
        led.set(False)
        while opened:
            opened.pop().close()
    return 0
