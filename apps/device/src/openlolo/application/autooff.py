"""Auto sleep: turn a battery-powered box off when no phone is connected and nothing uses it.

The owner picks the idle time on the box page (Off, 30 seconds, or 1 to 5 minutes, as on an
iPhone; ``power.json`` in the state directory, ``auto_off_seconds`` until set). The box counts only while all of these
hold: the PiSugar3 answers and reports no external power, the worker answered within the last
tick and reports no phone session (not connected, connecting, switching or mounting), no search
window is open, no control lease is live and no action is queued or running, Local or Setup mode
is off, no Wi-Fi join or reset is in progress, and no maintenance inhibitor is held. Any of them
failing restarts the count from zero, except a PiSugar3 that stops answering for less than
``POWER_GAP_SECONDS``: that keeps the count but holds off any warning until the board is back.

Activity restarts the count too: an accepted AI or SDK request (the coordinator), an owner
action on the box page (any POST), a button press or hold, a change of the setting. Page status
polls are not activity.

At the deadline the button daemon (the only process on I2C) shows a warning on the status LED
for ``auto_off_warning_seconds``; activity or a failed check cancels it. Then, after one last
check, the coordinator refuses new work, the daemon arms the PiSugar power cut (with a delay
long enough for a clean shutdown) and the API starts ``openlolo-poweroff.service``. Once armed
the cut always happens, so a commit whose result is unclear (no reply, or a write that may have
landed) also shuts down unless the daemon's status rules the cut out. The power button or
external power turns the box back on.

The daemon talks over ``power.sock`` in the runtime directory, one JSON line each way:
``status``, ``prepare``, ``cancel``, ``commit``. A box without the socket (a GPIO button, no
PiSugar) has no auto sleep.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger("openlolo.autooff")

CHOICES = (0, 30, 60, 120, 180, 240, 300)  # Seconds; 0 = Off. iPhone Auto-Lock's choices.
TICK_SECONDS = 2.0
POWER_TIMEOUT = 3.0
# A commit retries each I2C access on a flaky bus; it can take several seconds to answer.
COMMIT_TIMEOUT = 15.0
# The PiSugar3 drops off I2C for 30-60 s every few minutes on the Radxa bench box. A gap this
# short keeps the count (no warning while the board is silent); a longer one resets it.
POWER_GAP_SECONDS = 90.0
POWEROFF_UNIT = "openlolo-poweroff.service"  # Root unit: systemctl poweroff.
# Phone session states that keep the box on: the phone is in use or being reached.
PHONE_BUSY = {"connected", "connecting", "switching", "mounting_ddi", "reconnecting"}


class PowerClient:
    """The button daemon's power socket. Every call opens one connection; errors return None."""

    def __init__(self, path: Path, timeout: float = POWER_TIMEOUT):
        self.path, self.timeout = path, timeout

    async def call(self, request: dict, timeout: float | None = None) -> dict | None:
        if not self.path.exists():
            return None
        writer = None
        try:
            async with asyncio.timeout(timeout or self.timeout):
                reader, writer = await asyncio.open_unix_connection(str(self.path))
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
            reply = json.loads(line)
            return reply if isinstance(reply, dict) else None
        except (OSError, TimeoutError, ValueError):
            return None
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


def nearest(seconds: float) -> int:
    """The closest choice other than Off."""
    return min(CHOICES[1:], key=lambda choice: abs(choice - seconds))


class Settings:
    """The owner's choice in ``power.json``; a missing or invalid file falls back to config."""

    def __init__(self, path: Path, default_seconds: float):
        self.path = path
        self.default = nearest(default_seconds) if default_seconds else 0

    def seconds(self) -> int:
        try:
            saved = json.loads(self.path.read_text())
            value = saved.get("auto_sleep_seconds")
            if value is None:
                # Before the iPhone choices: whole minutes (5, 15, 30, 60). Snap to the nearest.
                minutes = saved.get("auto_sleep_minutes")
                if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0:
                    value = nearest(minutes * 60) if minutes else 0
        except (OSError, ValueError, AttributeError):
            return self.default
        ok = isinstance(value, int) and not isinstance(value, bool) and value in CHOICES
        return value if ok else self.default

    def set(self, seconds: int) -> None:
        if seconds not in CHOICES or isinstance(seconds, bool):
            raise OpenLoloError(
                "INVALID_SETTING", f"Choose one of {', '.join(map(str, CHOICES))} seconds", 400
            )
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"auto_sleep_seconds": seconds}, handle)
            handle.write("\n")
        temporary.replace(self.path)


def inhibitor_alive(path: Path) -> bool:
    """A maintenance script (update, reset) holds ``inhibit-sleep`` with its PID while it runs."""
    try:
        pid = int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AutoOff:
    def __init__(
        self,
        config,
        *,
        device,
        coordinator,
        provisioning=None,
        reset=None,
        power=None,
        poweroff=None,
        clock=time.monotonic,
    ):
        self.config = config
        self.device, self.coordinator = device, coordinator
        self.provisioning, self.reset = provisioning, reset
        self.power = power or PowerClient(config.runtime_dir / "power.sock")
        self.poweroff = poweroff
        self.clock = clock
        self.settings = Settings(config.state_dir / "power.json", config.auto_off_seconds)
        self.inhibitor = config.runtime_dir / "inhibit-sleep"
        self.idle_since = clock()
        self.generation = 0  # Bumped by every activity; a warning checks it before the cut.
        self.attempt: str | None = None
        self.attempt_generation = -1
        self.warn_until = 0.0
        self.daemon_id: str | None = None
        self.state = "off"
        self.last_power: dict | None = None
        self.power_seen: float | None = None  # Last status with the board answering.
        self.committed = False
        self.task: asyncio.Task | None = None
        self.woken = asyncio.Event()
        self.held_locally = 0  # In-process maintenance (``hold``) count.

    # ----- inputs --------------------------------------------------------------------

    def activity(self, reason: str = "") -> None:
        """Restart the idle count; cancel a warning at the next tick (woken at once)."""
        if self.committed:
            return
        self.generation += 1
        self.idle_since = self.clock()
        self.woken.set()

    @contextlib.contextmanager
    def hold(self):
        """Keep the box on while in-process maintenance runs (a factory reset, a join)."""
        self.held_locally += 1
        self.activity("hold")
        try:
            yield
        finally:
            self.held_locally -= 1
            self.activity("hold end")

    def set_seconds(self, seconds) -> dict:
        self.settings.set(seconds)
        self.activity("setting")
        return self.view()

    def keep_on(self) -> dict:
        self.activity("keep on")
        return self.view()

    # ----- view ----------------------------------------------------------------------

    def view(self) -> dict:
        power = self.last_power or {}
        supported = bool(power.get("pisugar"))
        external = power.get("external_power")
        seconds = self.settings.seconds()
        result = {
            "auto_sleep_seconds": seconds,
            "choices": list(CHOICES),
            "supported": supported,
            "on_battery": supported and external is False,
            "battery_percent": power.get("battery_percent") if supported else None,
            "state": self.state,
            "seconds_left": None,
        }
        if self.state == "counting":
            result["seconds_left"] = max(0, int(self.idle_since + seconds - self.clock()))
        elif self.state == "warning":
            result["seconds_left"] = max(0, int(self.warn_until - self.clock() + 0.999))
        return result

    # ----- checks --------------------------------------------------------------------

    async def _phone_idle(self) -> bool:
        """True only when the worker answered and reports no session and no search."""
        try:
            status = await asyncio.wait_for(self.device.health(), POWER_TIMEOUT)
        except Exception:
            return False
        connection = (status or {}).get("connection") or {}
        state = connection.get("state")
        if not isinstance(state, str) or state in PHONE_BUSY:
            return False
        return not connection.get("searching")

    def _box_idle(self) -> bool:
        coordinator = self.coordinator
        if coordinator.live() or coordinator.current is not None or not coordinator.queue.empty():
            return False
        provisioning = self.provisioning
        if provisioning is not None:
            if provisioning.active or provisioning.state.value in {"joining", "verifying"}:
                return False
        if self.reset is not None and self.reset.stage is not None:
            return False
        return not (self.held_locally or inhibitor_alive(self.inhibitor))

    async def _eligible(self) -> tuple[bool, str | None]:
        """(may count, view state when not): one snapshot of every condition. A state of None
        means the board went silent a moment ago: keep the count, but do not warn or cut."""
        power = await self.power.call({"op": "status"})
        now = self.clock()
        if not power or not power.get("pisugar"):
            if self.power_seen is not None and now - self.power_seen < POWER_GAP_SECONDS:
                return False, None
            self.last_power, self.power_seen = power, None
            return False, "unsupported"
        self.last_power, self.power_seen = power, now
        self.daemon_id = power.get("daemon_id")
        if self.settings.seconds() == 0:
            return False, "off"
        if power.get("external_power") is not False:
            return False, "plugged_in"
        if not self._box_idle() or not await self._phone_idle():
            return False, "in_use"
        return True, "counting"

    # ----- loop ----------------------------------------------------------------------

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._cancel_warning()

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("auto sleep check failed")
            if self.committed:
                return
            self.woken.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.woken.wait(), TICK_SECONDS)

    async def step(self) -> None:
        """One check: count, warn, cancel or turn off."""
        now = self.clock()
        if self.attempt is not None:
            if self.generation != self.attempt_generation:
                await self._cancel_warning()
                log.info("auto sleep cancelled by activity")
            else:
                ok, state = await self._eligible()
                if not ok:
                    idle_since = self.idle_since
                    await self._cancel_warning()
                    if state is None:  # Board silent: warn again once it answers.
                        self.idle_since = idle_since
                        log.info("auto sleep warning stopped: PiSugar3 not answering")
                    else:
                        self.idle_since, self.state = self.clock(), state
                        log.info("auto sleep cancelled: %s", state)
                elif now >= self.warn_until:
                    await self._commit()
                return
        ok, state = await self._eligible()
        if state is None:
            return
        if not ok:
            self.idle_since, self.state = self.clock(), state
            return
        if self.state != "counting":
            self.state = "counting"
            self.idle_since = min(self.idle_since, now)
        if now - self.idle_since < self.settings.seconds():
            return
        await self._prepare()

    async def _prepare(self) -> None:
        attempt = secrets.token_hex(8)
        seconds = self.config.auto_off_warning_seconds
        reply = await self.power.call({"op": "prepare", "attempt": attempt, "seconds": seconds})
        if not reply or not reply.get("ok"):
            log.warning("auto sleep warning not shown: %s", (reply or {}).get("error", "no reply"))
            self.idle_since = self.clock()
            return
        self.attempt, self.attempt_generation = attempt, self.generation
        self.daemon_id = reply.get("daemon_id", self.daemon_id)
        self.warn_until = self.clock() + seconds
        self.state = "warning"
        log.info("auto sleep: turning off in %d s unless the box is used", seconds)

    async def _maybe_armed(self, reply: dict | None) -> bool:
        """After a failed or lost commit reply: could the PiSugar3 cut still be counting down?"""
        if reply is not None and not reply.get("maybe_armed"):
            return False  # The daemon answered: the output switch was never cleared.
        status = await self.power.call({"op": "status"}, timeout=COMMIT_TIMEOUT)
        if status and status.get("pisugar") and status.get("daemon_id") == self.daemon_id:
            return bool(status.get("committed"))
        return True  # Unknown: a clean shutdown is safer than a cut while running.

    async def _cancel_warning(self) -> None:
        attempt, self.attempt = self.attempt, None
        if attempt is None:
            return
        self.state = "counting"
        self.idle_since = self.clock()
        await self.power.call({"op": "cancel", "attempt": attempt})

    async def _commit(self) -> None:
        """Last check passed: refuse new work, arm the cut, power off. Any failure before the cut
        is armed reopens the box and restarts the count."""
        attempt = self.attempt
        self.coordinator.closing = True
        if self.generation != self.attempt_generation:
            self.coordinator.closing = False
            await self._cancel_warning()
            return
        request = {"op": "commit", "attempt": attempt, "daemon_id": self.daemon_id}
        reply = await self.power.call(request, timeout=COMMIT_TIMEOUT)
        if reply and reply.get("ok"):
            log.warning("auto sleep: power cut armed in %s s; shutting down", reply.get("cut_delay"))
        elif await self._maybe_armed(reply):
            # A cut that may be counting down must not hit a running box: shut down cleanly.
            log.error(
                "auto sleep: power cut may be armed (%s); shutting down",
                (reply or {}).get("error", "no reply"),
            )
        else:
            self.coordinator.closing = False
            log.warning("auto sleep: power cut not armed (%s)", (reply or {}).get("error", "no reply"))
            await self._cancel_warning()
            return
        self.attempt = None
        self.committed = True
        self.state = "shutting_down"
        if self.poweroff is not None:
            try:
                await self.poweroff()
            except Exception:
                # The cut is armed and fires anyway; the filesystem may not be synced.
                log.exception("auto sleep: poweroff did not start; the power cut follows anyway")
