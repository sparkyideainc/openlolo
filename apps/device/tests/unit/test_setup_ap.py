from types import SimpleNamespace

import pytest

from openlolo.adapters.system import setup_ap
from openlolo.adapters.system.setup_ap import SCAN_UNIT, UNIT, SetupAccessPoint, parse_iw_scan
from openlolo.domain.errors import OpenLoloError


class Runner:
    """Answers argv prefixes from a script; unknown commands succeed silently."""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls: list[tuple] = []

    async def __call__(self, *args, timeout=2, quiet=True):
        self.calls.append(args)
        for prefix, result in self.script.items():
            if args[: len(prefix)] == prefix:
                return result() if callable(result) else result
        return 0, ""

    def argv(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]


IW_INFO_AP = "Interface wlan0\n\tifindex 3\n\ttype AP\n\tchannel 6 (2437 MHz)\n"
IW_INFO_STA = "Interface wlan0\n\tifindex 3\n\ttype managed\n"
SCAN = """BSS 28:ec:22:2b:d3:e7(on wlan0)
\tfreq: 5180.0
\tsignal: -52.00 dBm
\tSSID: NETGEARZ
\tRSN:\t * Version: 1
BSS 28:ec:22:2b:d3:e6(on wlan0)
\tsignal: -70.00 dBm
\tSSID: NETGEARZ
\tRSN:\t * Version: 1
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tsignal: -40.00 dBm
\tSSID: OpenLolo-TEST
\tRSN:\t * Version: 1
BSS 11:22:33:44:55:66(on wlan0)
\tsignal: -80.50 dBm
\tSSID: Cafe
BSS 11:22:33:44:55:67(on wlan0)
\tsignal: -60.00 dBm
\tSSID: Legacy
\tWPA:\t * Version: 1
"""


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(setup_ap, "AP_READY_SECONDS", 1)
    monkeypatch.setattr(setup_ap, "STATION_READY_SECONDS", 1)


def access_point(tmp_path, script=None, radio_mode=IW_INFO_AP):
    config = SimpleNamespace(
        setup_name="OpenLolo-TEST",
        setup_ap_password="abcd-efgh-ijkl-mnop",
        wifi_interface="wlan0",
        runtime_dir=tmp_path,
    )
    state = {"active": False}

    def is_active():
        return (0, "active") if state["active"] else (3, "inactive")

    def start():
        state["active"] = True
        return 0, ""

    def stop():
        state["active"] = False
        return 0, ""

    runner = Runner(
        {
            ("systemctl", "is-active", UNIT): is_active,
            ("systemctl", "start", UNIT): start,
            ("systemctl", "stop", UNIT): stop,
            ("iw", "reg", "get"): (0, "global\ncountry US: DFS-FCC\n"),
            ("iw", "dev", "wlan0", "info"): (0, radio_mode),
            ("nmcli", "-g", "GENERAL.STATE", "dev", "show", "wlan0"): (0, "30 (disconnected)"),
            **(script or {}),
        }
    )
    return SetupAccessPoint(config, runner), runner


async def test_start_writes_plain_wpa2_configs_and_starts_the_unit(tmp_path):
    ap, runner = access_point(tmp_path)
    await ap.start()
    hostapd = (tmp_path / "hostapd.conf").read_text()
    assert "wpa_key_mgmt=WPA-PSK\n" in hostapd and "ieee80211w=0\n" in hostapd
    assert "rsn_pairwise=CCMP\n" in hostapd and "wpa_passphrase=abcd-efgh-ijkl-mnop\n" in hostapd
    assert "ssid=OpenLolo-TEST\n" in hostapd and "country_code=US\n" in hostapd
    assert (tmp_path / "hostapd.conf").stat().st_mode & 0o777 == 0o640
    dnsmasq = (tmp_path / "setup-dnsmasq.conf").read_text()
    assert "address=/#/192.168.4.1\n" in dnsmasq and "dhcp-range=192.168.4.10,192.168.4.254" in dnsmasq
    assert runner.argv("systemctl", "start", UNIT) == [("systemctl", "start", UNIT)]
    assert await ap.active()


async def test_start_without_a_regulatory_country_omits_it(tmp_path):
    ap, _ = access_point(tmp_path, {("iw", "reg", "get"): (0, "global\ncountry 00: DFS-UNSET\n")})
    await ap.start()
    assert "country_code" not in (tmp_path / "hostapd.conf").read_text()


async def test_start_is_idempotent_while_the_unit_runs(tmp_path):
    ap, runner = access_point(tmp_path)
    await ap.start()
    await ap.start()
    assert len(runner.argv("systemctl", "start", UNIT)) == 1


async def test_start_fails_and_stops_the_unit_when_the_radio_never_enters_ap_mode(tmp_path):
    ap, runner = access_point(tmp_path, radio_mode=IW_INFO_STA)
    with pytest.raises(OpenLoloError) as info:
        await ap.start()
    assert info.value.code == "SETUP_AP_FAILED" and "AP mode" in info.value.message
    assert runner.argv("systemctl", "stop", UNIT)


async def test_start_reports_the_units_error(tmp_path):
    failure = (1, "Job for openlolo-setup-ap.service failed")
    ap, _ = access_point(tmp_path, {("systemctl", "start", UNIT): failure})
    with pytest.raises(OpenLoloError) as info:
        await ap.start()
    assert "failed" in info.value.message


async def test_stop_waits_until_networkmanager_manages_the_radio_again(tmp_path):
    states = iter(["10 (unmanaged)", "30 (disconnected)"])
    script = {("nmcli", "-g", "GENERAL.STATE", "dev", "show", "wlan0"): lambda: (0, next(states))}
    ap, runner = access_point(tmp_path, script)
    await ap.start()
    await ap.stop()
    assert runner.argv("systemctl", "stop", UNIT) and len(runner.argv("nmcli", "-g")) == 2
    assert not await ap.active()


async def test_scan_runs_the_scan_unit_and_parses_iw_output(tmp_path):
    ap, runner = access_point(tmp_path)
    (tmp_path / "wifi-scan.txt").write_text(SCAN)
    networks = await ap.scan()
    assert runner.argv("systemctl", "start", SCAN_UNIT)
    assert [n["ssid"] for n in networks] == ["NETGEARZ", "Legacy", "Cafe"]  # own AP filtered
    assert networks[0] == {"ssid": "NETGEARZ", "signal": 96, "security": "WPA2", "active": False}
    assert networks[1]["security"] == "WPA1" and networks[2]["security"] == ""


def test_parse_iw_scan_keeps_the_strongest_bss_per_ssid_and_skips_hidden():
    hidden = "BSS 00:00:00:00:00:01(on wlan0)\n\tsignal: -30.00 dBm\n\tSSID: \\x00\\x00\n"
    result = parse_iw_scan(SCAN + hidden)
    assert [n["ssid"] for n in result][:1] == ["OpenLolo-TEST"]
    assert all("\\x00" not in n["ssid"] for n in result)
