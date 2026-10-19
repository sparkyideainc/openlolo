"""Reset from the box page, finished with the top button: challenge, arm, confirm, guards."""

import pytest

from openlolo.application import reset as reset_module
from openlolo.application.reset import ResetChallenge
from openlolo.domain.errors import OpenLoloError


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Log:
    """The button daemon's event log, written the way the daemon writes it."""

    def __init__(self, path, session="aaaa"):
        self.path, self.seq = path, 0
        path.write_text(f"0 start {session}\n")

    def add(self, *kinds):
        with self.path.open("a") as f:
            for kind in kinds:
                self.seq += 1
                f.write(f"{self.seq} {kind} 1.000\n")

    def restart(self, session="bbbb"):
        self.seq = 0
        self.path.write_text(f"0 start {session}\n")


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    log = Log(tmp_path / "button-events")
    runs = []
    state = {"busy": False}

    async def runner():
        runs.append(clock.now)
        return False

    challenge = ResetChallenge(
        log.path, runner=runner, busy=lambda: state["busy"], clock=clock, choose=lambda targets: 6
    )
    return challenge, log, clock, runs, state


async def arm(challenge, log, clock, *kinds):
    log.add(*kinds)
    await challenge.poll()
    clock.now += reset_module.QUIET_SECONDS
    await challenge.poll()


async def test_exact_count_arms_and_a_hold_resets(rig):
    challenge, log, clock, runs, _ = rig
    log.add("single")  # Before the challenge: never counted.
    started = challenge.start()
    assert started["target"] == 6 and started["expires_in"] == reset_module.CHALLENGE_SECONDS
    assert challenge.indicator == "challenge"
    log.add("single", "double", "single")
    await challenge.poll()
    assert challenge.view()["count"] == 4
    log.add("double")
    await challenge.poll()
    assert challenge.stage == "challenge"  # Waits for the quiet gap: another press may follow.
    clock.now += reset_module.QUIET_SECONDS
    await challenge.poll()
    assert challenge.stage == "armed" and challenge.indicator == "armed"
    log.add("long")
    await challenge.poll()
    assert challenge.stage == "resetting" and runs == [clock.now]


async def test_one_press_too_many_fails(rig):
    challenge, log, clock, runs, _ = rig
    challenge.start()
    log.add("double", "double", "double")  # 6
    await challenge.poll()
    log.add("single")  # 7 arrives inside the quiet gap
    await challenge.poll()
    assert challenge.stage is None and challenge.view()["reason"] == "too_many"
    clock.now += 10
    await challenge.poll()
    assert runs == []


async def test_a_hold_during_the_challenge_fails_it(rig):
    challenge, log, _, runs, _ = rig
    challenge.start()
    log.add("single", "long")
    assert await challenge.poll() is True  # The hold belongs to the attempt: no Local mode
    assert challenge.stage is None and challenge.view()["reason"] == "hold" and runs == []


async def test_challenge_times_out(rig):
    challenge, log, clock, _, _ = rig
    challenge.start()
    log.add("double")
    clock.now += reset_module.CHALLENGE_SECONDS
    await challenge.poll()
    assert challenge.stage is None and challenge.view()["reason"] == "timeout"


async def test_armed_is_cancelled_by_a_press_a_timeout_or_the_page(rig):
    challenge, log, clock, runs, _ = rig
    challenge.start()
    await arm(challenge, log, clock, "double", "double", "double")
    log.add("single")
    await challenge.poll()
    assert challenge.view() == {
        "stage": "cancelled",
        "attempt": challenge.outcome["attempt"],
        "reason": "press",
    }

    challenge.start()
    await arm(challenge, log, clock, "double", "double", "double")
    clock.now += reset_module.ARMED_SECONDS
    await challenge.poll()
    assert challenge.view()["reason"] == "timeout"

    attempt = challenge.start()["attempt"]
    await arm(challenge, log, clock, "double", "double", "double")
    assert challenge.cancel("someone-else") is False
    assert challenge.cancel(attempt) is True
    assert challenge.view()["reason"] == "page"
    log.add("long")  # A hold after the attempt closed is an ordinary hold.
    assert await challenge.poll() is False
    assert runs == []


async def test_a_missed_event_or_daemon_restart_fails_the_attempt(rig):
    challenge, log, clock, runs, _ = rig
    challenge.start()
    log.add("single")
    log.seq += 1  # A line the box never saw
    log.add("single")
    await challenge.poll()
    assert challenge.view()["reason"] == "lost"

    challenge.start()
    await arm(challenge, log, clock, "double", "double", "double")
    log.restart()
    log.add("long")  # Never replayed into an erase.
    await challenge.poll()
    assert challenge.view() == {
        "stage": "cancelled",
        "attempt": challenge.outcome["attempt"],
        "reason": "lost",
    }

    challenge.start()
    log.add("single", "lost")  # The board stopped answering.
    await challenge.poll()
    assert challenge.view()["reason"] == "lost" and runs == []


async def test_target_is_shown_only_to_the_starting_browser(rig):
    challenge, *_ = rig
    attempt = challenge.start()["attempt"]
    assert "target" not in challenge.view()
    assert "target" not in challenge.view("guess")
    assert challenge.view(attempt)["target"] == 6


async def test_one_attempt_at_a_time_and_the_owner_takes_over(rig):
    challenge, log, clock, _, state = rig
    first = challenge.start()
    with pytest.raises(OpenLoloError) as refused:
        challenge.start()
    assert refused.value.code == "RESET_IN_PROGRESS"
    second = challenge.start(takeover=True)
    assert second["attempt"] != first["attempt"] and challenge.view(second["attempt"])["count"] == 0
    assert challenge.cancel(None, owner=True) is True
    state["busy"] = True
    with pytest.raises(OpenLoloError) as busy:
        challenge.start()
    assert busy.value.code == "PROVISIONING_BUSY"


async def test_no_event_log_means_no_button_reset(tmp_path):
    async def runner():
        return False

    challenge = ResetChallenge(tmp_path / "missing", runner=runner)
    assert challenge.available is False
    with pytest.raises(OpenLoloError) as refused:
        challenge.start()
    assert refused.value.code == "RESET_BUTTON_UNAVAILABLE"


async def test_a_reset_that_does_not_start_is_reported(rig):
    challenge, log, clock, _, _ = rig

    async def broken():
        raise OpenLoloError("RESET_FAILED", "nope", 502)

    challenge.runner = broken
    challenge.start()
    await arm(challenge, log, clock, "double", "double", "double")
    log.add("long")
    await challenge.poll()
    assert challenge.stage is None and challenge.view()["reason"] == "reset_failed"


async def test_outcome_expires(rig):
    challenge, _, clock, _, _ = rig
    attempt = challenge.start()["attempt"]
    challenge.cancel(attempt)
    assert challenge.view()["stage"] == "failed"
    clock.now += reset_module.OUTCOME_SECONDS
    assert challenge.view() is None
