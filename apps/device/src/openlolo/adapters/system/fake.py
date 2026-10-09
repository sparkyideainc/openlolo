"""Deterministic network backend for the simulator and tests."""

import asyncio

from openlolo.domain.errors import OpenLoloError


class FakeNetwork:
    def __init__(self, saved: list[str] | None = None, known: dict[str, str] | None = None):
        self.profiles: list[str] = list(saved or [])  # names
        self.known = dict(known or {"HomeNet": "correct horse"})  # ssid -> psk that works
        self.active: str | None = self.profiles[0] if self.profiles else None
        self.calls: list[tuple] = []
        self.connect_delay = 0.0
        self.setup_ap_active = False

    async def start_setup_ap(self):
        self.calls.append(("start_setup_ap",))
        self.setup_ap_active = True

    async def stop_setup_ap(self):
        self.calls.append(("stop_setup_ap",))
        self.setup_ap_active = False

    async def health(self):
        return {
            "wifi": ["connected" if self.active else "disconnected"],
            "internet": "full" if self.active else "none",
            "pi_throttling": "throttled=0x0",
            "cpu_temperature_c": 42.0,
            "load_1min": 0.1,
            "internet_probe": "simulated",
        }

    async def status(self):
        return {
            "interface": "wlan0",
            "state": "100 (connected)" if self.active else "30 (disconnected)",
            "connection": self.active,
            "ip": "192.168.4.25" if self.active else None,
            "ssid": self.active,
            "signal": 70 if self.active else None,
            "internet": "full" if self.active else "none",
            "saved": list(self.profiles),
        }

    async def scan(self):
        self.calls.append(("scan",))
        return [{"ssid": s, "signal": 80, "security": "WPA2", "active": s == self.active} for s in self.known]

    async def saved(self):
        return [{"name": p, "mode": "infrastructure"} for p in self.profiles]

    async def connect(self, ssid, psk, name, timeout=45):
        self.calls.append(("connect", ssid, name))
        await self.stop_setup_ap()
        await asyncio.sleep(self.connect_delay)
        if ssid not in self.known:
            raise OpenLoloError("WIFI_NOT_FOUND", status=502)
        if self.known[ssid] != psk:
            raise OpenLoloError("WIFI_AUTH_FAILED", status=502)
        self.profiles.append(name)
        self.active = ssid

    async def forget(self, name, force=False):
        self.calls.append(("forget", name))
        if not name.startswith("openlolo-") and not force:
            raise OpenLoloError("PROFILE_PROTECTED", status=403)
        if name in self.profiles:
            self.profiles.remove(name)

    async def restore_connection(self, name):
        self.calls.append(("restore_connection", name))

    async def remove_legacy_hotspot(self):
        self.calls.append(("remove_legacy_hotspot",))
