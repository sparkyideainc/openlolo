"""API side of the software update (application/update.py): checks, offers, refusals, attempts."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from openlolo.application import update as update_module
from openlolo.application.update import (
    BOOT_DELAY,
    COALESCE_SECONDS,
    FREE_BYTES,
    STALE_SECONDS,
    START_SECONDS,
    Updates,
    offered_version,
    parse_version,
)
from openlolo.domain.errors import OpenLoloError

REPO = "sparkyideainc/openlolo"


def release(version="0.0.3", *, repo=REPO, **changes):
    prefix = f"https://github.com/{repo}/releases/download/v{version}/"
    name = f"openlolo-device-{version}.tar.gz"
    data = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "html_url": "https://evil.example/notes",
        "assets": [
            {"name": name, "browser_download_url": prefix + name},
            {"name": f"{name}.sha256", "browser_download_url": prefix + name + ".sha256"},
        ],
    }
    data.update(changes)
    return data


class FakeGitHub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, url, headers):
        self.calls.append({"url": url, **headers})
        await asyncio.sleep(0)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        status, body, response_headers = response
        return status, response_headers, json.dumps(body).encode() if body is not None else b""


class FakeProvisioning:
    def __init__(self, mode="wifi", state="idle"):
        self.mode = mode
        self.state = SimpleNamespace(value=state)
        self.changed = asyncio.Event()

    async def window_change(self):
        await self.changed.wait()
        self.changed = asyncio.Event()


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def box(tmp_path):
    def make(*responses, installed="0.0.2", mode="wifi", free=10 * FREE_BYTES, start_code=0, **config):
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "run").mkdir(exist_ok=True)
        settings = SimpleNamespace(
            update_repo=config.get("update_repo", REPO),
            update_check=config.get("update_check", True),
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
        )
        started = []

        async def start_unit():
            started.append(True)
            return start_code, "polkit said no" if start_code else ""

        updates = Updates(
            settings,
            installed=installed,
            provisioning=FakeProvisioning(mode),
            reset=SimpleNamespace(open=False),
            start_unit=start_unit,
            fetch=FakeGitHub(*(responses or [(200, release(), {"etag": '"one"'})])),
            clock=Clock(),
            disk_free=lambda path: free,
            state_file=tmp_path / "update" / "state.json",
            disk_paths=(tmp_path,),
        )
        setattr(updates, "started", started)
        return updates

    return make


def write_state(updates, **fields):
    data = {
        "attempt": "abc123",
        "version": "0.0.3",
        "installed": "0.0.2",
        "step": "download",
        "result": "running",
        "error_code": None,
        "started": updates.clock(),
        "updated": updates.clock(),
    }
    data.update(fields)
    updates.state_file.parent.mkdir(parents=True, exist_ok=True)
    updates.state_file.write_text(json.dumps(data))


# -- versions and releases ----------------------------------------------------------------


def test_versions_compare_numerically_and_dev_builds_do_not_parse():
    assert parse_version("0.0.10") == (0, 0, 10) and (0, 0, 10) > (0, 0, 9)
    assert sorted(["0.0.10", "1.0.0", "0.0.9", "0.99.999"], key=lambda v: parse_version(v) or ()) == [
        "0.0.9",
        "0.0.10",
        "0.99.999",
        "1.0.0",
    ]
    for text in ("20261026-db683c1", "dev-abc", "v0.0.3", "0.0", "0.0.3-rc1", "", None, 3):
        assert parse_version(text) is None


def test_only_a_complete_stable_release_of_this_repo_is_offered():
    assert offered_version(release(), REPO) == "0.0.3"
    assert offered_version(release(prerelease=True), REPO) is None
    assert offered_version(release(draft=True), REPO) is None
    assert offered_version(release(tag_name="0.0.3"), REPO) is None
    assert offered_version(release(tag_name="v0.0.3-rc1"), REPO) is None
    assert offered_version(release(repo="someone/else"), REPO) is None
    assert offered_version(release(assets=release()["assets"][:1]), REPO) is None
    doubled = release()
    doubled["assets"].append(dict(doubled["assets"][0]))
    assert offered_version(doubled, REPO) is None
    assert offered_version(["not", "a", "release"], REPO) is None


# -- checking -----------------------------------------------------------------------------


async def test_newer_release_is_offered_with_a_rebuilt_changelog_link(box):
    updates = box()
    view = await updates.check()
    assert view["available"] is True and view["latest"] == "0.0.3" and view["installed"] == "0.0.2"
    assert view["changelog_url"] == f"https://github.com/{REPO}/releases/tag/v0.0.3"
    assert view["checked_at"] == updates.clock() and view["check"]["blocked"] is None
    [call] = updates.fetch.calls
    assert call["url"] == f"https://api.github.com/repos/{REPO}/releases/latest"


async def test_same_or_lower_release_is_not_offered(box):
    for installed in ("0.0.3", "0.1.0"):
        view = await box(installed=installed).check()
        assert view["available"] is False and view["latest"] == "0.0.3"
        assert view["changelog_url"] == f"https://github.com/{REPO}/releases/tag/v{installed}"


async def test_dev_build_never_checks_or_offers(box):
    updates = box(installed="20261026-db683c1")
    view = await updates.check(manual=True)
    assert updates.fetch.calls == [] and view["available"] is False
    assert view["dev_build"] is True and view["check"]["blocked"] == "DEV_BUILD"
    assert view["changelog_url"] is None
    updates.start()
    assert updates.task is None


async def test_local_and_setup_mode_skip_the_check(box):
    for mode in ("local", "setup"):
        updates = box(mode=mode)
        view = await updates.check(manual=True)
        assert updates.fetch.calls == [] and view["check"]["blocked"] == "NO_INTERNET"


async def test_manual_checks_coalesce(box):
    updates = box()
    await asyncio.gather(updates.check(manual=True), updates.check(manual=True), updates.check())
    assert len(updates.fetch.calls) == 1
    updates.clock.now += COALESCE_SECONDS - 1
    await updates.check(manual=True)
    assert len(updates.fetch.calls) == 1
    updates.clock.now += 2
    await updates.check(manual=True)
    assert len(updates.fetch.calls) == 2


async def test_etag_is_sent_and_304_keeps_the_known_release(box):
    updates = box((200, release(), {"etag": '"one"'}), (304, None, {}))
    await updates.check()
    updates.clock.now += 3600
    view = await updates.check()
    assert updates.fetch.calls[0].get("If-None-Match") is None
    assert updates.fetch.calls[1]["If-None-Match"] == '"one"'
    assert view["latest"] == "0.0.3" and view["checked_at"] == updates.clock()


async def test_rate_limit_backs_off_and_keeps_the_last_result(box):
    updates = box((200, release(), {}), (429, {"message": "slow down"}, {"retry-after": "900"}))
    await updates.check()
    first = updates.checked_at
    updates.clock.now += 3600
    view = await updates.check(manual=True)
    assert view["check"]["error"] == "RATE_LIMITED" and view["latest"] == "0.0.3"
    assert view["checked_at"] == first
    updates.clock.now += 899
    await updates.check(manual=True)
    assert len(updates.fetch.calls) == 2  # still backing off
    updates.clock.now += 2
    await updates.check(manual=True)
    assert len(updates.fetch.calls) == 3


async def test_403_without_retry_after_uses_the_rate_limit_reset(box):
    updates = box()
    reset = int(updates.clock()) + 1200
    updates.fetch.responses = [(403, {}, {"x-ratelimit-reset": str(reset)})]
    await updates.check()
    assert updates.retry_at == reset


async def test_network_errors_and_bad_releases_offer_nothing_new(box, caplog):
    updates = box(httpx.ConnectError("offline"))
    view = await updates.check()
    assert view["check"]["error"] == "CHECK_FAILED" and view["latest"] is None
    updates = box((200, release(prerelease=True), {}))
    view = await updates.check()
    assert view["latest"] is None and view["check"]["error"] is None
    updates.clock.now += 3600
    await updates.check()
    assert caplog.text.count("is not offered") == 1  # logged once per version
    updates = box((404, {"message": "Not Found"}, {}))
    assert (await updates.check())["latest"] is None


async def test_result_survives_a_restart_for_the_same_repo_only(box):
    await box().check()
    again = box(installed="0.0.2")
    assert again.latest == "0.0.3" and again.etag == '"one"' and again.checked_at is not None
    other = box(update_repo="you/fork")
    assert other.latest is None and other.etag is None


# -- installing ------------------------------------------------------------------------------


async def test_install_writes_the_request_and_starts_root_once(box):
    updates = box()
    await updates.check()
    attempt = await updates.install("0.0.3")
    assert attempt["result"] == "running" and attempt["step"] == "download"
    request = (updates.request_file).read_text().splitlines()
    assert request == ["version=0.0.3", f"attempt={attempt['id']}"]
    again = await updates.install("0.0.3")
    assert again["id"] == attempt["id"] and len(updates.started) == 1
    write_state(updates, attempt=attempt["id"], step="install")
    again = await updates.install("0.0.3")
    assert again["id"] == attempt["id"] and again["step"] == "install" and len(updates.started) == 1


@pytest.mark.parametrize(
    "setup, code",
    [
        (dict(installed="20261026-db683c1"), "DEV_BUILD"),
        (dict(mode="local"), "NO_INTERNET"),
        (dict(free=FREE_BYTES - 1), "LOW_DISK"),
    ],
)
async def test_install_refusals(box, setup, code):
    updates = box(**setup)
    updates.latest = "0.0.3"
    with pytest.raises(OpenLoloError) as refused:
        await updates.install("0.0.3")
    assert refused.value.code == code and updates.started == []
    assert not updates.request_file.exists()


async def test_install_refused_while_busy_or_for_another_version(box):
    updates = box()
    await updates.check()
    updates.reset.open = True
    with pytest.raises(OpenLoloError, match="reset") as refused:
        await updates.install("0.0.3")
    assert refused.value.code == "RESET_RUNNING"
    updates.reset.open = False
    updates.provisioning.state.value = "joining"
    with pytest.raises(OpenLoloError) as refused:
        await updates.install("0.0.3")
    assert refused.value.code == "PROVISIONING_BUSY"
    updates.provisioning.state.value = "idle"
    for version in ("0.0.4", "0.0.2", "../../etc"):
        with pytest.raises(OpenLoloError) as refused:
            await updates.install(version)
        assert refused.value.code == "UPDATE_NOT_AVAILABLE"
    assert updates.started == []


async def test_failed_unit_start_leaves_no_request(box):
    updates = box(start_code=1)
    await updates.check()
    with pytest.raises(OpenLoloError) as refused:
        await updates.install("0.0.3")
    assert refused.value.code == "UPDATE_START_FAILED" and refused.value.status == 502
    assert not updates.request_file.exists() and updates.attempt() is None


async def test_attempt_root_never_recorded_fails_after_a_minute(box):
    updates = box()
    await updates.check()
    attempt = await updates.install("0.0.3")
    updates.clock.now += START_SECONDS + 1
    assert updates.attempt() == {**attempt, "result": "failed", "error_code": "NOT_STARTED"}
    assert updates.attempt() is None


# -- attempt state -------------------------------------------------------------------------------


def test_state_file_missing_partial_or_garbage_means_no_attempt(box):
    updates = box()
    assert updates.attempt() is None
    write_state(updates, step=None)
    assert updates.attempt() is None
    write_state(updates, attempt="../x")
    assert updates.attempt() is None
    updates.state_file.write_text('{"attempt": "abc')
    assert updates.attempt() is None
    updates.state_file.write_text("[]")
    assert updates.attempt() is None


def test_recorded_outcome_after_the_restart(box):
    updates = box(installed="0.0.3")
    write_state(updates, step="restart", result="succeeded")
    attempt = updates.view()["attempt"]
    assert attempt["id"] == "abc123" and attempt["result"] == "succeeded" and attempt["version"] == "0.0.3"
    write_state(updates, step="restart", result="failed", error_code="VERIFY_FAILED")
    assert updates.attempt()["error_code"] == "VERIFY_FAILED"  # type: ignore[index]


def test_running_attempt_older_than_the_unit_timeout_is_stale(box):
    updates = box()
    write_state(updates, step="install")
    assert updates.attempt()["result"] == "running"  # type: ignore[index]
    updates.clock.now += STALE_SECONDS + 1
    assert updates.attempt() == {
        **updates._read_state(),  # type: ignore[dict-item]
        "result": "failed",
        "error_code": "STALE",
    }


def test_cancel_only_while_downloading(box):
    updates = box()
    with pytest.raises(OpenLoloError):
        updates.cancel("abc123")
    write_state(updates)
    with pytest.raises(OpenLoloError):
        updates.cancel("other")
    assert updates.cancel("abc123") == {"cancelling": "abc123"}
    assert updates.cancel_file.exists()
    updates.cancel_file.unlink()
    for fields in (dict(step="install"), dict(step="restart"), dict(result="failed")):
        write_state(updates, **fields)
        with pytest.raises(OpenLoloError) as refused:
            updates.cancel("abc123")
        assert refused.value.code == "CANCEL_UNAVAILABLE"
    assert not updates.cancel_file.exists()


# -- daily timer ---------------------------------------------------------------------------------


async def test_timer_waits_for_wifi_then_checks_after_the_boot_delay(box, monkeypatch):
    monkeypatch.setattr(update_module.random, "uniform", lambda a, b: 0)
    updates = box(mode="local")
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise asyncio.CancelledError

    updates.sleep = sleep
    task = asyncio.create_task(updates.run())
    await asyncio.sleep(0)
    assert slept == [] and updates.fetch.calls == []  # Local mode: waiting, not polling
    updates.provisioning.mode = "wifi"
    updates.provisioning.changed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slept == [BOOT_DELAY, update_module.DAY - BOOT_DELAY] and len(updates.fetch.calls) == 1


async def test_timer_off_when_automatic_checks_are_disabled(box):
    updates = box(update_check=False)
    updates.start()
    assert updates.task is None
    assert (await updates.check(manual=True))["check"]["automatic"] is False
    assert len(updates.fetch.calls) == 1
