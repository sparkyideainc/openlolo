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
    await asyncio.sleep(0.05)  # Past the handoff delay; the join itself is still in flight.
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


async def test_pair_button_flag_opens_one_search_per_press(tmp_path):
    import os

    net = FakeNetwork(["home"])
    flag = tmp_path / "pair-button"
    searches = []

    async def search():
        searches.append(True)
        return {}

    prov = Provisioning(config(tmp_path), net, clock=Clock(), pair_flag=flag, search=search)
    await prov.tick()
    assert searches == []
    flag.touch()
    await prov.tick()
    await prov.tick()  # The same press is not replayed.
    assert searches == [True] and not flag.exists() and prov.state == ProvisioningState.IDLE
    flag.touch()
    stamp = prov.pair_seen + 5
    os.utime(flag, (stamp, stamp))
    await prov.tick()
    assert searches == [True, True]

    async def failing():
        raise OpenLoloError("WORKER_UNAVAILABLE")

    prov.search = failing
    flag.touch()
    os.utime(flag, (stamp + 5, stamp + 5))
    await prov.tick()  # Logged, never raised into the supervisor.


async def test_window_change_wakes_on_open_and_close(tmp_path):
    prov, _ = build(tmp_path, FakeNetwork(["home"]))
    waiter = asyncio.create_task(prov.window_change())
    await asyncio.sleep(0)
    assert not waiter.done()
    await prov.enter("api")
    await asyncio.wait_for(waiter, 1)
    waiter = asyncio.create_task(prov.window_change())
    await asyncio.sleep(0)
    await prov.exit()
    await asyncio.wait_for(waiter, 1)


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


def press(flag, seconds=[0.0]):
    """Touch a button flag with a strictly newer mtime than any earlier press in the test."""
    import os
    import time

    seconds[0] = max(seconds[0] + 1, time.time())
    flag.touch()
    os.utime(flag, (seconds[0], seconds[0]))


def button_rig(tmp_path, bound=True):
    clock = Clock()
    requests = []
    saved = [bound]

    async def search():
        requests.append("search")
        return {}

    prov = Provisioning(
        config(tmp_path),
        FakeNetwork(["home"]),
        clock=clock,
        button_flag=tmp_path / "setup-button",
        pair_flag=tmp_path / "pair-button",
        search=search,
        indicator=tmp_path / "indicator",
        bound=lambda: saved[0],
    )
    return prov, clock, requests


def shown(tmp_path):
    return (tmp_path / "indicator").read_text().strip()


async def test_hold_opens_local_mode_and_a_press_leaves_it(tmp_path):
    prov, clock, requests = button_rig(tmp_path)
    await prov.tick()
    assert shown(tmp_path) == "idle" and prov.health()["mode"] == "wifi"
    press(tmp_path / "setup-button")
    await prov.tick()
    assert prov.health()["mode"] == "local" and shown(tmp_path) == "setup"
    press(tmp_path / "pair-button")
    await prov.tick()
    assert prov.health()["mode"] == "wifi" and prov.reason == "button"
    assert shown(tmp_path) == "idle" and requests == []  # Leaving is not a search.
    press(tmp_path / "pair-button")
    await prov.tick()
    assert requests == ["search"] and prov.state == ProvisioningState.IDLE


async def test_setup_mode_ignores_the_press(tmp_path):
    prov, clock, requests = button_rig(tmp_path, bound=False)
    press(tmp_path / "setup-button")
    await prov.tick()
    assert prov.health()["mode"] == "setup" and shown(tmp_path) == "setup"
    press(tmp_path / "pair-button")
    await prov.tick()
    assert prov.health()["mode"] == "setup" and requests == []


async def test_hold_while_the_window_is_open_is_ignored(tmp_path):
    prov, clock, _ = button_rig(tmp_path)
    press(tmp_path / "setup-button")
    await prov.tick()
    expires = prov.expires
    clock.advance(30)
    press(tmp_path / "setup-button")
    await prov.tick()
    assert prov.state == ProvisioningState.SETUP_MODE and prov.expires == expires
    assert len(calls(prov.network, "start_setup_ap")) == 1


async def test_press_older_than_the_opening_hold_does_not_leave(tmp_path):
    import os

    prov, clock, requests = button_rig(tmp_path)
    press(tmp_path / "pair-button")
    stamp = (tmp_path / "pair-button").stat().st_mtime
    press(tmp_path / "setup-button")
    await prov.tick()  # Opens Local mode; the older press is read on the next tick.
    os.utime(tmp_path / "pair-button", (stamp, stamp))
    await prov.tick()
    assert prov.health()["mode"] == "local" and requests == []


async def test_press_leaves_local_mode_opened_from_the_page(tmp_path):
    prov, clock, _ = button_rig(tmp_path)
    await prov.enter("api")
    press(tmp_path / "pair-button")
    await prov.tick()
    assert prov.state == ProvisioningState.IDLE and not prov.window


async def test_buttons_are_ignored_during_a_join(tmp_path):
    prov, clock, requests = button_rig(tmp_path)
    press(tmp_path / "setup-button")
    await prov.tick()
    prov.state = ProvisioningState.JOINING
    press(tmp_path / "pair-button")
    await prov.tick()
    press(tmp_path / "setup-button")
    await prov.tick()
    assert prov.state == ProvisioningState.JOINING and prov.window and requests == []


async def test_status_led_follows_the_phone_connection(tmp_path):
    import time

    prov, clock, _ = button_rig(tmp_path)
    health = {"connection": {"state": "disconnected", "searching": False, "searched_at": 0.0}}

    async def link():
        if health is None:
            raise OSError("worker down")
        return health

    prov.link = link
    await prov.tick()
    assert shown(tmp_path) == "idle"
    health["connection"]["searching"] = True  # Any search, pressed or background.
    await prov.tick()
    assert shown(tmp_path) == "searching"
    health["connection"].update(searching=False, searched_at=time.time() - 1)
    await prov.tick()
    assert shown(tmp_path) == "searching"  # A one-second search still shows for a moment.
    health["connection"]["searched_at"] = time.time() - 10
    await prov.tick()
    assert shown(tmp_path) == "idle"
    health["connection"] = {"state": "connected", "searching": False}
    await prov.tick()
    assert shown(tmp_path) == "connected"
    press(tmp_path / "setup-button")
    await prov.tick()
    assert shown(tmp_path) == "setup"  # The window outranks the connection.
    press(tmp_path / "pair-button")
    await prov.tick()
    assert shown(tmp_path) == "connected"
    await prov.tick(network=False)  # Fast button ticks do not poll the worker.
    health = None
    await prov.tick()
    assert shown(tmp_path) == "idle"


async def test_lan_name_is_reclaimed_only_on_home_wifi_and_throttled(tmp_path):
    clock = Clock()
    net = FakeNetwork(["home"])
    prov = Provisioning(config(tmp_path), net, clock=clock)
    await prov.tick()
    await prov.tick()  # Within RECLAIM_SECONDS: no second check.
    assert calls(net, "reclaim_lan_name") == [("reclaim_lan_name", "openlolo-test")]
    await prov.enter("api")
    clock.advance(60)
    await prov.tick()  # The access point owns the radio: leave avahi alone.
    assert len(calls(net, "reclaim_lan_name")) == 1
    await prov.exit()
    await prov.tick()
    assert len(calls(net, "reclaim_lan_name")) == 2


async def test_reset_attempt_owns_the_button(tmp_path):
    from openlolo.application.reset import ResetChallenge

    prov, clock, requests = button_rig(tmp_path)
    events = tmp_path / "button-events"
    events.write_text("0 start s1\n")
    runs = []

    async def runner():
        runs.append(True)
        return False

    prov.reset = ResetChallenge(events, runner=runner, clock=clock, choose=lambda targets: 5)
    prov.reset.start()
    await prov.tick()
    assert shown(tmp_path) == "challenge"
    # Daemon order: flag first, log second. A press counts, never searches.
    press(tmp_path / "pair-button")
    events.write_text("0 start s1\n1 double 1.0\n2 double 1.0\n3 single 1.0\n")
    await prov.tick()
    clock.now += 2
    await prov.tick()
    assert shown(tmp_path) == "armed" and requests == []
    # The confirming hold never opens Local mode.
    press(tmp_path / "setup-button")
    events.write_text(events.read_text() + "4 long 1.0\n")
    await prov.tick()
    await prov.tick()
    assert runs == [True] and prov.health()["mode"] == "wifi" and shown(tmp_path) == "resetting"


async def test_hold_that_fails_a_challenge_is_not_a_local_mode_hold(tmp_path):
    from openlolo.application.reset import ResetChallenge

    prov, clock, requests = button_rig(tmp_path)
    events = tmp_path / "button-events"
    events.write_text("0 start s1\n")

    async def runner():
        return False

    prov.reset = ResetChallenge(events, runner=runner, clock=clock)
    prov.reset.start()
    press(tmp_path / "setup-button")
    events.write_text("0 start s1\n1 long 1.0\n")
    await prov.tick()
    await prov.tick()
    assert prov.reset.stage is None and prov.health()["mode"] == "wifi"
    assert shown(tmp_path) == "idle"
    press(tmp_path / "setup-button")  # After the attempt closed: an ordinary hold.
    await prov.tick()
    assert prov.health()["mode"] == "local"


def link_rig(tmp_path, bound=True):
    prov, clock, _ = button_rig(tmp_path, bound=bound)
    health = {"connection": {"state": "disconnected"}}

    async def link():
        if health.get("down"):
            raise OSError("worker down")
        return health

    prov.link = link
    return prov, clock, health


async def test_local_mode_holds_while_the_phone_is_connected(tmp_path):
    prov, clock, health = link_rig(tmp_path)
    await prov.enter("button")
    health["connection"]["state"] = "connected"
    await prov.tick()
    assert prov.health()["held"] and prov.health()["expires_in"] is None
    for _ in range(30):  # An hour connected, well past the 600 s timeout.
        clock.advance(120)
        await prov.tick()
    assert prov.health()["mode"] == "local" and prov.health()["held"]


async def test_local_mode_countdown_restarts_after_the_phone_drops(tmp_path):
    prov, clock, health = link_rig(tmp_path)
    await prov.enter("button")
    health["connection"]["state"] = "connected"
    clock.advance(500)
    await prov.tick()
    health["connection"]["state"] = "disconnected"
    clock.advance(1)
    await prov.tick()
    assert not prov.health()["held"] and prov.health()["expires_in"] == 599
    clock.advance(500)
    await prov.tick()
    health["connection"]["state"] = "connected"  # Back before expiry: held again.
    clock.advance(1)
    await prov.tick()
    assert prov.health()["held"] and prov.window
    health["connection"]["state"] = "disconnected"
    clock.advance(1)
    await prov.tick()
    clock.advance(598)
    await prov.tick()
    assert prov.window and prov.health()["expires_in"] == 1
    clock.advance(1)
    await prov.tick()
    assert not prov.window and prov.reason == "timeout" and prov.health()["mode"] == "wifi"


async def test_local_mode_without_a_connected_phone_expires_from_entry(tmp_path):
    prov, clock, health = link_rig(tmp_path)
    await prov.enter("button")
    clock.advance(599)
    await prov.tick()
    assert prov.window and not prov.health()["held"] and prov.health()["expires_in"] == 1
    clock.advance(1)
    await prov.tick()
    assert not prov.window and prov.reason == "timeout"


async def test_unreachable_worker_counts_as_not_connected(tmp_path):
    prov, clock, health = link_rig(tmp_path)
    await prov.enter("button")
    health["connection"]["state"] = "connected"
    await prov.tick()
    assert prov.health()["held"]
    health["down"] = True
    clock.advance(1)
    await prov.tick()
    assert not prov.health()["held"] and prov.health()["expires_in"] == 599
    clock.advance(599)
    await prov.tick()
    assert not prov.window and prov.reason == "timeout"


async def test_setup_mode_keeps_its_fixed_timeout(tmp_path):
    prov, clock, health = link_rig(tmp_path, bound=False)
    health["connection"]["state"] = "connected"
    await prov.enter("button")
    await prov.tick()
    assert prov.health()["mode"] == "setup" and not prov.health()["held"]
    clock.advance(600)
    await prov.tick()
    assert not prov.window and prov.reason == "timeout"


async def test_recovery_ap_keeps_its_fixed_timeout(tmp_path):
    prov, clock, health = link_rig(tmp_path)
    health["connection"]["state"] = "connected"
    prov.network.active = None  # No previous profile to restore: the recovery AP opens.
    await prov.connect("Nowhere", "x")
    await prov.worker
    assert prov.reason == "join failed; recovery AP" and prov.health()["mode"] == "local"
    await prov.tick()
    assert not prov.health()["held"]
    clock.advance(600)
    await prov.tick()
    assert not prov.window and prov.reason == "timeout"
