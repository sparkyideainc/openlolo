"""Box page listener: bootstrap routes, cable-gated sessions, and the owner table behind them."""

import contextlib

import httpx
import pytest

from openlolo.interfaces import setup_portal
from openlolo.interfaces.setup_portal import CLIENT_HEADER, SESSION_COOKIE, create_setup_portal

PHONE = ("192.168.1.77", 50000)  # The simulated phone's RemotePairing address.
LAPTOP = ("192.168.1.20", 50000)
WRITE = {CLIENT_HEADER: "portal"}


@pytest.fixture
def web(tmp_path):
    build = tmp_path / "portal"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<!doctype html><title>OpenLolo</title><div id=app></div>")
    (build / "assets" / "index-abc123.js").write_text("console.log('ok')")
    (build / "favicon.png").write_bytes(b"\x89PNG")
    return build


@pytest.fixture
def clock():
    class Clock:
        now = 1_000_000.0

        def __call__(self):
            return self.now

    return Clock()


@pytest.fixture
def portal(runtime, web, clock):
    return create_setup_portal(runtime, web=web, clock=clock)


@contextlib.asynccontextmanager
async def browser(app, client=PHONE, base_url="http://openlolo-test.local"):
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as c:
        yield c


def ap(app):
    return browser(app, client=("192.168.4.25", 50000), base_url="http://192.168.4.1")


def unplug(runtime):
    runtime.phone.device.cabled = []
    runtime.phone._usb_cache = None


async def test_page_assets_and_fallback(portal):
    async with browser(portal) as c:
        home = await c.get("/")
        assert home.status_code == 200 and "id=app" in home.text
        assert home.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in home.headers["content-security-policy"]
        assert (await c.get("/access?r=abc")).text == home.text  # SPA route
        probe = await c.get("/hotspot-detect.html")  # Captive probe: never a sheet (ADR 0011)
        assert probe.status_code == 200 and probe.text == setup_portal.APPLE_SUCCESS
        assert (await c.get("/generate_204")).status_code == 204
        asset = await c.get("/assets/index-abc123.js")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        assert (await c.get("/favicon.png")).content == b"\x89PNG"
        assert (await c.get("/missing.png")).text == home.text  # Unknown file name: the page


async def test_missing_build_is_reported(runtime, tmp_path):
    async with browser(create_setup_portal(runtime, web=tmp_path / "nope")) as c:
        response = await c.get("/")
        assert response.status_code == 503 and "not built" in response.text


async def test_status_is_local_only(portal, runtime):
    async with browser(portal, client=LAPTOP) as c:
        status = (await c.get("/setup/status")).json()
        assert status["hostname"] == f"{runtime.config.setup_name.lower()}.local"
        assert status["bound"] is True and status["session"] is False
        assert status["access"] == {"cable": True, "setup_ap": False, "paired_phone": False, "allowed": False}
        assert status["usb_pairing"]["state"] in {"idle", "off"}
    async with browser(portal, client=("8.8.8.8", 1234)) as c:
        assert (await c.get("/setup/status")).status_code == 403


async def test_paired_phone_on_the_cable_opens_the_page(portal, runtime):
    async with browser(portal) as c:
        status = (await c.get("/setup/status")).json()
        assert status["access"]["paired_phone"] is True and status["access"]["allowed"] is True
        assert (await c.get("/app/status")).status_code == 200  # Allowed even before the cookie
        assert (await c.post("/setup/session", headers=WRITE)).status_code == 200
        assert (await c.post("/setup/session")).status_code == 403  # No client header
        opened = await c.post("/setup/session", headers=WRITE)
        assert opened.status_code == 200 and SESSION_COOKIE in opened.cookies
        cookie = opened.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
        assert (await c.get("/setup/status")).json()["session"] is True
        status = await c.get("/app/status")
        assert (
            status.status_code == 200
            and "connection" in status.json()
            and "pending_consents" in status.json()
        )
        bare = await c.post("/app/pause", json={})
        assert bare.status_code == 403 and bare.json()["error"]["code"] == "CLIENT_HEADER_REQUIRED"
        assert (await c.post("/app/pause", json={}, headers=WRITE)).status_code == 200
        clients = (await c.get("/app/clients")).json()["clients"]
        assert all(not row["self"] for row in clients)  # The page is not a client of the box
        closed = await c.request("DELETE", "/setup/session", headers=WRITE)
        assert closed.status_code == 200
        assert (await c.get("/setup/status")).json()["session"] is False


async def test_other_lan_devices_are_refused(portal, runtime):
    async with browser(portal, client=LAPTOP) as c:
        refused = await c.post("/setup/session", headers=WRITE)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "PHONE_REQUIRED"
        owner = await c.get("/app/status")
        assert owner.status_code == 401 and owner.json()["error"]["code"] == "LOGIN_REQUIRED"
        assert (await c.post("/app/pause", json={}, headers=WRITE)).status_code == 401
        assert (await c.get("/setup/scan")).status_code == 403


async def test_the_phone_must_be_on_the_cable(portal, runtime):
    unplug(runtime)
    async with browser(portal) as c:
        status = (await c.get("/setup/status")).json()
        assert status["access"]["cable"] is False and status["access"]["allowed"] is False
        refused = await c.post("/setup/session", headers=WRITE)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "CABLE_REQUIRED"


async def test_reconfiguration_needs_the_cable_but_pause_does_not(portal, runtime):
    async with browser(portal) as c:
        await c.post("/setup/session", headers=WRITE)
        unplug(runtime)
        assert (await c.get("/app/status")).status_code == 200  # Reading keeps working
        assert (await c.post("/app/pause", json={}, headers=WRITE)).status_code == 200
        scan = await c.post("/app/network/scan", json={}, headers=WRITE)
        assert scan.status_code == 403 and scan.json()["error"]["code"] == "CABLE_REQUIRED"


async def test_phone_address_is_refreshed_on_a_miss(portal, runtime):
    runtime.phone.device.addresses = ["192.168.1.90"]
    async with browser(portal, client=("192.168.1.90", 50000)) as c:
        assert (await c.get("/setup/status")).json()["access"]["allowed"] is True
    runtime.phone.device.addresses = ["192.168.1.91"]  # DHCP moved the phone
    async with browser(portal, client=("192.168.1.91", 50000)) as c:
        assert (await c.get("/setup/status")).json()["access"]["allowed"] is True  # Refreshed on miss


async def test_session_expires(portal, runtime, clock):
    async with browser(portal) as c:
        await c.post("/setup/session", headers=WRITE)
        assert (await c.get("/setup/status")).json()["session"] is True
        clock.now += setup_portal.SESSION_SECONDS + 1
        assert (await c.get("/setup/status")).json()["session"] is False


async def test_setup_ap_clients_need_only_the_cable(portal, runtime):
    await runtime.provisioning.enter(reason="test")
    async with ap(portal) as c:
        status = (await c.get("/setup/status")).json()
        assert status["setup_mode"] is True and status["access"]["setup_ap"] is True
        assert status["access"]["allowed"] is True
        assert (await c.get("/app/status")).status_code == 200  # Association plus cable is the proof
        opened = await c.post("/setup/session", headers=WRITE)  # Cookie for the rest of the setup
        assert opened.status_code == 200 and SESSION_COOKIE in opened.cookies
        scan = await c.get("/setup/scan")
        assert scan.status_code == 200 and scan.json()["networks"]
    unplug(runtime)
    async with ap(portal) as c:
        refused = await c.post("/setup/session", headers=WRITE)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "CABLE_REQUIRED"
