"""Auto sleep: turn a battery-powered box off when no phone is connected and nothing uses it.

The owner picks the idle time on the box page (Off, 5, 15, 30 or 60 minutes; ``power.json`` in
the state directory, ``auto_off_seconds`` until set). The box counts only while all of these
hold: the PiSugar3 answers and reports no external power, the worker answered within the last
tick and reports no phone session (not connected, connecting, switching or mounting), no search
window is open, no control lease is live and no action is queued or running, Local or Setup mode
is off, no Wi-Fi join or reset is in progress, and no maintenance inhibitor is held. Any of them
failing restarts the count from zero.

Activity restarts the count too: an accepted AI or SDK request (the coordinator), an owner
action on the box page (any POST), a button press or hold, a change of the setting. Page status
polls are not activity.

At the deadline the button daemon (the only process on I2C) shows a warning on the status LED
for ``auto_off_warning_seconds``; activity or a failed check cancels it. Then, after one last
check, the coordinator refuses new work, the daemon arms the PiSugar power cut (with a delay
long enough for a clean shutdown) and the API starts ``openlolo-poweroff.service``. Once armed
the cut always happens. The power button or external power turns the box back on.

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

CHOICES = (0, 5, 15, 30, 60)  # Minutes; 0 = Off.
TICK_SECONDS = 2.0
POWER_TIMEOUT = 3.0
POWEROFF_UNIT = "openlolo-poweroff.service"  # Root unit: systemctl poweroff.
# Phone session states that keep the box on: the phone is in use or being reached.
PHONE_BUSY = {"connected", "connecting", "switching", "mounting_ddi", "reconnecting"}


class PowerClient:
    """The button daemon's power socket. Every call opens one connection; errors return None."""

    def __init__(self, path: Path, timeout: float = POWER_TIMEOUT):
        self.path, self.timeout = path, timeout

    async def call(self, request: dict) -> dict | None:
        if not self.path.exists():
            return None
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
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


class Settings:
    """The owner's choice in ``power.json``; a missing or invalid file falls back to config."""

    def __init__(self, path: Path, default_seconds: float):
        self.path = path
        self.default = min(CHOICES, key=lambda m: abs(m * 60 - default_seconds)) if default_seconds else 0

    def minutes(self) -> int:
        try:
            value = json.loads(self.path.read_text()).get("auto_sleep_minutes")
        except (OSError, ValueError, AttributeError):
            return self.default
        return value if isinstance(value, int) and value in CHOICES else self.default

    def set(self, minutes: int) -> None:
        if minutes not in CHOICES or isinstance(minutes, bool):
            raise OpenLoloError(
                "INVALID_SETTING", f"Choose one of {', '.join(map(str, CHOICES))} minutes", 400
            )
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"auto_sleep_minutes": minutes}, handle)
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

    def set_minutes(self, minutes) -> dict:
        self.settings.set(minutes)
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
        minutes = self.settings.minutes()
        result = {
            "auto_sleep_minutes": minutes,
            "choices": list(CHOICES),
            "supported": supported,
            "on_battery": supported and external is False,
            "battery_percent": power.get("battery_percent") if supported else None,
            "state": self.state,
            "seconds_left": None,
        }
        if self.state == "counting":
            result["seconds_left"] = max(0, int(self.idle_since + minutes * 60 - self.clock()))
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

    async def _eligible(self) -> tuple[bool, str]:
        """(may count, view state when not): one snapshot of every condition."""
        power = await self.power.call({"op": "status"})
        self.last_power = power
        if not power or not power.get("pisugar"):
            return False, "unsupported"
        self.daemon_id = power.get("daemon_id")
        if self.settings.minutes() == 0:
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
                    await self._cancel_warning()
                    self.idle_since, self.state = self.clock(), state
                    log.info("auto sleep cancelled: %s", state)
                elif now >= self.warn_until:
                    await self._commit()
                return
        ok, state = await self._eligible()
        if not ok:
            self.idle_since, self.state = self.clock(), state
            return
        if self.state != "counting":
            self.state = "counting"
            self.idle_since = min(self.idle_since, now)
        if now - self.idle_since < self.settings.minutes() * 60:
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
        reply = await self.power.call({"op": "commit", "attempt": attempt, "daemon_id": self.daemon_id})
        if not reply or not reply.get("ok"):
            self.coordinator.closing = False
            log.warning("auto sleep: power cut not armed (%s)", (reply or {}).get("error", "no reply"))
            await self._cancel_warning()
            return
        self.attempt = None
        self.committed = True
        self.state = "shutting_down"
        log.warning("auto sleep: power cut armed in %s s; shutting down", reply.get("cut_delay"))
        if self.poweroff is not None:
            try:
                await self.poweroff()
            except Exception:
                # The cut is armed and fires anyway; the filesystem may not be synced.
                log.exception("auto sleep: poweroff did not start; the power cut follows anyway")
