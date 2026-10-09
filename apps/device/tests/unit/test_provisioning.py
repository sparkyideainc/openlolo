import asyncio

import pytest

from openlolo.adapters.system.fake import FakeNetwork
from openlolo.application.provisioning import Provisioning, ProvisioningState, slug
from openlolo.config import Config
from openlolo.domain.errors import OpenLoloError


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def config(tmp_path, **overrides):
    return Config(
        backend="simulated",
        state_dir=tmp_path,
        lan_url="https://openlolo-test.local:8443",
        setup_grace_seconds=120,
        setup_timeout_seconds=600,
        **overrides,
    )


def build(tmp_path, network, clock=None, **overrides):
    clock = clock or Clock()
    prov = Provisioning(config(tmp_path, **overrides), network, clock=clock)
    return prov, clock


def calls(network, name):
    return [c for c in network.calls if c[0] == name]


async def test_setup_mode_is_a_window_on_the_setup_ap(tmp_path):
    net = FakeNetwork()
    prov, _ = build(tmp_path, net)
    assert not prov.active
    await prov.enter("api")
    assert prov.active and net.setup_ap_active and calls(net, "start_setup_ap") == [("start_setup_ap",)]
    health = prov.health()
    assert health["setup_name"] == "OpenLolo-TEST" and health["expires_in"] == 600
    assert "psk" not in str(health).lower() and not (tmp_path / "setup.json").exists()
    await prov.enter("button")  # Re-entering only renews the window.
    assert prov.reason == "button" and prov.health()["expires_in"] == 600
    await prov.exit()
    assert prov.state == ProvisioningState.IDLE and not prov.active and not net.setup_ap_active
    assert prov.health()["setup_name"] is None and prov.health()["expires_in"] is None


async def test_boot_without_saved_network_enters_setup_mode_once(tmp_path):
    net = FakeNetwork()
    prov, clock = build(tmp_path, net)
    assert prov.state == ProvisioningState.IDLE and prov.setup_name == "OpenLolo-TEST"
    await prov.tick()
    assert prov.state == ProvisioningState.SETUP_MODE and prov.active
    assert prov.reason == "no saved network"
    clock.advance(10)
    await prov.tick()
    await prov.tick()
    assert calls(net, "start_setup_ap") == [("start_setup_ap",)]
    assert prov.health()["expires_in"] == 590


async def test_saved_but_offline_enters_after_grace_period(tmp_path):
    net = FakeNetwork(["home"])
    net.active = None
    prov, clock = build(tmp_path, net)
    await prov.tick()
    clock.advance(119)
    await prov.tick()
    assert prov.state == ProvisioningState.IDLE and not prov.active
    clock.advance(1)
    await prov.tick()
    assert prov.state == ProvisioningState.SETUP_MODE and prov.reason == "offline after boot"


async def test_online_box_never_auto_enters(tmp_path):
    net = FakeNetwork(["home"])
    prov, clock = build(tmp_path, net)
    for _ in range(3):
        await prov.tick()
        clock.advance(300)
    assert prov.state == ProvisioningState.IDLE and not prov.active
    assert not calls(net, "start_setup_ap")


async def test_timeout_closes_the_window(tmp_path):
    net = FakeNetwork()
    prov, clock = build(tmp_path, net)
    await prov.tick()
    clock.advance(599)
    await prov.tick()
    assert prov.active
    clock.advance(1)
    await prov.tick()
    assert prov.state == ProvisioningState.IDLE and prov.reason == "timeout" and not prov.active
    assert not net.setup_ap_active
    assert prov.health()["expires_in"] is None and prov.health()["setup_name"] is None
    await prov.tick()  # The boot-time entry is single-shot: no re-entry after a timeout.
    assert prov.state == ProvisioningState.IDLE and len(calls(net, "start_setup_ap")) == 1


async def test_connect_from_setup_mode_closes_the_window_on_success(tmp_path):
    net = FakeNetwork(["home"])
    net.active = None
    prov, clock = build(tmp_path, net)
    await prov.enter("api")
    clock.advance(500)
    attempt = await prov.connect("HomeNet", "correct horse")
    assert prov.worker is not None
    await prov.worker
    # The single radio left AP mode to join; the owner reads the result at the LAN address.
    assert prov.state == ProvisioningState.IDLE and prov.reason == "connected" and not prov.active
    assert prov.health()["expires_in"] is None
    assert prov.last_attempt == {
        "id": attempt,
        "ssid": "HomeNet",
        "started": prov.last_attempt["started"],
        "result": "connected",
        "profile": f"openlolo-homenet-{attempt}",
    }
    assert prov.last_error is None
    assert calls(net, "connect") == [("connect", "HomeNet", f"openlolo-homenet-{attempt}")]
    assert not calls(net, "forget")
    assert net.profiles == ["home", f"openlolo-homenet-{attempt}"] and net.active == "HomeNet"


async def test_wrong_psk_keeps_window_and_forgets_only_new_profile(tmp_path):
    net = FakeNetwork(["home"])
    net.active = None
    prov, _ = build(tmp_path, net)
    await prov.enter("api")
    attempt = await prov.connect("HomeNet", "wrong")
    await prov.worker
    assert prov.state == ProvisioningState.SETUP_MODE and prov.reason == "join failed" and prov.active
    assert prov.last_error["code"] == "WIFI_AUTH_FAILED"
    assert prov.last_attempt["result"] == "WIFI_AUTH_FAILED"
    assert calls(net, "forget") == [("forget", f"openlolo-homenet-{attempt}")]
    assert [c[0] for c in net.calls] == [
        "start_setup_ap",
        "connect",
        "stop_setup_ap",
        "forget",
        "start_setup_ap",
    ]
    assert net.profiles == ["home"] and net.setup_ap_active
    assert prov.health()["expires_in"] == 600


async def test_connect_from_idle_stays_idle_and_opens_no_window(tmp_path):
    net = FakeNetwork(["home"])
    prov, _ = build(tmp_path, net)
    await prov.connect("Nowhere", "x")
    await prov.worker
    assert prov.state == ProvisioningState.IDLE and prov.last_error["code"] == "WIFI_NOT_FOUND"
    assert not prov.active and not net.setup_ap_active  # The previous profile was restored.
    assert calls(net, "restore_connection") == [("restore_connection", "home")]
    await prov.connect("HomeNet", "correct horse")
    await prov.worker
    assert prov.state == ProvisioningState.IDLE and prov.reason == "connected" and not prov.active


async def test_connect_while_joining_is_busy(tmp_path):
    net = FakeNetwork(["home"])
    net.connect_delay = 0.2
    prov, _ = build(tmp_path, net)
    await prov.connect("HomeNet", "correct horse")
    await asyncio.sleep(1.05)  # Past the handoff delay; the join itself is still in flight.
    assert prov.state == ProvisioningState.JOINING
    with pytest.raises(OpenLoloError) as info:
        await prov.connect("HomeNet", "correct horse")
    assert info.value.code == "PROVISIONING_BUSY" and info.value.status == 409
    for coroutine in (prov.enter("api"), prov.exit()):
        with pytest.raises(OpenLoloError) as info:
            await coroutine
        assert info.value.code == "PROVISIONING_BUSY"
    await prov.worker
    assert prov.state == ProvisioningState.IDLE
    assert len(calls(net, "connect")) == 1


async def test_button_flag_opens_the_window(tmp_path):
    net = FakeNetwork(["home"])
    flag = tmp_path / "setup-button"
    clock = Clock()
    prov = Provisioning(config(tmp_path), net, clock=clock, button_flag=flag)
    await prov.tick()
    assert prov.state == ProvisioningState.IDLE
    flag.touch()
    await prov.tick()
    assert prov.state == ProvisioningState.SETUP_MODE and prov.reason == "button"
    await prov.exit()
    await prov.tick()  # The same press is not replayed.
    assert prov.state == ProvisioningState.IDLE
    import os

    flag.touch()
    stamp = flag.stat().st_mtime + 5
    os.utime(flag, (stamp, stamp))
    await prov.tick()
    assert prov.state == ProvisioningState.SETUP_MODE and len(calls(net, "start_setup_ap")) == 2


async def test_disabled_provisioning(tmp_path):
    net = FakeNetwork()
    prov, _ = build(tmp_path, net, provisioning=False)
    assert prov.state == ProvisioningState.DISABLED and not prov.active
    await prov.tick()
    assert prov.state == ProvisioningState.DISABLED and not net.calls
    for coroutine in (prov.enter("api"), prov.connect("HomeNet", "correct horse")):
        with pytest.raises(OpenLoloError) as info:
            await coroutine
        assert info.value.code == "PROVISIONING_DISABLED"
    await prov.exit()
    assert prov.state == ProvisioningState.DISABLED
    prov.start()
    assert prov.task is None


async def test_close_stops_the_setup_ap(tmp_path):
    net = FakeNetwork()
    prov, _ = build(tmp_path, net)
    await prov.enter("api")
    await prov.close()
    assert not net.setup_ap_active and prov.task is None and prov.worker is None


def test_profile_slug():
    assert slug("Home Net") == "openlolo-home-net"
    assert slug("Café:5G!") == "openlolo-caf-5g"
    assert slug("***") == "openlolo-wifi"
    assert len(slug("x" * 60)) == len("openlolo-") + 24
