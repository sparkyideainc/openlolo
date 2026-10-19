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

What the owner sees (``mode`` in health) follows from the window and the binding:

    wifi   window closed: the box is on its saved Wi-Fi (or offline)
    local  window open, a phone is saved: the box broadcasts its own Wi-Fi for that phone
    setup  window open, no phone saved: first-time setup

Buttons (Lolo Pocket top button: long press = hold, single press = press; Raspberry Pi: 5 s hold):

    wifi --hold--> local (setup when no phone is saved)   wifi --press--> one phone search
    local --press--> wifi                                 local, setup --hold--> ignored
    setup --press--> ignored                              joining --any--> ignored

A press opens the same search window the worker opens by itself (boot, cable pulled, phone
advertising, a request). Factory reset starts on the box page and is finished with the button
(application/reset.py); while a reset attempt is open the button belongs to it alone.

The status LED mirrors one word in ``indicator``, highest first: ``setup`` (window open, fast
blink), ``connected`` (phone connected, steady on), ``searching`` (a search window is open or
ended less than SEARCH_SHOW_SECONDS ago, slow blink), ``idle`` (off). An open reset attempt
from the page comes first: ``challenge``, ``armed``, ``resetting`` (application/reset.py).
"""

import asyncio
import contextlib
import enum
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger("openlolo.provisioning")

HANDOFF_READ_SECONDS = 6  # Captive sheet time to show the next address before the AP drops.
BUTTON_POLL_SECONDS = 0.2  # The daemon acts on release; a fast poll keeps the box's reply immediate.
SEARCH_SHOW_SECONDS = 3  # A search often ends within a second; keep the slow blink visible.
RECLAIM_SECONDS = 15  # How often, with the window closed, the mDNS host name is checked.


class ProvisioningState(str, enum.Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    SETUP_MODE = "setup_mode"
    JOINING = "joining"
    VERIFYING = "verifying"


def slug(ssid: str) -> str:
    return "openlolo-" + (re.sub(r"[^a-z0-9]+", "-", ssid.lower()).strip("-") or "wifi")[:24]


class Provisioning:
    def __init__(
        self,
        config,
        network,
        *,
        clock=time.monotonic,
        button_flag: Path | None = None,
        pair_flag: Path | None = None,
        search=None,
        indicator: Path | None = None,
        link=None,
        bound=None,
        reset=None,
    ):
        self.config, self.network = config, network
        self.setup_name = config.setup_name or "OpenLolo"
        self.clock = clock
        self.button_flag = button_flag
        # Top-button press (written by the button daemon): one phone search, or leaves Local mode.
        self.pair_flag, self.search = pair_flag, search
        self.bound = bound  # () -> whether a phone is saved; None counts as saved
        # The page's reset attempt (application/reset.py): while open it owns the button.
        self.reset = reset
        self.pair_seen = 0.0
        self.opened_stamp = 0.0  # mtime of the hold that opened setup; older presses never cancel
        # Status LED: ``indicator`` is the file the button daemon reads; ``link`` returns the
        # device health ({"connection": {"state", "searching"}}), polled about once a second.
        self.indicator, self.link = indicator, link
        self.link_word = "idle"
        self.shown: str | None = None
        host = urlsplit(config.lan_url).hostname if getattr(config, "lan_url", None) else None
        self.lan_name = host.removesuffix(".local") if host and host.endswith(".local") else None
        self.reclaim_at = 0.0
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
        self._window = False
        self._window_changed = asyncio.Event()

    # -- observation ---------------------------------------------------------------

    @property
    def window(self) -> bool:
        """Setup window open: the temporary Wi-Fi network is active."""
        return self._window

    @window.setter
    def window(self, value: bool) -> None:
        if value != self._window:
            self._window = value
            changed, self._window_changed = self._window_changed, asyncio.Event()
            changed.set()

    async def window_change(self) -> None:
        """Wait until the setup window opens or closes."""
        await self._window_changed.wait()

    @property
    def active(self) -> bool:
        """The setup window is open (also while a join started from it is in progress)."""
        return self.window

    @property
    def mode(self) -> str:
        if not self.window:
            return "wifi"
        return "local" if self._bound() else "setup"

    def _bound(self) -> bool:
        if self.bound is None:
            return True
        try:
            return bool(self.bound())
        except Exception:
            return True

    def health(self) -> dict:
        return {
            "state": self.state.value,
            "mode": self.mode,
            "since": self.since,
            "reason": self.reason,
            "expires_in": max(0, int(self.expires - self.clock())) if self.expires else None,
            "setup_name": self.setup_name if self.active else None,
            "last_error": self.last_error,
            "last_attempt": self.last_attempt,
        }

    def _show(self) -> None:
        """Write the indicator word when it changed. A failed write is logged and not retried:
        without the runtime directory there is no button daemon to read it."""
        if self.indicator is None:
            return
        word = (self.reset.indicator if self.reset is not None else None) or (
            "setup" if self.window else self.link_word
        )
        if word == self.shown:
            return
        try:
            temporary = self.indicator.with_name(f".{self.indicator.name}.tmp")
            temporary.write_text(word + "\n")
            temporary.replace(self.indicator)
        except OSError as exc:
            log.warning("indicator not written: %s", exc)
        self.shown = word

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
        self._show()

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
        self._show()

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

    async def tick(self, network: bool = True) -> None:
        """One supervisor step: timeouts, the physical button, and (``network``) boot/grace entry."""
        if self.state == ProvisioningState.DISABLED:
            return
        try:
            await self._tick(network)
        finally:
            self._show()

    async def _tick(self, network: bool) -> None:
        now = self.clock()
        if (
            self.window
            and self.state == ProvisioningState.SETUP_MODE
            and self.expires
            and now >= self.expires
        ):
            await self.finish("timeout")
            return
        # A reset attempt reads the button from the daemon's event log; the flags of the same
        # presses and holds are consumed unread so they never also open Local mode or search.
        if self.reset is not None and await self.reset.poll():
            self._drop_buttons()
        # Hold before press: a press read in the same tick as the opening hold waits a tick.
        elif await self._setup_button():
            return
        else:
            await self._pair_button()
        if network:
            await self._poll_link()
            await self._reclaim_lan_name(now)
        if network and self.state == ProvisioningState.IDLE and not self.grace_used:
            status = await self.network.status()
            saved = list(status.get("saved") or [])
            if not saved:
                await self._auto_enter("no saved network")
            elif not status.get("ssid") and now - self.boot >= self.config.setup_grace_seconds:
                await self._auto_enter("offline after boot")
            elif status.get("ssid"):
                self.grace_used = True  # Online once: never auto-enter again this boot.

    def _drop_buttons(self) -> None:
        for flag, seen in ((self.button_flag, "button_seen"), (self.pair_flag, "pair_seen")):
            if flag is None:
                continue
            try:
                setattr(self, seen, max(getattr(self, seen), flag.stat().st_mtime))
            except OSError:
                continue
            with contextlib.suppress(OSError):
                flag.unlink()

    async def _setup_button(self) -> bool:
        """A new mtime of the setup-button flag (a hold): open Local mode (Setup mode when no
        phone is saved). Returns whether the tick should end here."""
        if self.button_flag is None:
            return False
        try:
            stamp = self.button_flag.stat().st_mtime
        except OSError:
            return False
        if stamp <= self.button_seen:
            return False
        self.button_seen = stamp
        # Consume the edge-triggered marker so a service restart cannot reopen the window from a
        # stale file left by the last physical press.
        with contextlib.suppress(OSError):
            self.button_flag.unlink()
        if self.state == ProvisioningState.IDLE:
            try:
                await self.enter("button")
            except OpenLoloError as exc:
                log.warning("local mode not started (button): %s", exc.message)
                return True
            self.opened_stamp = stamp
            return True
        if self.state == ProvisioningState.SETUP_MODE:
            log.info("hold ignored: %s mode is already on", self.mode)
        else:
            log.info("hold ignored: a Wi-Fi join is in progress")
        return True

    async def _pair_button(self) -> None:
        """A new mtime of the pair-button flag (a press): in Local mode leave it, in Setup mode
        nothing, otherwise one phone search. The flag is consumed like the setup button's, so a
        restart never replays an old press; a failure is logged."""
        if self.pair_flag is None:
            return
        try:
            stamp = self.pair_flag.stat().st_mtime
        except OSError:
            return
        if stamp <= self.pair_seen:
            return
        self.pair_seen = stamp
        with contextlib.suppress(OSError):
            self.pair_flag.unlink()
        if self.state in {ProvisioningState.JOINING, ProvisioningState.VERIFYING}:
            log.info("press ignored: a Wi-Fi join is in progress")
            return
        if self.window:
            if stamp <= self.opened_stamp:
                return  # Pressed before the hold that opened the window: not a request to leave.
            if self.mode == "setup":
                log.info("press ignored: setup mode stays until a phone is paired")
                return
            log.info("local mode ended by a button press")
            await self.finish("button")
            return
        if self.search is None:
            return
        try:
            await self.search()
        except Exception as exc:
            log.warning("phone search not started (button): %s", getattr(exc, "code", type(exc).__name__))

    async def _reclaim_lan_name(self, now: float) -> None:
        """avahi renames the box after it sees its own records during a fast access point stop
        and start, and the box page stops resolving; take the name back while on the home Wi-Fi."""
        if self.lan_name is None or self.window or now < self.reclaim_at:
            return
        self.reclaim_at = now + RECLAIM_SECONDS
        reclaim = getattr(self.network, "reclaim_lan_name", None)
        if reclaim is None:
            return
        try:
            if await reclaim(self.lan_name):
                log.warning("mDNS host name taken back as %s.local after an avahi rename", self.lan_name)
        except Exception as exc:
            log.warning("mDNS host name not taken back: %s", getattr(exc, "message", exc))

    async def _poll_link(self) -> None:
        """Phone connection for the status LED; an unreachable worker shows as not connected."""
        if self.link is None:
            return
        try:
            connection = (await self.link()).get("connection") or {}
        except Exception:
            connection = {}
        if connection.get("state") == "connected":
            self.link_word = "connected"
        elif (
            connection.get("searching")
            or time.time() - float(connection.get("searched_at") or 0) < SEARCH_SHOW_SECONDS
        ):
            self.link_word = "searching"
        else:
            self.link_word = "idle"

    async def _auto_enter(self, reason: str) -> None:
        """Boot-time entry; a network failure is logged and retried on the next tick."""
        try:
            await self.enter(reason)
        except OpenLoloError as exc:
            log.warning("setup mode not started (%s): %s", reason, exc.message)
            return
        self.grace_used = True

    async def run(self, interval: float = BUTTON_POLL_SECONDS) -> None:
        """Buttons every ``interval``; the Wi-Fi status check (nmcli) about once a second."""
        self.boot = self.clock()
        every = max(1, round(1.0 / interval))
        n = 0
        while True:
            with contextlib.suppress(Exception):
                await self.tick(network=n % every == 0)
            n += 1
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
