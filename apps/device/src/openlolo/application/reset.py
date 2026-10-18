"""Factory reset from the box page, proven at the box with the top button (Lolo Pocket).

Any browser that can load the page may start a reset; only someone at the box can finish it:

    start --> challenge: press the top button until the count reaches ``target`` (5 to 8, random)
    challenge --(exactly target, then QUIET_SECONDS without a press)--> armed
    armed --(a hold within ARMED_SECONDS)--> resetting (the reset unit runs; it stops the API)

A challenge fails on more presses than the target, a hold, CHALLENGE_SECONDS without arming, a
page cancel, or a lost event. An armed reset is cancelled by a press, a page cancel,
ARMED_SECONDS without a hold, or a lost event. Nothing survives an API restart.

A stranger on the home Wi-Fi can open a challenge but cannot press a button they cannot touch,
so nothing is armed and the owner's next hold stays an ordinary hold. The target is shown only
to the browser that started the attempt (it knows the attempt id).

Presses are read from the button daemon's append-only event log, not the flag files: the flags
merge quick presses into one mtime, and counting needs every press in order. A double press
counts 2 (the firmware reports two quick taps as one double, bench 2026-10-18). Each log line is
``<seq> <kind> <monotonic time>``; the first line is ``0 start <session>`` and a new session
means the daemon restarted. A gap in sequence numbers, a new session, or a ``lost`` line (the
board stopped answering) fails an open attempt: a press the box did not see must never let a
wrong count arm, and a hold it did not see must never be replayed into an erase.

Boxes without the log (Raspberry Pi: a GPIO hold button or none) keep the owner's typed-name
reset; ``available`` says which one the page shows.
"""

import logging
import secrets
import time
from pathlib import Path

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger("openlolo.reset")

TARGETS = (5, 6, 7, 8)
CHALLENGE_SECONDS = 30
ARMED_SECONDS = 30
QUIET_SECONDS = 2  # A single arrives ~1 s after its tap, a double ~0.6 s after its pair.
OUTCOME_SECONDS = 60  # How long a finished attempt's result stays readable for the page.
PRESS_VALUE = {"single": 1, "double": 2}


class ButtonEvents:
    """Reads the daemon's event log; re-reads only when the file changed."""

    def __init__(self, path: Path):
        self.path = path
        self.stamp: tuple[int, int] | None = None
        self.cached: tuple[str, list[tuple[int, str]]] | None = None

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> tuple[str, list[tuple[int, str]]] | None:
        """``(session, [(seq, kind), ...])``, or None when the log is missing or unreadable."""
        try:
            stat = self.path.stat()
        except OSError:
            self.stamp = self.cached = None
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.stamp:
            return self.cached
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return None
        session, events = None, []
        for line in lines:
            parts = line.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            if parts[1] == "start" and len(parts) >= 3:
                session = parts[2]
            else:
                events.append((int(parts[0]), parts[1]))
        if session is None:
            return None
        self.stamp, self.cached = stamp, (session, events)
        return self.cached


class ResetChallenge:
    def __init__(self, events: Path, *, runner, busy=lambda: False, clock=time.monotonic, choose=None):
        self.events = ButtonEvents(events)
        # async () -> bool: starts the factory reset; True when it finished in process (the
        # simulator), False when the reset unit took over and will stop this service.
        self.runner = runner
        self.busy = busy  # () -> whether a Wi-Fi join is running
        self.clock = clock
        self.choose = choose or secrets.choice
        self.stage: str | None = None  # challenge | armed | resetting
        self.attempt: str | None = None
        self.target = 0
        self.count = 0
        self.deadline = 0.0
        self.counted_at = 0.0
        self.session: str | None = None
        self.seq = 0
        self.outcome: dict | None = None
        self.outcome_until = 0.0

    # -- observation -------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """The box has a top button that reports presses (the daemon's event log exists)."""
        return self.events.exists()

    @property
    def open(self) -> bool:
        return self.stage is not None

    @property
    def indicator(self) -> str | None:
        """The status LED word while an attempt is open."""
        return self.stage

    def view(self, attempt: str | None = None) -> dict | None:
        """Attempt state for ``/setup/status``; the target only for the attempt's own browser."""
        now = self.clock()
        if self.stage is not None:
            state = {
                "stage": self.stage,
                "attempt": self.attempt,
                "count": self.count,
                "expires_in": max(0, round(self.deadline - now)) if self.stage != "resetting" else None,
            }
            if attempt and attempt == self.attempt:
                state["target"] = self.target
            return state
        if self.outcome is not None and now < self.outcome_until:
            return dict(self.outcome)
        return None

    # -- page actions ------------------------------------------------------------------

    def start(self, *, takeover: bool = False) -> dict:
        """Open a challenge. Another browser's open attempt is refused unless ``takeover`` (the
        owner's signed-in page), so a stranger cannot cut into the owner's count."""
        if self.stage == "resetting":
            raise OpenLoloError("RESET_RUNNING", "The box is resetting", 409)
        if self.busy():
            raise OpenLoloError(
                "PROVISIONING_BUSY", "The box is joining a Wi-Fi network; try again shortly", 409
            )
        if self.stage is not None and not takeover:
            raise OpenLoloError(
                "RESET_IN_PROGRESS", "A reset is already waiting for the button; try again shortly", 409
            )
        snapshot = self.events.read()
        if snapshot is None:
            raise OpenLoloError("RESET_BUTTON_UNAVAILABLE", "The box's top button is not available", 409)
        if self.stage is not None:
            log.info("reset attempt %s replaced by the owner's page", self.attempt)
        self.session, events = snapshot
        self.seq = events[-1][0] if events else 0  # Only presses after this one count.
        self.stage = "challenge"
        self.attempt = secrets.token_urlsafe(12)
        self.target = self.choose(TARGETS)
        self.count = 0
        self.counted_at = 0.0
        self.deadline = self.clock() + CHALLENGE_SECONDS
        self.outcome = None
        log.info("reset challenge opened: %d presses", self.target)
        return {"attempt": self.attempt, "target": self.target, "expires_in": CHALLENGE_SECONDS}

    def cancel(self, attempt: str | None, *, owner: bool = False) -> bool:
        """Close the open attempt from the page: its own browser, or the owner's page."""
        if self.stage not in {"challenge", "armed"}:
            return False
        if not owner and attempt != self.attempt:
            return False
        self._close("cancelled" if self.stage == "armed" else "failed", "page")
        return True

    # -- supervisor --------------------------------------------------------------------

    async def poll(self) -> bool:
        """One step (the provisioning loop, every 0.2 s). Returns whether the button belongs to
        this attempt right now: open before or after the step, so a press or hold read in the
        same tick never also opens Local mode or starts a search."""
        if self.stage not in {"challenge", "armed"}:
            return self.stage is not None
        snapshot = self.events.read()
        if snapshot is None or snapshot[0] != self.session:
            self._close("failed" if self.stage == "challenge" else "cancelled", "lost")
            return True
        for seq, kind in snapshot[1]:
            if seq <= self.seq:
                continue
            if seq != self.seq + 1:
                self._close("failed" if self.stage == "challenge" else "cancelled", "lost")
                return True
            self.seq = seq
            await self._event(kind)
            if self.stage not in {"challenge", "armed"}:
                return True
        now = self.clock()
        if self.stage == "challenge" and self.count == self.target and now - self.counted_at >= QUIET_SECONDS:
            self.stage = "armed"
            self.deadline = now + ARMED_SECONDS
            log.info("reset armed: hold the top button within %d s to erase", ARMED_SECONDS)
        elif now >= self.deadline:
            self._close("failed" if self.stage == "challenge" else "cancelled", "timeout")
        return True

    async def _event(self, kind: str) -> None:
        if self.stage == "challenge":
            if kind in PRESS_VALUE:
                self.count += PRESS_VALUE[kind]
                self.counted_at = self.clock()
                if self.count > self.target:
                    self._close("failed", "too_many")
            elif kind == "long":
                self._close("failed", "hold")
            else:
                self._close("failed", "lost")
            return
        if kind == "long":
            await self._confirm()
        elif kind in PRESS_VALUE:
            self._close("cancelled", "press")
        else:
            self._close("cancelled", "lost")

    async def _confirm(self) -> None:
        self.stage = "resetting"
        log.warning("factory reset confirmed by a hold of the top button")
        try:
            finished = await self.runner()
        except Exception as exc:
            log.error("factory reset did not start: %s", getattr(exc, "message", exc))
            self._close("failed", "reset_failed")
            return
        if finished:
            self._close("done", "reset")

    def _close(self, stage: str, reason: str) -> None:
        log.info("reset attempt %s: %s (%s)", self.attempt, stage, reason)
        self.outcome = {"stage": stage, "attempt": self.attempt, "reason": reason}
        self.outcome_until = self.clock() + OUTCOME_SECONDS
        self.stage = None
        self.attempt = None
        self.target = self.count = 0
