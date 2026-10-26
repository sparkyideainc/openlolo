#!/usr/bin/python3
"""Root side of the box page's software update (design/plans/software-update.md).

Runs as ``openlolo-update.service`` (``run``) and, at boot, ``openlolo-update-recover.service``
(``recover``). Standard library only: it runs from the system Python, so a broken release venv
cannot stop it.

The API never passes arguments. It writes ``version=`` and ``attempt=`` to
``/run/openlolo/update-request`` and starts the unit; this script re-reads the release from
GitHub itself and refuses anything that is not a newer ``vX.Y.Z`` release of ``update_repo``.
Progress goes to ``/var/lib/openlolo/update/state.json`` for the API to read, before and after
the restart. A file at ``/run/openlolo/update-cancel`` stops the attempt while it downloads;
from the install step on there is no cancel, and ``/opt/openlolo/previous`` is the way back.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_REPO = "sparkyideainc/openlolo"
VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,6})$")
ATTEMPT_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
JSON_CAP = 256 * 1024
SHA_CAP = 1024
TARBALL_CAP = 200 * 1024 * 1024
FREE_BYTES = 600 * 1024 * 1024
CHUNK = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_SECONDS = 600
INSTALL_SECONDS = 1800
HEALTH_SECONDS = 120
SERVICES = ("openlolo-device.service", "openlolo-api.service")


class UpdateError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class Paths:
    root: Path = Path("/opt/openlolo")
    config: Path = Path("/etc/openlolo/box.toml")
    run: Path = Path("/run/openlolo")  # writable by the API user: never follow links here
    private: Path = Path("/run/openlolo-update")  # root only (RuntimeDirectory of the unit)
    state_dir: Path = Path("/var/lib/openlolo/update")
    work: Path = Path("/var/tmp/openlolo-update")

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def previous(self) -> Path:
        return self.root / "previous"

    @property
    def failed(self) -> Path:
        return self.root / "current.failed"

    @property
    def request(self) -> Path:
        return self.run / "update-request"

    @property
    def cancel(self) -> Path:
        return self.run / "update-cancel"

    @property
    def lock(self) -> Path:
        return self.private / "update.lock"

    @property
    def state(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def log(self) -> Path:
        return self.state_dir / "install.log"


@dataclass
class System:
    """Everything that leaves the process, replaceable in tests."""

    urlopen: Callable = urllib.request.urlopen
    run: Callable = subprocess.run
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    disk_free: Callable[[Path], int] = field(default=lambda path: shutil.disk_usage(path).free)

    def systemctl(self, *args: str) -> None:
        self.run(["systemctl", *args], check=False)


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(text or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def read_release(current: Path) -> str | None:
    """The installed release stamped by scripts/package.sh (``release=`` in RELEASE)."""
    try:
        for line in (current / "RELEASE").read_text().splitlines():
            if line.startswith("release="):
                return line.removeprefix("release=").strip() or None
    except OSError:
        return None
    return None


def read_config(path: Path) -> tuple[str, int]:
    """``update_repo`` and ``portal_port`` from box.toml; defaults when unset or unreadable."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    repo = data.get("update_repo", DEFAULT_REPO)
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise UpdateError("INVALID_CONFIG", "update_repo must look like owner/name")
    port = data.get("portal_port", 80)
    if not isinstance(port, int) or not 0 < port < 65536:
        port = 80
    return repo, port


def read_request(path: Path) -> tuple[str, str]:
    # The API user owns the directory: O_NOFOLLOW keeps a planted link from pointing root elsewhere.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            text = handle.read(512).decode("ascii", "replace")
    except OSError as error:
        raise UpdateError("NO_REQUEST") from error
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    version, attempt = fields.get("version", "").strip(), fields.get("attempt", "").strip()
    if not parse_version(version) or not ATTEMPT_RE.match(attempt):
        raise UpdateError("INVALID_REQUEST")
    return version, attempt


class State:
    """state.json, replaced atomically on every change and readable by the API."""

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = path
        self.clock = clock
        self.data: dict = {}

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        self.data = data if isinstance(data, dict) else {}
        return self.data

    def write(self, **changes) -> None:
        self.data.update(changes, updated=int(self.clock()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True))
        tmp.chmod(0o644)
        os.replace(tmp, self.path)


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise UpdateError("INSECURE_REDIRECT", newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


def https_urlopen(request, timeout):
    return _OPENER.open(request, timeout=timeout)


def fetch(system: System, url: str, cap: int, timeout: float) -> bytes:
    if not url.startswith("https://"):
        raise UpdateError("INSECURE_URL", url)
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with system.urlopen(request, timeout=timeout) as response:
            body = response.read(cap + 1)
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError("DOWNLOAD_FAILED", str(error)) from error
    if len(body) > cap:
        raise UpdateError("TOO_LARGE", url)
    return body


def release_assets(data: dict, repo: str, version: str) -> tuple[str, str]:
    """Download URLs of the device tarball and its checksum, after checking the release."""
    if data.get("tag_name") != f"v{version}" or data.get("draft") or data.get("prerelease"):
        raise UpdateError("INVALID_RELEASE", "not a published v-tag release")
    prefix = f"https://github.com/{repo}/releases/download/v{version}/"
    name = f"openlolo-device-{version}.tar.gz"
    urls = {
        asset.get("name"): asset.get("browser_download_url", "")
        for asset in data.get("assets", [])
        if isinstance(asset, dict)
    }
    tarball, checksum = urls.get(name), urls.get(f"{name}.sha256")
    if tarball != prefix + name or checksum != prefix + name + ".sha256":
        raise UpdateError("INVALID_RELEASE", "device tarball or checksum missing")
    return tarball, checksum


def expected_digest(text: str, name: str) -> str:
    parts = text.split()
    if len(parts) < 2 or not SHA_RE.match(parts[0]) or parts[1].lstrip("*") != name:
        raise UpdateError("INVALID_CHECKSUM")
    return parts[0]


def download(system: System, url: str, dest: Path, cancelled: Callable[[], bool]) -> str:
    """Stream ``url`` to ``dest`` within TARBALL_CAP; return its sha256. Retries with backoff."""
    last: UpdateError | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        if attempt:
            system.sleep(5 * 2**attempt)
        try:
            digest = hashlib.sha256()
            size = 0
            request = urllib.request.Request(url)
            with system.urlopen(request, timeout=DOWNLOAD_SECONDS) as response, dest.open("wb") as out:
                while chunk := response.read(CHUNK):
                    if cancelled():
                        raise UpdateError("CANCELLED")
                    size += len(chunk)
                    if size > TARBALL_CAP:
                        raise UpdateError("TOO_LARGE", url)
                    digest.update(chunk)
                    out.write(chunk)
            return digest.hexdigest()
        except UpdateError as error:
            if error.code in ("CANCELLED", "TOO_LARGE", "INSECURE_REDIRECT"):
                raise
            last = error
        except OSError as error:
            last = UpdateError("DOWNLOAD_FAILED", str(error))
    assert last is not None
    raise last


def unpack(tarball: Path, dest: Path, version: str) -> Path:
    name = f"openlolo-device-{version}"
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            if any(not m.name.startswith(f"{name}/") and m.name != name for m in archive.getmembers()):
                raise UpdateError("INVALID_ARCHIVE", "unexpected top-level entry")
            archive.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise UpdateError("INVALID_ARCHIVE", str(error)) from error
    source = dest / name
    if not (source / "scripts" / "install.sh").is_file():
        raise UpdateError("INVALID_ARCHIVE", "scripts/install.sh missing")
    return source


def healthy(system: System, port: int, version: str, seconds: float = HEALTH_SECONDS) -> bool:
    """True once the box page's API answers /setup/status with ``version``."""
    deadline = system.clock() + seconds
    url = f"http://127.0.0.1:{port}/setup/status"
    while system.clock() < deadline:
        try:
            with system.urlopen(urllib.request.Request(url), timeout=5) as response:
                if json.loads(response.read(JSON_CAP)).get("version") == version:
                    return True
        except (OSError, ValueError, AttributeError):
            pass
        system.sleep(3)
    return False


def rollback(paths: Paths, system: System) -> bool:
    """Put ``previous`` back as ``current``; False when there is nothing to go back to."""
    if not paths.previous.is_dir():
        return False
    system.systemctl("stop", *SERVICES)
    shutil.rmtree(paths.failed, ignore_errors=True)
    if paths.current.exists():
        paths.current.rename(paths.failed)
    paths.previous.rename(paths.current)
    system.systemctl("start", *SERVICES)
    return True


def run_update(paths: Paths, system: System, *, force: bool = False) -> int:
    state = State(paths.state, system.clock)
    paths.private.mkdir(mode=0o700, parents=True, exist_ok=True)
    with paths.lock.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # another attempt is running; the API reports that one
        version, attempt = read_request(paths.request)
        paths.request.unlink(missing_ok=True)
        paths.cancel.unlink(missing_ok=True)
        installed = read_release(paths.current)
        state.data = {}
        state.write(
            attempt=attempt,
            version=version,
            installed=installed,
            step="download",
            result="running",
            error_code=None,
            started=int(system.clock()),
        )
        try:
            _update(paths, system, state, version, installed, force)
        except UpdateError as error:
            result = "cancelled" if error.code == "CANCELLED" else "failed"
            print(f"update {attempt} to {version}: {error}", file=sys.stderr)
            state.write(result=result, error_code=None if result == "cancelled" else error.code)
            return 1
        finally:
            paths.cancel.unlink(missing_ok=True)
            shutil.rmtree(paths.work, ignore_errors=True)
        state.write(result="succeeded")
        return 0


def _update(
    paths: Paths, system: System, state: State, version: str, installed: str | None, force: bool
) -> None:
    repo, port = read_config(paths.config)
    current = parse_version(installed)
    if current is None and not force:
        raise UpdateError("DEV_BUILD", f"installed release {installed!r} is not vX.Y.Z")
    if current is not None and parse_version(version) <= current and not force:  # type: ignore[operator]
        raise UpdateError("NOT_NEWER", f"{version} is not newer than {installed}")
    for path in (paths.root, paths.work.parent):
        if system.disk_free(path) < FREE_BYTES:
            raise UpdateError("LOW_DISK", str(path))

    api = f"https://api.github.com/repos/{repo}/releases/tags/v{version}"
    try:
        release = json.loads(fetch(system, api, JSON_CAP, 10))
    except ValueError as error:
        raise UpdateError("INVALID_RELEASE", "release JSON unreadable") from error
    tarball_url, checksum_url = release_assets(release, repo, version)
    name = tarball_url.rsplit("/", 1)[1]
    digest = expected_digest(fetch(system, checksum_url, SHA_CAP, 10).decode("ascii", "replace"), name)

    shutil.rmtree(paths.work, ignore_errors=True)
    paths.work.mkdir(parents=True)
    tarball = paths.work / name
    if download(system, tarball_url, tarball, paths.cancel.exists) != digest:
        raise UpdateError("CHECKSUM_MISMATCH")
    source = unpack(tarball, paths.work, version)

    state.write(step="install")
    with paths.log.open("w") as log:
        try:
            done = system.run(
                ["/bin/sh", str(source / "scripts" / "install.sh"), str(source), "--upgrade"],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=INSTALL_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            rollback(paths, system)
            raise UpdateError("INSTALL_TIMEOUT") from error
    swapped = read_release(paths.current) == version
    if done.returncode != 0:
        if swapped:
            rollback(paths, system)
        raise UpdateError("INSTALL_FAILED", f"install.sh exited {done.returncode}; see {paths.log}")

    state.write(step="restart")
    if not healthy(system, port, version):
        rollback(paths, system)
        raise UpdateError("VERIFY_FAILED", "the new API did not answer in time")
    shutil.rmtree(paths.previous, ignore_errors=True)


def recover(paths: Paths, system: System) -> int:
    """At boot: finish an attempt that a power cut or crash interrupted."""
    state = State(paths.state, system.clock)
    data = state.load()
    if data.get("result") != "running":
        return 0
    version = data.get("version")
    if data.get("step") == "download" or not paths.previous.is_dir() or not parse_version(version):
        state.write(result="failed", error_code="INTERRUPTED")
        return 0
    _, port = read_config(paths.config)
    if read_release(paths.current) == version and healthy(system, port, version):
        shutil.rmtree(paths.previous, ignore_errors=True)
        state.write(result="succeeded")
        return 0
    rollback(paths, system)
    state.write(result="failed", error_code="VERIFY_FAILED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "recover"), nargs="?", default="run")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow a dev build or a same/older version (root over SSH only; the unit never passes it)",
    )
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Run as root", file=sys.stderr)
        return 2
    system = System(urlopen=https_urlopen)
    paths = Paths()
    if args.command == "recover":
        return recover(paths, system)
    try:
        return run_update(paths, system, force=args.force)
    except UpdateError as error:  # bad request file: nothing started, nothing to record
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
