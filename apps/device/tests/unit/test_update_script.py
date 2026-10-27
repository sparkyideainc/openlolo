"""deploy/update/openlolo_update.py: the root side of the box page's software update."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "update" / "openlolo_update.py"
spec = importlib.util.spec_from_file_location("openlolo_update", SCRIPT)
assert spec and spec.loader
upd = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = upd  # dataclasses look the module up while the file executes
spec.loader.exec_module(upd)

REPO = "sparkyideainc/openlolo"
NEW = "0.0.3"
BASE = f"https://github.com/{REPO}/releases/download/v{NEW}/"
NAME = f"openlolo-device-{NEW}.tar.gz"


def tarball(version: str = NEW, extra: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in (f"openlolo-device-{version}/scripts/install.sh", extra):
            if name:
                data = b"#!/bin/sh\n"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def release_json(**overrides) -> dict:
    data = {
        "tag_name": f"v{NEW}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": NAME, "browser_download_url": BASE + NAME},
            {"name": NAME + ".sha256", "browser_download_url": BASE + NAME + ".sha256"},
        ],
    }
    data.update(overrides)
    return data


class FakeSystem(upd.System):
    """GitHub, install.sh, systemctl and the local API, all in memory."""

    def __init__(self, paths, *, archive=None, digest=None, install_rc=0, swap=True, healthy=True):
        self.paths = paths
        self.archive = archive if archive is not None else tarball()
        self.digest = digest or hashlib.sha256(self.archive).hexdigest()
        self.install_rc, self.swap, self.is_healthy = install_rc, swap, healthy
        self.calls: list[list[str]] = []
        self.now = 1000.0
        self.downloaded = 0
        super().__init__(
            urlopen=self.fake_urlopen,
            run=self.fake_run,
            clock=lambda: self.now,
            sleep=self.fake_sleep,
            disk_free=lambda path: 10**12,
        )

    def fake_sleep(self, seconds):
        self.now += seconds

    def fake_urlopen(self, request, timeout):
        url = request.full_url
        if url == f"https://api.github.com/repos/{REPO}/releases/tags/v{NEW}":
            return io.BytesIO(json.dumps(release_json()).encode())
        if url == BASE + NAME + ".sha256":
            return io.BytesIO(f"{self.digest}  {NAME}\n".encode())
        if url == BASE + NAME:
            self.downloaded += 1
            return io.BytesIO(self.archive)
        if url.endswith("/setup/status"):
            if not self.is_healthy:
                raise OSError("connection refused")
            return io.BytesIO(json.dumps({"version": upd.read_release(self.paths.current)}).encode())
        raise AssertionError(f"unexpected URL {url}")

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "/bin/sh" and self.swap:  # what install.sh --upgrade does on the box
            self.paths.current.rename(self.paths.previous)
            make_release(self.paths.current, NEW)
        return subprocess.CompletedProcess(args, self.install_rc)


def make_release(directory: Path, release: str) -> None:
    directory.mkdir(parents=True)
    (directory / "RELEASE").write_text(f"release={release}\ncommit=abc\n")


@pytest.fixture
def paths(tmp_path):
    p = upd.Paths(
        root=tmp_path / "opt",
        config=tmp_path / "box.toml",
        run=tmp_path / "run",
        private=tmp_path / "private",
        state_dir=tmp_path / "state",
        work=tmp_path / "vartmp" / "work",
    )
    make_release(p.current, "0.0.2")
    p.run.mkdir()
    p.work.parent.mkdir()
    p.config.write_text('setup_name = "OpenLolo-TEST"\n')
    p.request.write_text(f"version={NEW}\nattempt=a1\n")
    return p


def state(paths) -> dict:
    return json.loads(paths.state.read_text())


def test_versions_and_config(paths):
    assert upd.parse_version("0.0.3") == (0, 0, 3)
    assert upd.parse_version("0.10.0") > upd.parse_version("0.9.9")
    for bad in ("v0.0.3", "20261020-04caa74", "dev-202610261200", "1.2", "", None):
        assert upd.parse_version(bad) is None
    assert upd.read_release(paths.current) == "0.0.2"
    assert upd.read_config(paths.config) == (REPO, 80)
    paths.config.write_text('update_repo = "you/fork"\nportal_port = 8080\n')
    assert upd.read_config(paths.config) == ("you/fork", 8080)
    paths.config.write_text('update_repo = "../../etc"\n')
    with pytest.raises(upd.UpdateError, match="INVALID_CONFIG"):
        upd.read_config(paths.config)


def test_request_is_validated_and_never_followed(paths, tmp_path):
    assert upd.read_request(paths.request) == (NEW, "a1")
    for text in ("version=v0.0.3\nattempt=a1\n", "version=0.0.3\nattempt=../x\n", "attempt=a1\n"):
        paths.request.write_text(text)
        with pytest.raises(upd.UpdateError, match="INVALID_REQUEST"):
            upd.read_request(paths.request)
    target = tmp_path / "secret"
    target.write_text(f"version={NEW}\nattempt=a1\n")
    paths.request.unlink()
    paths.request.symlink_to(target)
    with pytest.raises(upd.UpdateError, match="NO_REQUEST"):
        upd.read_request(paths.request)


def test_release_assets_accepts_only_this_repos_tarball():
    assert upd.release_assets(release_json(), REPO, NEW) == (BASE + NAME, BASE + NAME + ".sha256")
    for bad in (
        release_json(draft=True),
        release_json(prerelease=True),
        release_json(tag_name="v0.0.4"),
        release_json(assets=[{"name": NAME, "browser_download_url": BASE + NAME}]),
        release_json(
            assets=[
                {"name": NAME, "browser_download_url": "https://evil.example/" + NAME},
                {"name": NAME + ".sha256", "browser_download_url": BASE + NAME + ".sha256"},
            ]
        ),
    ):
        with pytest.raises(upd.UpdateError, match="INVALID_RELEASE"):
            upd.release_assets(bad, REPO, NEW)


def test_checksum_line_and_archive_layout(tmp_path):
    digest = "a" * 64
    assert upd.expected_digest(f"{digest}  {NAME}\n", NAME) == digest
    for bad in (f"{digest}  other.tar.gz", "zz  " + NAME, ""):
        with pytest.raises(upd.UpdateError, match="INVALID_CHECKSUM"):
            upd.expected_digest(bad, NAME)
    good = tmp_path / "good.tar.gz"
    good.write_bytes(tarball())
    assert upd.unpack(good, tmp_path / "out", NEW) == tmp_path / "out" / f"openlolo-device-{NEW}"
    stray = tmp_path / "stray.tar.gz"
    stray.write_bytes(tarball(extra="elsewhere/file"))
    with pytest.raises(upd.UpdateError, match="INVALID_ARCHIVE"):
        upd.unpack(stray, tmp_path / "out2", NEW)


def test_update_succeeds_and_drops_previous(paths):
    system = FakeSystem(paths)
    assert upd.run_update(paths, system) == 0
    assert upd.read_release(paths.current) == NEW and not paths.previous.exists()
    s = state(paths)
    assert s["result"] == "succeeded" and s["step"] == "restart" and s["attempt"] == "a1"
    assert s["installed"] == "0.0.2" and s["error_code"] is None
    assert not paths.request.exists() and not paths.work.exists()
    install = next(call for call in system.calls if call[0] == "/bin/sh")
    assert install[-1] == "--upgrade"


def test_unhealthy_new_build_is_rolled_back(paths):
    system = FakeSystem(paths, healthy=False)
    assert upd.run_update(paths, system) == 1
    assert upd.read_release(paths.current) == "0.0.2" and not paths.previous.exists()
    assert upd.read_release(paths.failed) == NEW
    assert state(paths)["result"] == "failed" and state(paths)["error_code"] == "VERIFY_FAILED"
    assert ["systemctl", "start", *upd.SERVICES] in system.calls


def test_install_failure_before_the_swap_changes_nothing(paths):
    system = FakeSystem(paths, install_rc=1, swap=False)
    assert upd.run_update(paths, system) == 1
    assert upd.read_release(paths.current) == "0.0.2"
    assert state(paths)["error_code"] == "INSTALL_FAILED"
    assert not any(call[0] == "systemctl" for call in system.calls)


@pytest.mark.parametrize(
    ("installed", "code"), [("0.0.3", "NOT_NEWER"), ("0.1.0", "NOT_NEWER"), ("20261020-04caa74", "DEV_BUILD")]
)
def test_refuses_dev_builds_and_downgrades(paths, installed, code):
    (paths.current / "RELEASE").write_text(f"release={installed}\n")
    system = FakeSystem(paths)
    assert upd.run_update(paths, system) == 1
    assert state(paths)["error_code"] == code and system.downloaded == 0


def test_force_allows_a_dev_build(paths):
    (paths.current / "RELEASE").write_text("release=20261020-04caa74\n")
    assert upd.run_update(paths, FakeSystem(paths), force=True) == 0


def test_checksum_mismatch_and_low_disk(paths):
    system = FakeSystem(paths, digest="b" * 64)
    assert upd.run_update(paths, system) == 1
    assert state(paths)["error_code"] == "CHECKSUM_MISMATCH" and upd.read_release(paths.current) == "0.0.2"
    paths.request.write_text(f"version={NEW}\nattempt=a2\n")
    system = FakeSystem(paths)
    system.disk_free = lambda path: 10
    assert upd.run_update(paths, system) == 1
    assert state(paths)["error_code"] == "LOW_DISK" and state(paths)["attempt"] == "a2"


def test_cancel_during_download(paths):
    paths.cancel.write_text("")  # removed at start, so plant it after the request is read
    system = FakeSystem(paths)
    original = system.fake_urlopen

    def cancel_then_download(request, timeout):
        if request.full_url == BASE + NAME:
            paths.cancel.write_text("")
        return original(request, timeout)

    system.urlopen = cancel_then_download
    assert upd.run_update(paths, system) == 1
    s = state(paths)
    assert s["result"] == "cancelled" and s["error_code"] is None
    assert upd.read_release(paths.current) == "0.0.2" and not paths.cancel.exists()


def test_second_attempt_while_one_runs_does_nothing(paths):
    paths.private.mkdir()
    with paths.lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert upd.run_update(paths, FakeSystem(paths)) == 0
    assert not paths.state.exists() and paths.request.exists()


def test_recover_after_power_loss(paths):
    write = upd.State(paths.state, lambda: 1.0).write
    write(attempt="a1", version=NEW, step="download", result="running")
    assert upd.recover(paths, FakeSystem(paths)) == 0
    assert state(paths)["result"] == "failed" and state(paths)["error_code"] == "INTERRUPTED"

    # Power cut after the swap, new build healthy at boot: keep it.
    paths.current.rename(paths.previous)
    make_release(paths.current, NEW)
    write(step="restart", result="running", error_code=None)
    assert upd.recover(paths, FakeSystem(paths)) == 0
    assert state(paths)["result"] == "succeeded" and not paths.previous.exists()

    # Same, but the new build never answers: put the old one back.
    paths.current.rename(paths.previous)
    make_release(paths.current, "0.0.4")
    write(version="0.0.4", step="install", result="running")
    assert upd.recover(paths, FakeSystem(paths, healthy=False)) == 0
    assert upd.read_release(paths.current) == NEW
    assert state(paths)["error_code"] == "VERIFY_FAILED"


def test_recover_ignores_finished_attempts(paths):
    upd.State(paths.state, lambda: 1.0).write(attempt="a1", version=NEW, step="restart", result="succeeded")
    before = paths.state.read_text()
    assert upd.recover(paths, FakeSystem(paths)) == 0
    assert paths.state.read_text() == before
