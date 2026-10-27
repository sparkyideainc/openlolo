"""Software update, API side: find a newer GitHub release and hand the install to root.

design/plans/software-update.md. The API only checks and asks. It reads
``GET /repos/<update_repo>/releases/latest`` once a day (and when the owner taps Check for
updates), offers only a ``vX.Y.Z`` release newer than the installed one that carries exactly one
device tarball and its checksum, and never offers anything to a dev build or while the box has
no internet (Local and Setup mode).

Installing is root's job (``deploy/update/openlolo_update.py`` as ``openlolo-update.service``).
The API writes ``version=`` and ``attempt=`` to ``<runtime_dir>/update-request`` and starts the
unit; root re-validates the release itself, writes progress to
``/var/lib/openlolo/update/state.json`` and restarts this process. Cancel is a file
(``<runtime_dir>/update-cancel``) and only counts while root downloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger("openlolo.update")

UPDATE_UNIT = "openlolo-update.service"  # Root unit: deploy/update/openlolo_update.py
DEFAULT_REPO = "sparkyideainc/openlolo"
REPO_PATTERN = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,6})$")
ATTEMPT_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
STEPS = {"download", "install", "restart", "verify"}
RESULTS = {"running", "succeeded", "failed", "cancelled"}
JSON_CAP = 256 * 1024
FETCH_SECONDS = 10
COALESCE_SECONDS = 60  # A manual check this soon after the last one returns the cached result.
DEFAULT_BACKOFF = 3600  # After a 403 or 429 without Retry-After.
BOOT_DELAY = 300  # The first check waits until the box has had internet this long.
DAY = 24 * 3600
JITTER = 1800  # Daily checks drift by up to this much either way: a random minute, not midnight.
FREE_BYTES = 600 * 1024 * 1024  # Same floor as the root script.
START_SECONDS = 60  # Root writes state.json within moments of starting; longer means it never ran.
STALE_SECONDS = 50 * 60  # Past the unit's TimeoutStartSec (45 min): the attempt died unrecorded.


def parse_version(text: object) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(text) if isinstance(text, str) else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def changelog_url(repo: str, version: str) -> str:
    """Rebuilt from config, never taken from GitHub's response."""
    return f"https://github.com/{repo}/releases/tag/v{version}"


def offered_version(data: object, repo: str) -> str | None:
    """The version a ``releases/latest`` body offers, or None when it is not a usable release:
    a ``vX.Y.Z`` tag, not a draft or prerelease, exactly one device tarball and its checksum,
    both under this repo's download URL for that tag."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else None
    if not parse_version(version):
        return None
    tarball = f"openlolo-device-{version}.tar.gz"
    prefix = f"https://github.com/{repo}/releases/download/v{version}/"
    found: dict[str, list[str]] = {tarball: [], f"{tarball}.sha256": []}
    for asset in data.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") in found:
            found[asset["name"]].append(str(asset.get("browser_download_url", "")))
    for name, urls in found.items():
        if urls != [prefix + name]:
            return None
    return version


class TooLarge(Exception):
    pass


Fetch = Callable[[str, Mapping[str, str]], Awaitable[tuple[int, Mapping[str, str], bytes]]]


async def github_get(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
    """One GET with a size cap and an overall deadline; no redirects."""

    async def get():
        async with httpx.AsyncClient(timeout=FETCH_SECONDS, follow_redirects=False) as client:
            async with client.stream("GET", url, headers=dict(headers)) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > JSON_CAP:
                        raise TooLarge(url)
                return response.status_code, response.headers, bytes(body)

    return await asyncio.wait_for(get(), FETCH_SECONDS + 5)


def retry_delay(headers: Mapping[str, str], now: float) -> float:
    """Seconds to wait after a 403 or 429: Retry-After, else the rate-limit reset, else an hour."""
    after = headers.get("retry-after", "")
    if after.isdigit():
        return min(int(after), DAY)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit() and int(reset) > now:
        return min(int(reset) - now, DAY)
    return DEFAULT_BACKOFF


class Updates:
    def __init__(
        self,
        config,
        *,
        installed: str,
        provisioning=None,
        reset=None,
        start_unit: Callable[[], Awaitable[tuple[int, str]]] | None = None,
        fetch: Fetch = github_get,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
        state_file: Path = Path("/var/lib/openlolo/update/state.json"),
        disk_paths: tuple[Path, ...] = (Path("/opt/openlolo"), Path("/var/tmp")),
    ):
        self.repo: str = config.update_repo
        self.automatic: bool = config.update_check
        self.installed = installed
        self.provisioning, self.reset = provisioning, reset
        self.start_unit = start_unit
        self.fetch, self.clock, self.sleep, self.disk_free = fetch, clock, sleep, disk_free
        self.state_file, self.disk_paths = state_file, disk_paths
        self.cache_file = config.state_dir / "update-check.json"
        self.request_file = config.runtime_dir / "update-request"
        self.cancel_file = config.runtime_dir / "update-cancel"
        self.latest: str | None = None
        self.etag: str | None = None
        self.checked_at: float | None = None
        self.error: str | None = None
        self.tried_at = 0.0
        self.retry_at = 0.0
        self.pending: dict | None = None
        self.warned: set[str] = set()
        self.inflight: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self._load_cache()

    # -- observation ---------------------------------------------------------------

    @property
    def dev_build(self) -> bool:
        return parse_version(self.installed) is None

    @property
    def available(self) -> bool:
        latest, installed = parse_version(self.latest), parse_version(self.installed)
        return latest is not None and installed is not None and latest > installed

    def blocked(self) -> str | None:
        """Why the box cannot check or install right now, or None."""
        if self.dev_build:
            return "DEV_BUILD"
        if self.provisioning is not None and self.provisioning.mode != "wifi":
            return "NO_INTERNET"
        return None

    def view(self) -> dict:
        shown = self.latest if self.available and self.latest else self.installed
        return {
            "installed": self.installed,
            "dev_build": self.dev_build,
            "latest": self.latest,
            "available": self.available,
            "checked_at": self.checked_at,
            "changelog_url": changelog_url(self.repo, shown) if parse_version(shown) else None,
            "check": {"blocked": self.blocked(), "error": self.error, "automatic": self.automatic},
            "attempt": self.attempt(),
        }

    def attempt(self) -> dict | None:
        """The latest attempt as root recorded it in state.json, or the one this process just
        asked for while root has not written it yet."""
        now = self.clock()
        recorded = self._read_state()
        pending = self.pending
        if pending is not None and (recorded is None or recorded["id"] != pending["id"]):
            if now - pending["started"] < START_SECONDS:
                return {**pending, "step": "download", "result": "running", "error_code": None}
            self.pending = None
            log.warning("update %s: root never recorded the attempt", pending["id"])
            return {**pending, "step": "download", "result": "failed", "error_code": "NOT_STARTED"}
        if recorded is not None and recorded["result"] == "running":
            if now - (recorded["updated"] or recorded["started"] or 0) > STALE_SECONDS:
                return {**recorded, "result": "failed", "error_code": "STALE"}
        return recorded

    def _read_state(self) -> dict | None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        attempt, version = data.get("attempt"), data.get("version")
        step, result = data.get("step"), data.get("result")
        if not (isinstance(attempt, str) and ATTEMPT_RE.match(attempt)) or not parse_version(version):
            return None
        if step not in STEPS or result not in RESULTS:
            return None
        error = data.get("error_code")

        def number(key: str) -> float | None:
            value = data.get(key)
            return value if isinstance(value, int | float) and not isinstance(value, bool) else None

        return {
            "id": attempt,
            "version": version,
            "step": step,
            "result": result,
            "error_code": error if isinstance(error, str) and len(error) <= 64 else None,
            "started": number("started"),
            "updated": number("updated"),
        }

    # -- check ----------------------------------------------------------------------

    async def check(self, *, manual: bool = False) -> dict:
        """Read the latest release unless blocked, rate limited, or (manual) checked a moment
        ago. Concurrent callers share one request."""
        now = self.clock()
        if self.blocked() is None and now >= self.retry_at:
            if self.inflight is None and not (manual and now - self.tried_at < COALESCE_SECONDS):
                self.inflight = asyncio.create_task(self._check())
                self.inflight.add_done_callback(lambda _: setattr(self, "inflight", None))
            if self.inflight is not None:
                await asyncio.shield(self.inflight)
        return self.view()

    async def _check(self) -> None:
        now = self.tried_at = self.clock()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"openlolo/{self.installed}",
        }
        if self.etag:
            headers["If-None-Match"] = self.etag
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        try:
            status, response_headers, body = await self.fetch(url, headers)
        except (httpx.HTTPError, OSError, TimeoutError, TooLarge) as exc:
            log.info("update check failed: %s", exc)
            self.error = "CHECK_FAILED"
            return
        if status == 304:
            self.checked_at, self.error = now, None
        elif status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                self.error = "CHECK_FAILED"
                return
            self.latest = offered_version(data, self.repo)
            if self.latest is None:
                self._warn_once(data)
            self.etag = response_headers.get("etag") or None
            self.checked_at, self.error = now, None
        elif status == 404:  # no published release yet
            self.latest, self.etag, self.checked_at, self.error = None, None, now, None
        elif status in {403, 429}:
            self.retry_at = now + retry_delay(response_headers, now)
            self.error = "RATE_LIMITED"
            log.info("update check rate limited for %.0f s", self.retry_at - now)
        else:
            self.error = "CHECK_FAILED"
            log.info("update check: GitHub answered %d", status)
        self._save_cache()

    def _warn_once(self, data: object) -> None:
        tag = str(data.get("tag_name", "?"))[:40] if isinstance(data, dict) else "?"
        if tag not in self.warned:
            self.warned.add(tag)
            log.warning("latest release %s of %s is not offered: not a usable device release", tag, self.repo)

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_file.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("repo") != self.repo:
            return
        latest, etag, checked = data.get("latest"), data.get("etag"), data.get("checked_at")
        self.latest = latest if parse_version(latest) else None
        self.etag = etag if isinstance(etag, str) and len(etag) <= 200 else None
        self.checked_at = (
            checked if isinstance(checked, int | float) and not isinstance(checked, bool) else None
        )

    def _save_cache(self) -> None:
        data = {"repo": self.repo, "latest": self.latest, "etag": self.etag, "checked_at": self.checked_at}
        try:
            temporary = self.cache_file.with_name(f".{self.cache_file.name}.tmp")
            temporary.write_text(json.dumps(data))
            temporary.replace(self.cache_file)
        except OSError as exc:
            log.warning("update check result not saved: %s", exc)

    # -- install and cancel -------------------------------------------------------------

    async def install(self, version: str) -> dict:
        """Ask root to install ``version``. A running attempt is returned as is, so a second tap
        never starts a second download."""
        current = self.attempt()
        if current is not None and current["result"] == "running":
            return current
        blocked = self.blocked()
        if blocked == "DEV_BUILD":
            raise OpenLoloError("DEV_BUILD", "Development builds are not updated from the box page")
        if blocked == "NO_INTERNET":
            raise OpenLoloError("NO_INTERNET", "Connect the box to Wi-Fi to update it")
        if self.reset is not None and self.reset.open:
            raise OpenLoloError("RESET_RUNNING", "A reset is in progress")
        if self.provisioning is not None and self.provisioning.state.value in {"joining", "verifying"}:
            raise OpenLoloError("PROVISIONING_BUSY", "The box is joining a Wi-Fi network; try again shortly")
        if not self.available or version != self.latest:
            raise OpenLoloError("UPDATE_NOT_AVAILABLE", "That version is not offered to this box")
        for path in self.disk_paths:
            try:
                free = self.disk_free(path)
            except OSError:
                continue
            if free < FREE_BYTES:
                raise OpenLoloError("LOW_DISK", "The box needs 600 MB free to update")
        if self.start_unit is None:
            raise OpenLoloError("UPDATE_UNAVAILABLE", "This box cannot install updates")
        attempt = secrets.token_hex(8)
        temporary = self.request_file.with_name(f".{self.request_file.name}.tmp")
        temporary.write_text(f"version={version}\nattempt={attempt}\n")
        temporary.replace(self.request_file)
        self.cancel_file.unlink(missing_ok=True)
        self.pending = {"id": attempt, "version": version, "started": self.clock(), "updated": None}
        code, output = await self.start_unit()
        if code != 0:
            self.pending = None
            self.request_file.unlink(missing_ok=True)
            raise OpenLoloError("UPDATE_START_FAILED", output or "Could not start the update", 502)
        log.info("update %s to %s started", attempt, version)
        return self.attempt() or {}

    def cancel(self, attempt: str) -> dict:
        """Stop an attempt that is still downloading. From the install step on there is no
        cancel: the new build either swaps in or the old one comes back."""
        current = self.attempt()
        if (
            current is None
            or current["id"] != attempt
            or current["result"] != "running"
            or current["step"] != "download"
        ):
            raise OpenLoloError("CANCEL_UNAVAILABLE", "Only a download in progress can be cancelled")
        fd = os.open(self.cancel_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.close(fd)
        return {"cancelling": attempt}

    # -- daily timer ----------------------------------------------------------------------

    async def run(self) -> None:
        """Check once the box has had internet for BOOT_DELAY, then about once a day. Sleeps the
        whole time in between: no wakeups outside the daily check."""
        while True:
            if self.blocked() == "NO_INTERNET" and self.provisioning is not None:
                await self.provisioning.window_change()
                continue
            await self.sleep(BOOT_DELAY)
            if self.blocked() is not None:
                continue
            await self.check()
            await self.sleep(DAY - BOOT_DELAY + random.uniform(-JITTER, JITTER))

    def start(self) -> None:
        if self.automatic and not self.dev_build and self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        for task in (self.task, self.inflight):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.task = self.inflight = None
