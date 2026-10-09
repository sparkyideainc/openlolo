import pytest

from openlolo.adapters.system.network import LEGACY_HOTSPOT, NetworkManager
from openlolo.domain.errors import OpenLoloError


class Runner:
    """Records nmcli argv and answers from a prefix-keyed script; unknown commands succeed silently."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []

    async def __call__(self, *args, timeout=2, quiet=True):
        self.calls.append(args)
        self.kwargs.append({"timeout": timeout, "quiet": quiet})
        for prefix, result in self.script.items():
            if args[: len(prefix)] == prefix:
                return result
        return 0, ""

    def argv(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]


def manager(script=None):
    runner = Runner(script)
    return NetworkManager("wlan0", runner=runner), runner


async def test_connect_adds_profile_then_brings_it_up():
    net, runner = manager()
    await net.connect("Home Net", "pw:secret", "openlolo-home-net")
    assert runner.calls == [
        (
            "nmcli",
            "con",
            "add",
            "type",
            "wifi",
            "ifname",
            "wlan0",
            "con-name",
            "openlolo-home-net",
            "ssid",
            "Home Net",
            "connection.autoconnect",
            "yes",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            "pw:secret",
        ),
        ("nmcli", "-w", "45", "con", "up", "openlolo-home-net"),
    ]
    assert runner.kwargs[1]["timeout"] == 50 and runner.kwargs[1]["quiet"] is False


async def test_connect_open_network_and_prefix_guard():
    net, runner = manager()
    await net.connect("Open", "", "openlolo-open")
    assert "wifi-sec.psk" not in runner.calls[0] and "wifi-sec.key-mgmt" not in runner.calls[0]
    with pytest.raises(OpenLoloError) as info:
        await net.connect("Home", "pw", "home")
    assert info.value.code == "INVALID_REQUEST" and len(runner.calls) == 2


@pytest.mark.parametrize(
    "output,code",
    [
        ("Error: Connection activation failed: Secrets were required, but not provided.", "WIFI_AUTH_FAILED"),
        ("Error: No network with SSID 'Home' found.", "WIFI_NOT_FOUND"),
        ("Error: Timeout expired (45 seconds)", "WIFI_TIMEOUT"),
    ],
)
async def test_connect_failure_deletes_profile_and_maps_error(output, code):
    net, runner = manager({("nmcli", "-w"): (4, output)})
    with pytest.raises(OpenLoloError) as info:
        await net.connect("Home", "pw", "openlolo-home")
    assert info.value.code == code and info.value.status == 502
    assert runner.calls[-1] == ("nmcli", "con", "delete", "openlolo-home")
    assert "pw" not in info.value.message


async def test_forget_protects_foreign_profiles():
    net, runner = manager()
    with pytest.raises(OpenLoloError) as info:
        await net.forget("netplan-wlan0-X")
    assert info.value.code == "PROFILE_PROTECTED" and info.value.status == 403
    assert runner.calls == []
    await net.forget("netplan-wlan0-X", force=True)
    assert runner.calls == [("nmcli", "con", "delete", "netplan-wlan0-X")]
    await net.forget("openlolo-home")
    assert runner.calls[-1] == ("nmcli", "con", "delete", "openlolo-home")
    assert len(runner.calls) == 2


async def test_legacy_hotspot_profile_is_removed_quietly():
    """A box upgraded from a SoftAP release still has openlolo-setup; deleting it must never fail."""
    net, runner = manager({("nmcli", "con", "delete"): (10, "Error: unknown connection")})
    await net.remove_legacy_hotspot()
    assert runner.calls == [("nmcli", "con", "delete", LEGACY_HOTSPOT)]
    assert not hasattr(net, "start_hotspot") and not hasattr(net, "stop_hotspot")


async def test_status_parses_escaped_nmcli_output():
    net, runner = manager(
        {
            ("nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show"): (
                0,
                "GENERAL.STATE:100 (connected)\nGENERAL.CONNECTION:openlolo-caf-net\n"
                "IP4.ADDRESS[1]:192.168.4.25/24\nIP4.ADDRESS[2]:fe80::1/64",
            ),
            ("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list"): (
                0,
                "no:Home:55\nyes:Caf\\:Net:72\nno:Back\\\\slash:10",
            ),
            ("nmcli", "-t", "-f", "CONNECTIVITY", "general"): (0, "full"),
            ("nmcli", "-t", "-f", "NAME,TYPE", "con", "show"): (
                0,
                "Wired connection 1:802-3-ethernet\nhome:802-11-wireless\n"
                "openlolo-caf-net:802-11-wireless\nlo:loopback",
            ),
        }
    )
    status = await net.status()
    assert not runner.argv("nmcli", "-t", "-f", "802-11-wireless.ssid")  # An ACTIVE row needs no fallback.
    assert status == {
        "interface": "wlan0",
        "state": "100 (connected)",
        "connection": "openlolo-caf-net",
        "ip": "192.168.4.25",
        "ssid": "Caf:Net",
        "signal": 72,
        "internet": "full",
        "saved": ["home", "openlolo-caf-net"],
    }


async def test_status_tolerates_nmcli_failures():
    net, _ = manager({("nmcli",): (-1, "")})
    status = await net.status()
    assert status["state"] == "unknown" and status["ip"] is None and status["ssid"] is None
    assert status["internet"] == "unknown" and status["saved"] == [] and "hotspot" not in status


async def test_status_falls_back_to_profile_ssid_without_active_row():
    net, runner = manager(
        {
            ("nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show"): (
                0,
                "GENERAL.STATE:100 (connected)\nGENERAL.CONNECTION:openlolo-home\nIP4.ADDRESS[1]:192.168.4.25/24",
            ),
            ("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list"): (
                0,
                "no:Home:55\nno:Other:40",
            ),
            ("nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", "openlolo-home"): (
                0,
                "802-11-wireless.ssid:Home\\:Net",
            ),
            ("nmcli", "-t", "-f", "NAME,TYPE", "con", "show"): (0, "openlolo-home:802-11-wireless"),
        }
    )
    status = await net.status()
    assert (
        status["ssid"] == "Home\\:Net"
        and status["signal"] is None
        and status["connection"] == "openlolo-home"
    )
    assert runner.argv("nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show") == [
        ("nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", "openlolo-home")
    ]
    # Not connected (state 30): no profile lookup, no SSID.
    net, runner = manager(
        {
            ("nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show"): (
                0,
                "GENERAL.STATE:30 (disconnected)\nGENERAL.CONNECTION:openlolo-home",
            )
        }
    )
    status = await net.status()
    assert status["ssid"] is None and not runner.argv("nmcli", "-t", "-f", "802-11-wireless.ssid")


async def test_saved_hides_the_legacy_hotspot_profile():
    net, runner = manager(
        {
            ("nmcli", "-t", "-f", "NAME,TYPE", "con", "show"): (
                0,
                f"home:802-11-wireless\n{LEGACY_HOTSPOT}:802-11-wireless\neth0:802-3-ethernet",
            )
        }
    )
    assert await net.saved() == [{"name": "home", "mode": "infrastructure"}]
    assert runner.calls == [("nmcli", "-t", "-f", "NAME,TYPE", "con", "show")]


async def test_scan_dedupes_by_ssid_keeping_best_signal(monkeypatch):
    import openlolo.adapters.system.network as module

    async def instant(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", instant)
    net, runner = manager(
        {
            ("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"): (
                0,
                "no:Home:40:WPA2\nyes:Caf\\:Net:70:WPA2 WPA3\nno:Home:85:WPA2\nno::30:\nno:Open:20:",
            )
        }
    )
    networks = await net.scan()
    assert runner.calls == [
        (
            "nmcli",
            "-t",
            "-f",
            "ACTIVE,SSID,SIGNAL,SECURITY",
            "dev",
            "wifi",
            "list",
            "ifname",
            "wlan0",
            "--rescan",
            "yes",
        )
    ]
    assert runner.kwargs[0]["timeout"] == 20
    assert networks == [
        {"ssid": "Home", "signal": 85, "security": "WPA2", "active": False},
        {"ssid": "Caf:Net", "signal": 70, "security": "WPA2 WPA3", "active": True},
        {"ssid": "Open", "signal": 20, "security": "", "active": False},
    ]
