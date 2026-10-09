"""Setup access point: hostapd and dnsmasq on the box's own radio, plus the setup credentials.

NetworkManager keeps station mode (joining the owner's Wi-Fi); the setup network is a root
systemd unit the API starts through polkit. hostapd is configured as plain WPA2-PSK before its
first beacon, so beacon and 4-way handshake agree on every Broadcom radio. (NetworkManager's
own AP mode always adds WPA-PSK-SHA256, which the BCM43430 firmware drops from its beacon and
phones then refuse as a downgrade; see docs/compatibility.md, 2026-10-06.)
"""

import asyncio
import logging
import re
import secrets
from pathlib import Path

from openlolo.domain.errors import OpenLoloError

log = logging.getLogger(__name__)

ADDRESS = "192.168.4.1/24"
UNIT = "openlolo-setup-ap.service"
SCAN_UNIT = "openlolo-wifi-scan.service"
HOSTAPD_CONF = "hostapd.conf"
DNSMASQ_CONF = "setup-dnsmasq.conf"
SCAN_FILE = "wifi-scan.txt"
CHANNEL = 6
AP_READY_SECONDS = 20
STATION_READY_SECONDS = 15


def _private_secret(path: Path, make, rotate=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not rotate:
        try:
            value = path.read_text().strip()
            if value:
                path.chmod(0o600)
                return value
        except OSError:
            pass
    value = make()
    fd = path.open("w")
    try:
        fd.write(value + "\n")
    finally:
        fd.close()
    path.chmod(0o600)
    return value


# Letters and digits that survive being read off a card and typed on a phone keyboard: no
# 0/o, 1/l/i or 5/s look-alikes. 16 symbols from 29 give ~78 bits, well past WPA2's PBKDF2 cost.
PASSWORD_ALPHABET = "abcdefghjkmnpqrtuvwxyz2346789"


def _setup_password() -> str:
    symbols = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(16))
    return "-".join(symbols[i : i + 4] for i in range(0, 16, 4))


def ensure_setup_identity(config, *, rotate_password=False) -> tuple[str, str]:
    """Persist a stable short board suffix and WPA2 passphrase for the physical setup card."""
    board = _private_secret(config.state_dir / "board-id", lambda: secrets.token_hex(4))[:8].lower()
    if config.setup_name is None:
        config.setup_name = f"OpenLolo-{board.upper()}"
    password = _private_secret(config.state_dir / "setup-ap-password", _setup_password, rotate_password)
    config.setup_ap_password = password
    return config.setup_name, password


def hostapd_config(interface: str, ssid: str, passphrase: str, country: str | None) -> str:
    lines = [
        f"interface={interface}",
        "driver=nl80211",
        f"ssid={ssid}",
        "hw_mode=g",
        f"channel={CHANNEL}",
        "ieee80211n=1",
        "wmm_enabled=1",
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
        # Plain WPA2-PSK, CCMP, no management-frame protection: the one configuration every
        # Broadcom firmware beacons exactly as configured (brcmfmac has no MFP in AP mode).
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        "ieee80211w=0",
        f"wpa_passphrase={passphrase}",
    ]
    if country:
        lines[5:5] = [f"country_code={country}", "ieee80211d=1"]
    return "\n".join(lines) + "\n"


def dnsmasq_config(interface: str) -> str:
    address, _, _ = ADDRESS.partition("/")
    prefix = address.rsplit(".", 1)[0]
    lines = [
        f"interface={interface}",
        "bind-dynamic",
        "except-interface=lo",
        "no-resolv",
        "no-hosts",
        "dhcp-authoritative",
        f"dhcp-range={prefix}.10,{prefix}.254,255.255.255.0,1h",
        f"dhcp-option=option:router,{address}",
        f"dhcp-option=option:dns-server,{address}",
        # Every name, including the captive-portal probe hosts, resolves to the box.
        f"address=/#/{address}",
    ]
    return "\n".join(lines) + "\n"


_SIGNAL = re.compile(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm")


def parse_iw_scan(text: str) -> list[dict]:
    """``iw dev <if> scan`` output as the portal's network entries, best signal per SSID."""
    seen: dict[str, dict] = {}
    for block in re.split(r"^BSS ", text, flags=re.MULTILINE)[1:]:
        ssid = None
        dbm = None
        security = ""
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("SSID: "):
                ssid = line[6:]
            elif line.startswith("signal:"):
                found = _SIGNAL.search(line)
                dbm = float(found.group(1)) if found else None
            elif line.startswith("RSN:"):
                security = "WPA2"
            elif line.startswith("WPA:") and not security:
                security = "WPA1"
        if not ssid or "\\x00" in ssid:
            continue
        signal = 0 if dbm is None else max(0, min(100, int((dbm + 100) * 2)))
        entry = {"ssid": ssid, "signal": signal, "security": security, "active": False}
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = entry
    return sorted(seen.values(), key=lambda e: -e["signal"])


class SetupAccessPoint:
    def __init__(self, config, runner):
        self.config, self.run = config, runner

    # -- state ----------------------------------------------------------------------

    async def active(self) -> bool:
        code, output = await self.run("systemctl", "is-active", UNIT, timeout=5)
        return code == 0 and output.strip() == "active"

    async def _country(self) -> str | None:
        code, output = await self.run("iw", "reg", "get", timeout=5)
        if code != 0:
            return None
        found = re.search(r"^country ([A-Z]{2}):", output, flags=re.MULTILINE)
        return found.group(1) if found else None

    async def _radio_mode(self) -> str | None:
        code, output = await self.run("iw", "dev", self.config.wifi_interface, "info", timeout=5)
        if code != 0:
            return None
        found = re.search(r"^\s*type (\S+)", output, flags=re.MULTILINE)
        return found.group(1) if found else None

    async def _station_state(self) -> str:
        code, output = await self.run(
            "nmcli", "-g", "GENERAL.STATE", "dev", "show", self.config.wifi_interface
        )
        return output.strip() if code == 0 else ""

    # -- lifecycle --------------------------------------------------------------------

    async def start(self):
        if not self.config.setup_name or not self.config.setup_ap_password:
            raise OpenLoloError("SETUP_AP_NOT_CONFIGURED", status=500)
        if await self.active():
            return
        runtime = Path(self.config.runtime_dir)
        country = await self._country()
        hostapd = hostapd_config(
            self.config.wifi_interface, self.config.setup_name, self.config.setup_ap_password, country
        )
        for name, text in (
            (HOSTAPD_CONF, hostapd),
            (DNSMASQ_CONF, dnsmasq_config(self.config.wifi_interface)),
        ):
            path = runtime / name
            path.touch(mode=0o640, exist_ok=True)
            path.chmod(0o640)
            path.write_text(text)
        code, output = await self.run("systemctl", "start", UNIT, timeout=30, quiet=False)
        if code != 0:
            raise OpenLoloError("SETUP_AP_FAILED", output or "Could not start the setup network", 502)
        for _ in range(AP_READY_SECONDS * 2):
            if await self._radio_mode() == "AP":
                return
            if not await self.active():
                break
            await asyncio.sleep(0.5)
        await self.run("systemctl", "stop", UNIT, timeout=15)
        raise OpenLoloError("SETUP_AP_FAILED", f"{UNIT} did not bring the radio into AP mode", 502)

    async def stop(self):
        await self.run("systemctl", "stop", UNIT, timeout=20)
        # The unit's ExecStopPost hands the radio back to NetworkManager; station commands
        # right after need the device managed again (state 10 is "unmanaged").
        for _ in range(STATION_READY_SECONDS * 2):
            state = await self._station_state()
            if state and not state.startswith("10"):
                return
            await asyncio.sleep(0.5)
        log.warning("setup AP: %s still holds the radio after %s s", UNIT, STATION_READY_SECONDS)

    async def scan(self) -> list[dict]:
        """Neighbouring networks while hostapd holds the radio (NetworkManager cannot scan then)."""
        code, output = await self.run("systemctl", "start", SCAN_UNIT, timeout=40, quiet=False)
        if code != 0:
            raise OpenLoloError("NETWORK_COMMAND_FAILED", output or "Wi-Fi scan failed", 502)
        try:
            text = (Path(self.config.runtime_dir) / SCAN_FILE).read_text()
        except OSError as exc:
            raise OpenLoloError("NETWORK_COMMAND_FAILED", f"no scan result: {exc}", 502) from None
        return [entry for entry in parse_iw_scan(text) if entry["ssid"] != self.config.setup_name]
