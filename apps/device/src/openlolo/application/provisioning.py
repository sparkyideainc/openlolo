"""Local Wi-Fi setup mode and provisioning state machine.

    IDLE --(no saved network at boot | grace period offline | button | API)--> SETUP_MODE
    SETUP_MODE --(credentials)--> JOINING --> VERIFYING --> IDLE (joined)
    JOINING/VERIFYING --(failure)--> SETUP_MODE (only the new profile removed)
    SETUP_MODE --(Wi-Fi connected | API | timeout)--> IDLE

The setup window activates a WPA2-protected SoftAP and local configuration page. The same
radio changes to station mode for a join; on success the phone reconnects to home Wi-Fi and
reads the saved result at the same local hostname. A failed attempt recreates the AP. The
setup button or an unconfigured boot opens a bounded window. Only profiles created by a failed
attempt may be deleted.
"""

import asyncio
import contextlib
import enum
import logging
import re
import secrets
import time
from pathlib import Path

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger("openlolo.provisioning")

HANDOFF_READ_SECONDS = 6  # Captive sheet time to show the next address before the AP drops.


class ProvisioningState(str, enum.Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    SETUP_MODE = "setup_mode"
    JOINING = "joining"
    VERIFYING = "verifying"


def slug(ssid: str) -> str:
    return "openlolo-" + (re.sub(r"[^a-z0-9]+", "-", ssid.lower()).strip("-") or "wifi")[:24]


class Provisioning:
    def __init__(self, config, network, *, clock=time.monotonic, button_flag: Path | None = None):
        self.config, self.network = config, network
        self.setup_name = config.setup_name or "OpenLolo"
        self.clock = clock
        self.button_flag = button_flag
        self.state = ProvisioningState.IDLE if config.provisioning else ProvisioningState.DISABLED
        self.since = clock()
        self.boot = self.since  # run() resets it; tick() may also be driven directly.
        self.expires: float | None = None
        self.last_error: dict | None = None
        self.last_attempt: dict | None = None
        self.reason: str | None = None
        self.task: asyncio.Task | None = None
        self.worker: asyncio.Task | None = None
        self.grace_used = False
        self.button_seen = 0.0
        self.window = False  # Setup window open: the temporary Wi-Fi network is active.

    # -- observation ---------------------------------------------------------------

    @property
    def active(self) -> bool:
        """The setup window is open (also while a join started from it is in progress)."""
        return self.window

    def health(self) -> dict:
        return {
            "state": self.state.value,
            "since": self.since,
            "reason": self.reason,
            "expires_in": max(0, int(self.expires - self.clock())) if self.expires else None,
            "setup_name": self.setup_name if self.active else None,
            "last_error": self.last_error,
            "last_attempt": self.last_attempt,
        }

    def _set(self, state: ProvisioningState, reason: str | None = None) -> None:
        if state != self.state:
            self.since = self.clock()
        self.state = state
        self.reason = reason

    # -- transitions ----------------------------------------------------------------

    async def enter(self, reason: str = "api") -> None:
        if self.state == ProvisioningState.DISABLED:
            raise OpenLoloError("PROVISIONING_DISABLED", status=409)
        if self.state in {ProvisioningState.JOINING, ProvisioningState.VERIFYING}:
            raise OpenLoloError("PROVISIONING_BUSY", "A Wi-Fi join is in progress", 409)
        await self.network.start_setup_ap()
        self.window = True
        self.expires = self.clock() + self.config.setup_timeout_seconds
        self._set(ProvisioningState.SETUP_MODE, reason)

    async def exit(self) -> None:
        await self.finish("exit")

    async def finish(self, reason: str) -> None:
        """Close setup and stop the temporary access point."""
        if self.state in {ProvisioningState.JOINING, ProvisioningState.VERIFYING}:
            raise OpenLoloError("PROVISIONING_BUSY", "A Wi-Fi join is in progress", 409)
        self.expires = None
        self.window = False
        await self.network.stop_setup_ap()
        if self.state != ProvisioningState.DISABLED:
            self._set(ProvisioningState.IDLE, reason)

    async def connect(self, ssid: str, psk: str) -> str:
        if self.state == ProvisioningState.DISABLED:
            raise OpenLoloError("PROVISIONING_DISABLED", status=409)
        if self.state in {ProvisioningState.JOINING, ProvisioningState.VERIFYING}:
            raise OpenLoloError("PROVISIONING_BUSY", "A Wi-Fi join is in progress", 409)
        attempt = secrets.token_hex(4)
        self.last_attempt = {"id": attempt, "ssid": ssid, "started": time.time(), "result": None}
        self.last_error = None
        # Let the 202 response and the portal's handoff message reach the browser before the
        # single Wi-Fi radio leaves AP mode. On the setup network the captive sheet dies with
        # the AP, so the page must stay long enough to be read: it tells the owner which
        # address to open next.
        delay = HANDOFF_READ_SECONDS if self.window else 1
        self.worker = asyncio.create_task(
            self._join_after_response(ssid, psk, f"{slug(ssid)}-{attempt}", delay)
        )
        return attempt

    async def _join_after_response(self, ssid: str, psk: str, name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._join(ssid, psk, name)

    async def _join(self, ssid: str, psk: str, name: str) -> None:
        in_setup = self.window
        previous = None
        if not in_setup:
            with contextlib.suppress(Exception):
                previous = (await self.network.status()).get("connection")
        self._set(ProvisioningState.JOINING, "credentials received")
        try:
            await self.network.connect(ssid, psk, name)
            self._set(ProvisioningState.VERIFYING, "link up")
            await self._verify()
            self.last_attempt = {**(self.last_attempt or {}), "result": "connected", "profile": name}
            if in_setup:
                # The AP must turn off before this single-radio device can join home Wi-Fi.
                self.window = False
                self.expires = None
                self._set(ProvisioningState.IDLE, "connected")
            else:
                self._set(ProvisioningState.IDLE, "connected")
        except Exception as exc:
            if not isinstance(exc, OpenLoloError):
                log.exception("Wi-Fi setup join failed unexpectedly")
                exc = OpenLoloError("WIFI_JOIN_FAILED", "OpenLolo could not join that network", 502)
            self.last_error = exc.as_dict()
            self.last_attempt = {**(self.last_attempt or {}), "result": exc.code}
            with contextlib.suppress(Exception):
                await self.network.forget(name)  # Only the profile this attempt created.
            if in_setup:
                with contextlib.suppress(Exception):
                    await self.network.start_setup_ap()
                self.expires = self.clock() + self.config.setup_timeout_seconds
                self._set(ProvisioningState.SETUP_MODE, "join failed")
            else:
                restored = False
                if previous and previous != name:
                    try:
                        await self.network.restore_connection(previous)
                        status = await self.network.status()
                        restored = status.get("connection") == previous and bool(status.get("ip"))
                    except Exception:
                        log.warning("could not restore previous Wi-Fi profile %s", previous)
                if restored:
                    self._set(ProvisioningState.IDLE, "join failed; previous network restored")
                else:
                    try:
                        # If a LAN-originated change leaves no working station link, expose the
                        # normal bounded recovery AP so the user can correct the credentials.
                        await self.network.start_setup_ap()
                    except Exception:
                        log.exception("could not start Wi-Fi recovery access point")
                        self._set(ProvisioningState.IDLE, "join failed; recovery AP unavailable")
                    else:
                        self.window = True
                        self.expires = self.clock() + self.config.setup_timeout_seconds
                        self._set(ProvisioningState.SETUP_MODE, "join failed; recovery AP")

    async def _verify(self, seconds: float = 15) -> None:
        deadline = self.clock() + seconds
        while True:
            status = await self.network.status()
            if status.get("ssid") and status.get("ip"):
                return
            if self.clock() >= deadline:
                raise OpenLoloError("WIFI_TIMEOUT", "Joined but no address was obtained", 502)
            await asyncio.sleep(1)

    # -- supervisor -----------------------------------------------------------------

    async def tick(self) -> None:
        """One supervisor step: timeouts, boot/grace entry, and the physical button."""
        if self.state == ProvisioningState.DISABLED:
            return
        now = self.clock()
        if (
            self.window
            and self.state == ProvisioningState.SETUP_MODE
            and self.expires
            and now >= self.expires
        ):
            await self.finish("timeout")
            return
        if self.button_flag is not None:
            try:
                stamp = self.button_flag.stat().st_mtime
            except OSError:
                stamp = 0.0
            if stamp > self.button_seen:
                self.button_seen = stamp
                # Consume the edge-triggered marker so a service restart cannot reopen setup
                # mode from a stale file left by the last physical press.
                with contextlib.suppress(OSError):
                    self.button_flag.unlink()
                if self.state == ProvisioningState.IDLE:
                    try:
                        await self.enter("button")
                    except OpenLoloError as exc:
                        log.warning("setup mode not started (button): %s", exc.message)
                    return
        if self.state == ProvisioningState.IDLE and not self.grace_used:
            status = await self.network.status()
            saved = list(status.get("saved") or [])
            if not saved:
                await self._auto_enter("no saved network")
            elif not status.get("ssid") and now - self.boot >= self.config.setup_grace_seconds:
                await self._auto_enter("offline after boot")
            elif status.get("ssid"):
                self.grace_used = True  # Online once: never auto-enter again this boot.

    async def _auto_enter(self, reason: str) -> None:
        """Boot-time entry; a network failure is logged and retried on the next tick."""
        try:
            await self.enter(reason)
        except OpenLoloError as exc:
            log.warning("setup mode not started (%s): %s", reason, exc.message)
            return
        self.grace_used = True

    async def run(self, interval: float = 1.0) -> None:
        self.boot = self.clock()
        while True:
            with contextlib.suppress(Exception):
                await self.tick()
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self.task is None and self.state != ProvisioningState.DISABLED:
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        for task in (self.task, self.worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.task = self.worker = None
        with contextlib.suppress(Exception):
            await self.network.stop_setup_ap()
