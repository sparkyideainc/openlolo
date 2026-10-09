"""System health, NetworkManager Wi-Fi station control and setup AP lifecycle through ``nmcli``.

Writes need the polkit rule in ``deploy/polkit/50-openlolo.rules`` for the service user; the
API unit itself keeps its sandbox. Passwords are passed to ``nmcli`` as arguments (briefly
visible to other local users in the process list on a multi-user box) and never logged.
Setup mode uses a temporary NetworkManager shared-mode access point; station joins stop it.
"""

import asyncio
import os
import re
from pathlib import Path

from openlolo.adapters.system.setup_ap import SetupAccessPoint
from openlolo.domain.errors import OpenLoloError

PREFIX = "openlolo-"
LEGACY_HOTSPOT = "openlolo-setup"  # Profile left behind by releases with SoftAP setup mode.


async def run(*args, timeout: float = 2, quiet: bool = True) -> tuple[int, str]:
    """Run a command; return (returncode, stdout). Timeouts and missing binaries return -1."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL if quiet else asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode or 0, output.decode("utf-8", "replace").strip()
    except (OSError, TimeoutError):
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        return -1, ""


async def command(*args):
    code, output = await run(*args)
    return output if code == 0 else "unknown"


def _unescape(field: str) -> str:
    return field.replace("\\:", ":").replace("\\\\", "\\")


def _split(line: str) -> list[str]:
    return [_unescape(f) for f in re.split(r"(?<!\\):", line)]


class SystemHealth:
    async def health(self):
        wifi, internet, thermal = await asyncio.gather(
            command("nmcli", "-t", "-f", "TYPE,STATE", "device"),
            command("nmcli", "-t", "-f", "CONNECTIVITY", "general"),
            command("vcgencmd", "get_throttled"),
        )
        wifi_states = [line.split(":", 1)[1] for line in wifi.splitlines() if line.startswith("wifi:")]
        try:
            temperature = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            temperature = None
        return {
            "wifi": wifi_states or ["unknown"],
            "internet": internet,
            # vcgencmd exists on Raspberry Pi OS only. Keep the old field for clients while
            # exposing the hardware-neutral name for ARM64 Linux hosts.
            "pi_throttling": thermal,
            "hardware_throttling": thermal if thermal != "unknown" else None,
            "cpu_temperature_c": temperature,
            "load_1min": os.getloadavg()[0],
            "internet_probe": "NetworkManager cached connectivity; no independent HTTPS probe",
        }


class NetworkManager(SystemHealth):
    """Wi-Fi station control for one interface."""

    def __init__(self, interface: str = "wlan0", runner=run):
        self.interface = interface
        self.run = runner
        self.setup_ap = None

    def configure_setup_ap(self, config):
        self.setup_ap = SetupAccessPoint(config, self.run)

    async def start_setup_ap(self):
        if self.setup_ap is not None:
            await self.setup_ap.start()

    async def stop_setup_ap(self):
        if self.setup_ap is not None:
            await self.setup_ap.stop()

    async def _nmcli(self, *args, timeout: float = 10) -> str:
        code, output = await self.run("nmcli", *args, timeout=timeout, quiet=False)
        if code != 0:
            raise OpenLoloError(
                "NETWORK_COMMAND_FAILED", output.splitlines()[-1] if output else "nmcli failed", 502
            )
        return output

    async def status(self) -> dict:
        info = {}
        try:
            for line in (
                await self._nmcli(
                    "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show", self.interface
                )
            ).splitlines():
                key, _, value = line.partition(":")
                info[key] = value
        except OpenLoloError:
            pass
        active = None
        try:
            for line in (
                await self._nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "no")
            ).splitlines():
                fields = _split(line)
                if len(fields) >= 3 and fields[0] == "yes":
                    active = {"ssid": fields[1], "signal": int(fields[2] or 0)}
        except (OpenLoloError, ValueError):
            pass
        code, connectivity = await self.run("nmcli", "-t", "-f", "CONNECTIVITY", "general")
        try:
            saved = [p["name"] for p in await self.saved()]
        except OpenLoloError:
            saved = []
        connection = info.get("GENERAL.CONNECTION") or None
        if active is None and connection and info.get("GENERAL.STATE", "").startswith("100"):
            # Some drivers report no ACTIVE access point; fall back to the profile's SSID.
            ssid = await self._profile_ssid(connection)
            active = {"ssid": ssid, "signal": None} if ssid else None
        return {
            "interface": self.interface,
            "state": info.get("GENERAL.STATE", "unknown"),
            "connection": connection,
            "ip": (info.get("IP4.ADDRESS[1]") or "").split("/")[0] or None,
            "ssid": active["ssid"] if active else None,
            "signal": active["signal"] if active else None,
            "internet": connectivity if code == 0 else "unknown",
            "saved": saved,
        }

    async def scan(self) -> list[dict]:
        """Fresh access-point list; ``--rescan yes`` waits for NetworkManager's scan to finish.

        A bare ``rescan`` returns before the radio has finished, so listing shortly after it
        showed only the associated network. When NetworkManager refuses a scan (one just ran),
        nmcli falls back to its cache, which is then recent anyway.
        """
        if self.setup_ap is not None and await self.setup_ap.active():
            return await self.setup_ap.scan()
        listing = ("-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", self.interface)
        code, output = await self.run("nmcli", *listing, "--rescan", "yes", timeout=20, quiet=False)
        if code != 0:
            output = await self._nmcli(*listing, "--rescan", "no")
        seen: dict[str, dict] = {}
        setup_ssid = self.setup_ap.config.setup_name if self.setup_ap is not None else None
        for line in output.splitlines():
            fields = _split(line)
            if len(fields) < 4 or not fields[1] or fields[1] == setup_ssid:
                continue
            entry = {
                "ssid": fields[1],
                "signal": int(fields[2] or 0),
                "security": fields[3],
                "active": fields[0] == "yes",
            }
            if fields[1] not in seen or entry["signal"] > seen[fields[1]]["signal"]:
                seen[fields[1]] = entry
        return sorted(seen.values(), key=lambda e: -e["signal"])

    async def saved(self) -> list[dict]:
        result = []
        for line in (await self._nmcli("-t", "-f", "NAME,TYPE", "con", "show")).splitlines():
            fields = _split(line)
            if len(fields) >= 2 and fields[1] == "802-11-wireless" and fields[0] != LEGACY_HOTSPOT:
                result.append({"name": fields[0], "mode": "infrastructure"})
        return result

    async def _profile_ssid(self, name: str) -> str | None:
        try:
            output = await self._nmcli("-t", "-f", "802-11-wireless.ssid", "con", "show", name)
        except OpenLoloError:
            return None
        return output.partition(":")[2] or None

    async def connect(self, ssid: str, psk: str, name: str, timeout: float = 45) -> None:
        if not name.startswith(PREFIX):
            raise OpenLoloError("INVALID_REQUEST", "profile names must carry the openlolo- prefix", 400)
        # Raspberry Pi's onboard radio is operated in one mode at a time. Drop the AP (hostapd
        # hands the radio back to NetworkManager) before switching to station mode;
        # Provisioning restores it on any failed attempt.
        await self.stop_setup_ap()
        args = [
            "con",
            "add",
            "type",
            "wifi",
            "ifname",
            self.interface,
            "con-name",
            name,
            "ssid",
            ssid,
            "connection.autoconnect",
            "yes",
        ]
        if psk:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        await self._nmcli(*args)
        try:
            await self._nmcli("-w", str(int(timeout)), "con", "up", name, timeout=timeout + 5)
        except OpenLoloError as exc:
            await self.run("nmcli", "con", "delete", name, timeout=5)
            message = exc.message.lower()
            code = (
                "WIFI_AUTH_FAILED"
                if "secrets" in message or "auth" in message
                else "WIFI_NOT_FOUND"
                if "no network" in message or "not found" in message
                else "WIFI_TIMEOUT"
            )
            raise OpenLoloError(code, exc.message, 502) from None

    async def forget(self, name: str, force: bool = False) -> None:
        if not name.startswith(PREFIX) and not force:
            raise OpenLoloError(
                "PROFILE_PROTECTED", "Only profiles created by OpenLolo can be removed without force", 403
            )
        await self._nmcli("con", "delete", name)

    async def restore_connection(self, name: str) -> None:
        """Bring the previously active station profile back after a failed LAN reconfiguration."""
        if name == LEGACY_HOTSPOT:
            return
        await self._nmcli("-w", "30", "con", "up", name, timeout=35)

    async def remove_legacy_hotspot(self) -> None:
        """Delete the SoftAP profile a pre-ADR-0006 release may have left; harmless when absent."""
        await self.run("nmcli", "con", "delete", LEGACY_HOTSPOT, timeout=5)
