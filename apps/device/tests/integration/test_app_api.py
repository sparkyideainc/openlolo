"""Owner routes (/app/*) on the box page, and how the owner decides MCP-client requests there."""

import asyncio
import html
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from support import (
    CONSENT_PATH,
    LAN,
    PUBLIC,
    box_page,
    lan_config,
    lan_token,
    owner_approve,
    portal_build,
    register,
    running,
    start_authorization,
    tokens,
    web,
)

from openlolo.application import provisioning as provisioning_module
from openlolo.bootstrap import Runtime
from openlolo.interfaces.auth import digest
from openlolo.interfaces.mcp_http import (
    ADMIN_CALLS_PER_MINUTE,
    REFRESH_SECONDS,
    SCOPE_WORDS,
    STATUS_PATH,
    create_gateway_app,
)
from openlolo.interfaces.mcp_server import ALL_SCOPES
from openlolo.interfaces.setup_portal import PORTAL_IDENTITY, create_setup_portal

PUBLIC_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LAPTOP = ("192.168.1.20", 50000)


@pytest.fixture
async def box(tmp_path):
    config = lan_config(tmp_path)
    runtime = Runtime(config)
    await runtime.start()
    yield runtime, create_setup_portal(runtime, web=portal_build(tmp_path))
    await runtime.close()


def bearer(issued):
    return {"Authorization": f"Bearer {issued['access_token']}"}


async def public_request(pc, *, name="Claude", scope="phone:read phone:control"):
    """Register a browser client on the remote entry and park it on the consent page."""
    registered = await register(pc, callback=PUBLIC_CALLBACK, scope=scope, name=name)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    started, request_id, verifier = await start_authorization(
        pc, PUBLIC, client_id, callback=PUBLIC_CALLBACK, scope=scope
    )
    assert started.status_code == 302 and request_id, started.text
    return client_id, request_id, verifier


async def status_of(pc, request_id):
    polled = await pc.get(STATUS_PATH, params={"request": request_id})
    assert polled.status_code == 200, polled.text
    return polled.json()


def refresh_window(provider, client_id):
    [row] = [r for r in provider.list_clients() if r["client_id"] == client_id]
    return row["active_until"]["refresh"] - time.time()


async def test_no_client_can_be_granted_owner_admin(box):
    runtime, _ = box
    lan = create_gateway_app(runtime, LAN, allowed_hosts=runtime.config.lan_hosts[1:])
    async with running(lan), web(lan, LAN) as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert "owner:admin" not in meta.get("scopes_supported", [])
        refused = await register(c, scope="phone:read phone:control owner:admin")
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_client_metadata"
        registered = await register(c)
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        assert registered.json()["scope"] == "phone:read phone:control"
        asked, request_id, _ = await start_authorization(c, LAN, client_id, scope="owner:admin")
        assert request_id is None and "error=invalid_scope" in asked.headers["location"]

        started, request_id, verifier = await start_authorization(c, LAN, client_id)
        assert started.status_code == 302 and request_id
        page = await c.get(started.headers["location"])
        assert page.status_code == 200 and "name='admin'" not in page.text
        code = parse_qs(urlsplit(owner_approve(runtime, request_id, window="24h")).query)["code"][0]
        issued = await tokens(c, LAN, client_id, code, verifier)
        assert issued.status_code == 200, issued.text
        assert issued.json()["scope"] == "phone:read phone:control"
        [row] = [r for r in lan.provider.list_clients() if r["client_id"] == client_id]
        assert row["approved"] and row["scopes"] == ["phone:control", "phone:read"]
        assert abs(row["active_until"]["refresh"] - (time.time() + 24 * 3600)) < 60
        assert row["renewed_while_used"] is False and row["never_expires"] is False
        # The LAN entry has no owner routes, whatever the token.
        assert (await c.get("/app/status", headers=bearer(issued.json()))).status_code == 404

        # Cancel on the consent page returns the client an access_denied redirect.
        _, request_id, _ = await start_authorization(c, LAN, client_id)
        denied = await c.post(
            CONSENT_PATH, data={"request": request_id, "decision": "deny"}, headers={"Origin": LAN}
        )
        assert denied.status_code == 302 and "error=access_denied" in denied.headers["location"]


async def test_info_status_pause_operations_and_clients(box):
    runtime, portal = box
    lan = create_gateway_app(runtime, LAN, allowed_hosts=runtime.config.lan_hosts[1:])
    async with running(lan), web(lan, LAN) as lc, box_page(portal) as c:
        info = (await c.get("/app/info")).json()
        assert info["entry"] == "portal" and info["board"] == "test"
        assert info["setup_mode"] is False and info["features"] == {"network": True, "diagnostics": True}
        assert info["scopes"] == ALL_SCOPES
        assert info["mcp_url"] == f"{PUBLIC}/mcp"

        status = await c.get("/app/status")
        assert status.status_code == 200, status.text
        assert status.json()["setup_mode"] is False and "mcp" in status.json()
        assert status.json()["pending_consents"] == 0
        assert (await c.get("/app/capabilities")).status_code == 200

        paused = await c.post("/app/pause", json={})
        assert paused.status_code == 200, paused.text
        assert paused.json()["state"] == "SUCCEEDED" and paused.json()["kind"] == "pause"
        pause_id = paused.json()["id"]

        listed = await c.get("/app/operations")
        assert listed.status_code == 200, listed.text
        [pause] = [op for op in listed.json()["operations"] if op["id"] == pause_id]
        assert pause["client"] == PORTAL_IDENTITY and "owner" not in pause
        page = await c.get("/app/operations", params={"limit": 1})
        assert len(page.json()["operations"]) == 1
        single = await c.get(f"/app/operations/{pause_id}")
        assert single.status_code == 200 and single.json()["state"] == "SUCCEEDED"
        assert (await c.get("/app/operations/nope")).status_code == 404

        client_id, _ = await lan_token(lc, runtime, name="Agent")
        other_id, other = await lan_token(lc, runtime, name="Other agent")
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await lc.post("/mcp", json=ping, headers=bearer(other))).status_code == 200
        clients = (await c.get("/app/clients")).json()["clients"]
        assert {row["client_id"]: row["self"] for row in clients} == {client_id: False, other_id: False}
        revoked = await c.post(f"/app/clients/{other_id}/revoke", json={})
        assert revoked.status_code == 200 and revoked.json() == {"revoked": other_id}
        clients = (await c.get("/app/clients")).json()["clients"]
        assert [row["client_id"] for row in clients] == [client_id]
        assert (await lc.post("/mcp", json=ping, headers=bearer(other))).status_code == 401
        assert (await c.post(f"/app/clients/{other_id}/revoke", json={})).status_code == 404


async def test_setup_and_discover_routes(box):
    runtime, portal = box
    async with box_page(portal) as c:
        discovered = await c.get("/app/discover")
        assert discovered.status_code == 200 and discovered.json()["usb"][0]["udid"] == "simulated-phone"

        queued = await c.post(
            "/app/setup/pair",
            json={"payload": {"udid": "simulated-phone", "owner_confirmed": True}},
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["state"] == "QUEUED" and queued.json()["kind"] == "pair"
        operation_id = queued.json()["id"]
        for _ in range(100):
            polled = await c.get(f"/app/operations/{operation_id}")
            assert polled.status_code == 200
            if polled.json()["state"] not in {"QUEUED", "DISPATCHED"}:
                break
            await asyncio.sleep(0.05)
        assert polled.json()["state"] == "SUCCEEDED", polled.text
        assert polled.json()["result"]["trusted"] is True
        assert runtime.coordinator.lease is not None  # Setup auto-acquired the box page's lease.
        # The lease lapsed between page steps: setup takes control again instead of failing.
        await runtime.coordinator.drop_control()
        mounted = await c.post("/app/setup/mount_ddi", json={})
        assert mounted.status_code in {200, 202}, mounted.text
        assert runtime.coordinator.lease is not None

        unconfirmed = await c.post("/app/setup/pair", json={"payload": {"udid": "simulated-phone"}})
        assert unconfirmed.status_code == 400, unconfirmed.text
        assert unconfirmed.json()["error"]["code"] == "OWNER_CONFIRMATION_REQUIRED"
        unknown = await c.post("/app/setup/nope", json={})
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "UNKNOWN_SETUP_ACTION"
        bad_id = await c.post("/app/setup/mount_ddi", json={"operation_id": "x" * 65})
        assert bad_id.status_code == 400 and bad_id.json()["error"]["code"] == "INVALID_OPERATION_ID"


async def test_reset_needs_the_box_name(box):
    runtime, portal = box
    async with box_page(portal) as c:
        wrong = await c.post("/app/reset", json={"device": "OpenLolo-NOPE"})
        assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "RESET_NAME_MISMATCH"
        right = await c.post("/app/reset", json={"device": runtime.config.setup_name.lower()})
        assert right.status_code == 200 and right.json()["state"] == "reset"
        assert runtime.bindings.read() is None
        assert (await c.get("/app/info")).json()["setup_mode"] is True


async def test_network_routes(box, monkeypatch):
    runtime, portal = box
    monkeypatch.setattr(provisioning_module, "HANDOFF_READ_SECONDS", 0.1)  # No captive-sheet pause in tests.
    async with box_page(portal) as c:
        network = await c.get("/app/network")
        assert network.status_code == 200, network.text
        assert network.json()["provisioning"]["state"] == "idle" and network.json()["saved"] == ["home"]

        scanned = await c.post("/app/network/scan", json={})
        assert scanned.status_code == 200 and scanned.json()["networks"][0]["ssid"] == "HomeNet"

        entered = await c.post("/app/network/setup_mode", json={"enabled": True})
        assert entered.status_code == 200 and entered.json()["state"] == "setup_mode"
        assert entered.json()["setup_name"] == "OpenLolo-TEST" and entered.json()["reason"] == "api"
        assert "channel" not in entered.json()
        assert (await c.get("/app/info")).json()["setup_mode"] is True
        assert (await c.get("/app/status")).json()["setup_mode"] is True
        # Setup mode is the local WPA2 access point (ADR 0011): the setup AP is up.
        assert runtime.network.setup_ap_active
        assert "hotspot" not in network.json()

        missing = await c.post("/app/network/connect", json={"ssid": "HomeNet"})
        assert missing.status_code == 400 and missing.json()["error"]["code"] == "INVALID_REQUEST"
        joining = await c.post("/app/network/connect", json={"ssid": "HomeNet", "psk": "correct horse"})
        assert joining.status_code == 202, joining.text
        assert joining.json()["attempt_id"] and joining.json()["state"] in {"setup_mode", "joining"}
        for _ in range(100):
            state = (await c.get("/app/network")).json()["provisioning"]
            if state["state"] == "idle" and state["last_attempt"]["result"]:
                break
            await asyncio.sleep(0.05)
        assert state["state"] == "idle" and state["last_attempt"]["result"] == "connected"
        # The single radio left the setup AP to join: the window is over, the box is on home Wi-Fi.
        assert (await c.get("/app/info")).json()["setup_mode"] is False
        assert not runtime.network.setup_ap_active and runtime.network.active == "HomeNet"
        assert "correct horse" not in str(state)
        profile = state["last_attempt"]["profile"]
        assert profile.startswith("openlolo-homenet-")

        protected = await c.post("/app/network/forget", json={"ssid": "home"})
        assert protected.status_code == 403 and protected.json()["error"]["code"] == "PROFILE_PROTECTED"
        forgotten = await c.post("/app/network/forget", json={"ssid": profile})
        assert forgotten.status_code == 200 and runtime.network.profiles == ["home"]
        unknown = await c.post("/app/network/reboot", json={})
        assert unknown.status_code == 404
        left = await c.post("/app/network/setup_mode", json={"enabled": False})
        assert left.status_code == 200 and left.json()["state"] == "idle"
        assert (await c.get("/app/info")).json()["setup_mode"] is False
        # A Wi-Fi change while online never opens the window.
        joining = await c.post("/app/network/connect", json={"ssid": "HomeNet", "psk": "correct horse"})
        assert joining.status_code == 202
        for _ in range(100):
            state = (await c.get("/app/network")).json()["provisioning"]
            if state["state"] == "idle" and state["last_attempt"]["result"]:
                break
            await asyncio.sleep(0.05)
        assert state["state"] == "idle" and state["setup_name"] is None and not runtime.provisioning.active


async def test_box_page_keeps_control_and_its_operations_across_the_sweep(box):
    runtime, portal = box
    gateway = runtime.door_gateway
    async with box_page(portal) as c:
        paused = (await c.post("/app/pause", json={})).json()
        bridge = gateway.bridges[PORTAL_IDENTITY][0]
        await bridge.ensure_control()
        lease = runtime.coordinator.lease
        assert lease is not None
        # The sweep runs every 30 s and on every revoke; the page has no OAuth client behind it.
        await gateway.sweep()
        assert gateway.bridges[PORTAL_IDENTITY][0] is bridge and runtime.coordinator.lease is lease
        single = await c.get(f"/app/operations/{paused['id']}")
        assert single.status_code == 200 and single.json()["kind"] == "pause"


async def test_diagnostics_are_redacted(box):
    runtime, portal = box
    async with box_page(portal) as c:
        report = await c.get("/app/diagnostics")
        assert report.status_code == 200, report.text
        data = report.json()
        assert {"versions", "config", "status", "journal", "logs", "network", "provisioning"} <= set(data)
        assert "lan_key" not in data["config"] and "lan_cert" not in data["config"]
        assert data["config"]["lan_url"] == LAN and data["config"]["backend"] == "simulated"
        assert "samples" not in data["status"].get("profile", {}).get("calibration", {})
        assert isinstance(data["logs"], list) and isinstance(data["journal"]["recent"], list)
        assert data["network"]["saved"] == ["home"] and "psk" not in str(data).lower()


async def test_owner_routes_need_the_box_page(box):
    _, portal = box
    # Another device on the LAN without a session gets nothing.
    async with box_page(portal, client=LAPTOP, session=False) as laptop:
        for path in ("/app/status", "/app/operations", "/app/network", "/app/clients", "/app/consents"):
            anonymous = await laptop.get(path)
            assert anonymous.status_code == 401, path
            assert anonymous.json()["error"]["code"] == "LOGIN_REQUIRED"
        assert (await laptop.post("/app/pause", json={})).status_code == 401
    # The box page polls status while the owner has it open: its higher limit applies.
    assert ADMIN_CALLS_PER_MINUTE == 300
    async with box_page(portal) as c:
        codes = {(await c.get("/app/status")).status_code for _ in range(130)}
        assert codes == {200}


async def test_owner_decides_pending_consents_of_the_remote_entry(box):
    runtime, portal = box
    public = create_gateway_app(runtime)
    lan = create_gateway_app(runtime, LAN, allowed_hosts=runtime.config.lan_hosts[1:])
    async with running(public), running(lan), web(lan, LAN) as lc, web(public, PUBLIC) as pc:
        async with box_page(portal) as c:
            assert (await c.get("/app/consents")).json() == {"consents": []}

            client_id, request_id, verifier = await public_request(pc)
            listed = await c.get("/app/consents")
            assert listed.status_code == 200, listed.text
            [pending] = listed.json()["consents"]
            assert pending["handle"] == digest(request_id) and request_id not in listed.text
            assert pending["client_name"] == "Claude" and pending["client_id"] == client_id
            assert pending["scopes"] == ["phone:read", "phone:control"] and pending["issuer"] == PUBLIC
            assert 0 < pending["expires_in"] <= 600 and pending["redirect_hosts"] == ["claude.ai"]
            assert (await c.get("/app/status")).json()["pending_consents"] == 1
            assert await status_of(pc, request_id) == {"state": "pending"}

            # Owner allows for 30 days from the box page; the browser's poll picks up the redirect once.
            choice = {"decision": "allow", "window": "30d"}
            decided = await c.post(f"/app/consents/{pending['handle']}", json=choice)
            assert decided.status_code == 200, decided.text
            assert decided.json() == {
                "decided": "allow",
                "scopes": ["phone:read", "phone:control"],
                "window": REFRESH_SECONDS,
            }
            assert (await c.get("/app/status")).json()["pending_consents"] == 0
            assert (await c.get("/app/consents")).json() == {"consents": []}
            polled = await status_of(pc, request_id)
            assert polled["state"] == "decided" and polled["redirect"].startswith(PUBLIC_CALLBACK + "?")
            query = parse_qs(urlsplit(polled["redirect"]).query)
            assert query["state"] == ["abc"] and query["code"]
            assert await status_of(pc, request_id) == {"state": "gone"}  # Handed over exactly once.

            # The code belongs to the remote issuer: the LAN entry refuses it.
            code = query["code"][0]
            foreign = await tokens(lc, LAN, client_id, code, verifier, callback=PUBLIC_CALLBACK)
            assert foreign.status_code in {400, 401}, foreign.text
            exchanged = await tokens(pc, PUBLIC, client_id, code, verifier, callback=PUBLIC_CALLBACK)
            assert exchanged.status_code == 200, exchanged.text
            assert exchanged.json()["scope"] == "phone:read phone:control"
            [row] = [r for r in public.provider.list_clients() if r["client_id"] == client_id]
            assert row["approved"] and row["issuer"] == PUBLIC
            assert row["scopes"] == ["phone:control", "phone:read"]
            assert abs(refresh_window(public.provider, client_id) - REFRESH_SECONDS) < 60
            ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            assert (await pc.post("/mcp", json=ping, headers=bearer(exchanged.json()))).status_code == 200

            # Deny from the box page: the client gets access_denied, nothing is granted.
            other_id, request_id, _ = await public_request(pc, name="Other agent")
            handle = digest(request_id)
            denied = await c.post(f"/app/consents/{handle}", json={"decision": "deny"})
            assert denied.status_code == 200 and denied.json() == {"decided": "deny", "scopes": []}
            polled = await status_of(pc, request_id)
            assert polled["state"] == "decided" and "error=access_denied" in polled["redirect"]
            approved = [r["client_id"] for r in public.provider.list_clients() if r["approved"]]
            assert other_id not in approved
            # A decided handle is unknown afterwards, as is one that never existed.
            for bad in (handle, "nope", digest("nope")):
                gone = await c.post(f"/app/consents/{bad}", json=choice)
                assert gone.status_code == 404 and gone.json()["error"]["code"] == "CONSENT_EXPIRED", bad

            # An unknown duration is refused, and the request stays pending.
            _, request_id, _ = await public_request(pc, name="Waiting")
            refused = await c.post(
                f"/app/consents/{digest(request_id)}", json={"decision": "allow", "window": "8h"}
            )
            assert refused.status_code == 422 and refused.json()["error"]["code"] == "INVALID_WINDOW"
            assert await status_of(pc, request_id) == {"state": "pending"}


async def test_consent_page_offers_box_page_approval(box):
    runtime, _ = box
    lan = create_gateway_app(runtime, LAN, allowed_hosts=runtime.config.lan_hosts[1:])
    async with running(lan), web(lan, LAN) as c:
        registered = await register(c)
        client_id = registered.json()["client_id"]
        started, request_id, _ = await start_authorization(c, LAN, client_id)
        page = await c.get(started.headers["location"])
        assert page.status_code == 200, page.text
        text = html.unescape(page.text)
        assert (
            f"http://openlolo-test.local/access?r={digest(request_id)}" in text
        )  # Box page link (ADR 0012).
        assert "<svg" in text and "value='deny'" in text
        # The box page is the only way to approve: no PIN, no credential, no duration on this page.
        assert "name='pin'" not in text and "name='credential'" not in text and "name='window'" not in text
        assert all(words in text for words in SCOPE_WORDS.values())
        assert STATUS_PATH in text

        csp = page.headers["content-security-policy"]
        assert "script-src 'nonce-" in csp and "connect-src 'self'" in csp and "default-src 'none'" in csp
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp)
        assert nonce and f"<script nonce='{nonce.group(1)}'>" in page.text
        assert "x-openlolo-nonce" not in page.headers and "nonce" not in page.headers.get(
            "x-openlolo-nonce", ""
        )
        assert page.headers["cache-control"] == "no-store"
        # Every render draws a fresh nonce; pages without a script carry no script-src at all.
        second = await c.get(started.headers["location"])
        assert second.headers["content-security-policy"] != csp
        expired = await c.get(CONSENT_PATH, params={"request": "nope"})
        assert expired.status_code == 400 and "script-src" not in expired.headers["content-security-policy"]

        assert await status_of(c, request_id) == {"state": "pending"}
        assert await status_of(c, "nope") == {"state": "gone"}
        assert (await c.get(STATUS_PATH)).json() == {"state": "gone"}
