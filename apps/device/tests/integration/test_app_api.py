"""Owner routes on the LAN entry: admin consent window, scopes, and every /app/* route."""

import asyncio
import html
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from support import (
    APP_CALLBACK,
    CONSENT_PATH,
    LAN,
    LAN_CERT,
    PUBLIC,
    app_token,
    consent_form,
    lan_config,
    register,
    running,
    start_authorization,
    tokens,
    web,
)

from openlolo.application import provisioning as provisioning_module
from openlolo.bootstrap import Runtime
from openlolo.interfaces import tls
from openlolo.interfaces.auth import digest
from openlolo.interfaces.mcp_http import (
    ADMIN_CALLS_PER_MINUTE,
    PIN_SECONDS,
    REFRESH_SECONDS,
    SCOPE_WORDS,
    STATUS_PATH,
    create_gateway_app,
)

ADMIN = "phone:read phone:control owner:admin"
PUBLIC_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
async def lan(tmp_path):
    config = lan_config(tmp_path)
    runtime = Runtime(config)
    await runtime.start()
    app = create_gateway_app(runtime, config.lan_url, allowed_hosts=config.lan_hosts[1:], app_routes=True)
    async with running(app):
        yield runtime, app
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


async def pin_form(pc, request_id, pin):
    """The browser's consent form submitted with a PIN from the box page (same-origin POST)."""
    form = {"request": request_id, "decision": "pin", "pin": pin}
    return await pc.post(CONSENT_PATH, data=form, headers={"Origin": PUBLIC})


def refresh_window(provider, client_id):
    [row] = [r for r in provider.list_clients() if r["client_id"] == client_id]
    return row["active_until"]["refresh"] - time.time()


async def test_admin_registration_consent_and_admin_window(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        registered = await register(c)
        assert registered.status_code == 201, registered.text
        body = registered.json()
        client_id = body["client_id"]
        assert body["redirect_uris"] == [APP_CALLBACK] and body["scope"] == ADMIN
        # owner:admin is not a scope the remote entry offers at all.
        public = create_gateway_app(runtime)
        async with web(public, PUBLIC) as pc:
            refused = await register(pc)
            assert refused.status_code == 400 and refused.json()["error"] == "invalid_client_metadata"

        started, request_id, verifier = await start_authorization(c, LAN, client_id)
        assert started.status_code == 302 and request_id
        page = await c.get(started.headers["location"])
        assert page.status_code == 200 and "name='admin'" in page.text and "30d" in page.text

        # JSON bodies are not a consent: the page is a same-origin form.
        as_json = await c.post(
            "/oauth/consent", json={"request": request_id, "decision": "allow", "credential": "x"}
        )
        assert as_json.status_code == 403 and "Cross-site" in as_json.text
        cross = await consent_form(c, "https://evil.test", request_id, "x")
        assert cross.status_code == 403 and "Cross-site" in cross.text
        wrong = await consent_form(c, LAN, request_id, "not-a-credential")
        assert wrong.status_code == 401 and "Not accepted." in wrong.text

        consented = await consent_form(c, LAN, request_id, runtime.auth.issue())
        assert consented.status_code == 302, consented.text
        redirect = consented.headers["location"]
        assert redirect.startswith(APP_CALLBACK + "?")
        query = parse_qs(urlsplit(redirect).query)
        assert query["state"] == ["abc"] and query["code"]
        assert (await consent_form(c, LAN, request_id, runtime.auth.issue())).status_code == 400  # Consumed.

        issued = await tokens(c, LAN, client_id, query["code"][0], verifier)
        assert issued.status_code == 200, issued.text
        assert issued.json()["scope"] == ADMIN and issued.json()["expires_in"] <= 900
        [row] = [r for r in app.provider.list_clients() if r["client_id"] == client_id]
        assert row["approved"] and row["scopes"] == ["owner:admin", "phone:control", "phone:read"]
        assert abs(row["active_until"]["refresh"] - (time.time() + REFRESH_SECONDS)) < 60
        assert row["issuer"] == LAN

        # The token itself is not accepted through the query string and works at /mcp.
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await c.post("/mcp", json=ping, headers=bearer(issued.json()))).status_code == 200

        # Without admin registration the admin checkbox is ignored and 30d falls back to 1h.
        plain = await register(c, scope="phone:read phone:control", name="Reader")
        plain_id = plain.json()["client_id"]
        _, request_id, verifier = await start_authorization(c, LAN, plain_id)
        consented = await consent_form(c, LAN, request_id, runtime.auth.issue(), admin=True, window="30d")
        assert consented.status_code == 302, consented.text
        code = parse_qs(urlsplit(consented.headers["location"]).query)["code"][0]
        issued = (await tokens(c, LAN, plain_id, code, verifier)).json()
        assert issued["scope"] == "phone:read phone:control"
        [row] = [r for r in app.provider.list_clients() if r["client_id"] == plain_id]
        assert abs(row["active_until"]["refresh"] - (time.time() + 3600)) < 60

        # Deny returns the client an access_denied redirect without needing a credential.
        _, request_id, _ = await start_authorization(c, LAN, plain_id)
        denied = await c.post(
            "/oauth/consent", data={"request": request_id, "decision": "deny"}, headers={"Origin": LAN}
        )
        assert denied.status_code == 302 and "error=access_denied" in denied.headers["location"]


async def test_info_status_pause_operations_and_clients(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        anonymous = await c.get("/app/info")
        assert anonymous.status_code == 200  # Discovery of the box needs no token.
        client_id, issued = await app_token(c, runtime)
        headers = bearer(issued)

        info = (await c.get("/app/info", headers=headers)).json()
        assert info["fingerprint"] == tls.fingerprint(LAN_CERT)
        assert info["entry"] == "lan" and info["issuer"] == LAN and info["board"] == "test"
        assert info["setup_mode"] is False and info["features"] == {"network": True, "diagnostics": True}
        assert info["scopes"] == ["phone:read", "phone:control", "owner:admin"]

        status = await c.get("/app/status", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["setup_mode"] is False and "mcp" in status.json()
        capabilities = await c.get("/app/capabilities", headers=headers)
        assert capabilities.status_code == 200

        paused = await c.post("/app/pause", json={}, headers=headers)
        assert paused.status_code == 200, paused.text
        assert paused.json()["state"] == "SUCCEEDED" and paused.json()["kind"] == "pause"
        pause_id = paused.json()["id"]

        listed = await c.get("/app/operations", headers=headers)
        assert listed.status_code == 200, listed.text
        operations = listed.json()["operations"]
        [pause] = [op for op in operations if op["id"] == pause_id]
        assert pause["client"] == "Lolo App" and "owner" not in pause
        page = await c.get("/app/operations", params={"limit": 1}, headers=headers)
        assert len(page.json()["operations"]) == 1
        single = await c.get(f"/app/operations/{pause_id}", headers=headers)
        assert single.status_code == 200 and single.json()["state"] == "SUCCEEDED"
        assert (await c.get("/app/operations/nope", headers=headers)).status_code == 404

        clients = (await c.get("/app/clients", headers=headers)).json()["clients"]
        [me] = [row for row in clients if row["client_id"] == client_id]
        assert me["self"] is True and me["client_name"] == "Lolo App"

        assert status.json()["pending_consents"] == 0
        assert (await c.post("/app/credential", json={}, headers=headers)).status_code == 404  # Gone.
        refused = await c.post(f"/app/clients/{client_id}/revoke", json={}, headers=headers)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "CANNOT_REVOKE_SELF"

        other_id, other = await app_token(c, runtime, name="Other Phone")
        assert (await c.get("/app/status", headers=bearer(other))).status_code == 200
        clients = (await c.get("/app/clients", headers=headers)).json()["clients"]
        assert {row["client_id"]: row["self"] for row in clients} == {client_id: True, other_id: False}
        revoked = await c.post(f"/app/clients/{other_id}/revoke", json={}, headers=headers)
        assert revoked.status_code == 200 and revoked.json() == {"revoked": other_id}
        clients = (await c.get("/app/clients", headers=headers)).json()["clients"]
        assert [row["client_id"] for row in clients] == [client_id]
        assert (await c.get("/app/status", headers=bearer(other))).status_code == 401
        assert (await c.post(f"/app/clients/{other_id}/revoke", json={}, headers=headers)).status_code == 404


async def test_setup_and_discover_routes(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        _, issued = await app_token(c, runtime)
        headers = bearer(issued)
        discovered = await c.get("/app/discover", headers=headers)
        assert discovered.status_code == 200 and discovered.json()["usb"][0]["udid"] == "simulated-phone"

        queued = await c.post(
            "/app/setup/pair",
            json={"payload": {"udid": "simulated-phone", "owner_confirmed": True}},
            headers=headers,
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["state"] == "QUEUED" and queued.json()["kind"] == "pair"
        operation_id = queued.json()["id"]
        for _ in range(100):
            polled = await c.get(f"/app/operations/{operation_id}", headers=headers)
            assert polled.status_code == 200
            if polled.json()["state"] not in {"QUEUED", "DISPATCHED"}:
                break
            await asyncio.sleep(0.05)
        assert polled.json()["state"] == "SUCCEEDED", polled.text
        assert polled.json()["result"]["trusted"] is True
        assert runtime.coordinator.lease is not None  # Setup auto-acquired the client's lease.

        unconfirmed = await c.post(
            "/app/setup/pair", json={"payload": {"udid": "simulated-phone"}}, headers=headers
        )
        assert unconfirmed.status_code == 400, unconfirmed.text
        assert unconfirmed.json()["error"]["code"] == "OWNER_CONFIRMATION_REQUIRED"
        unknown = await c.post("/app/setup/nope", json={}, headers=headers)
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "UNKNOWN_SETUP_ACTION"
        bad_id = await c.post("/app/setup/mount_ddi", json={"operation_id": "x" * 65}, headers=headers)
        assert bad_id.status_code == 400 and bad_id.json()["error"]["code"] == "INVALID_OPERATION_ID"


async def test_reset_needs_the_box_name(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        _, issued = await app_token(c, runtime)
        headers = bearer(issued)
        wrong = await c.post("/app/reset", json={"device": "OpenLolo-NOPE"}, headers=headers)
        assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "RESET_NAME_MISMATCH"
        right = await c.post(
            "/app/reset", json={"device": runtime.config.setup_name.lower()}, headers=headers
        )
        assert right.status_code == 200 and right.json()["state"] == "reset"
        assert runtime.bindings.read() is None
        assert (await c.get("/app/info")).json()["setup_mode"] is True


async def test_network_routes(lan, monkeypatch):
    runtime, app = lan
    monkeypatch.setattr(provisioning_module, "HANDOFF_READ_SECONDS", 0.1)  # No captive-sheet pause in tests.
    async with web(app, LAN) as c:
        _, issued = await app_token(c, runtime)
        headers = bearer(issued)
        network = await c.get("/app/network", headers=headers)
        assert network.status_code == 200, network.text
        assert network.json()["provisioning"]["state"] == "idle" and network.json()["saved"] == ["home"]

        scanned = await c.post("/app/network/scan", json={}, headers=headers)
        assert scanned.status_code == 200 and scanned.json()["networks"][0]["ssid"] == "HomeNet"

        entered = await c.post("/app/network/setup_mode", json={"enabled": True}, headers=headers)
        assert entered.status_code == 200 and entered.json()["state"] == "setup_mode"
        assert entered.json()["setup_name"] == "OpenLolo-TEST" and entered.json()["reason"] == "api"
        assert "channel" not in entered.json()  # No Bluetooth channel to report.
        assert (await c.get("/app/info")).json()["setup_mode"] is True
        assert (await c.get("/app/status", headers=headers)).json()["setup_mode"] is True
        # Setup mode is the local WPA2 access point (ADR 0011): the setup AP is up.
        assert runtime.network.setup_ap_active
        assert "hotspot" not in network.json()

        missing = await c.post("/app/network/connect", json={"ssid": "HomeNet"}, headers=headers)
        assert missing.status_code == 400 and missing.json()["error"]["code"] == "INVALID_REQUEST"
        joining = await c.post(
            "/app/network/connect", json={"ssid": "HomeNet", "psk": "correct horse"}, headers=headers
        )
        assert joining.status_code == 202, joining.text
        assert joining.json()["attempt_id"] and joining.json()["state"] in {"setup_mode", "joining"}
        for _ in range(100):
            state = (await c.get("/app/network", headers=headers)).json()["provisioning"]
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

        protected = await c.post("/app/network/forget", json={"ssid": "home"}, headers=headers)
        assert protected.status_code == 403 and protected.json()["error"]["code"] == "PROFILE_PROTECTED"
        forgotten = await c.post("/app/network/forget", json={"ssid": profile}, headers=headers)
        assert forgotten.status_code == 200 and runtime.network.profiles == ["home"]
        unknown = await c.post("/app/network/reboot", json={}, headers=headers)
        assert unknown.status_code == 404
        left = await c.post("/app/network/setup_mode", json={"enabled": False}, headers=headers)
        assert left.status_code == 200 and left.json()["state"] == "idle"
        assert (await c.get("/app/info")).json()["setup_mode"] is False
        # A Wi-Fi change while online never opens the window.
        joining = await c.post(
            "/app/network/connect", json={"ssid": "HomeNet", "psk": "correct horse"}, headers=headers
        )
        assert joining.status_code == 202
        for _ in range(100):
            state = (await c.get("/app/network", headers=headers)).json()["provisioning"]
            if state["state"] == "idle" and state["last_attempt"]["result"]:
                break
            await asyncio.sleep(0.05)
        assert state["state"] == "idle" and state["setup_name"] is None and not runtime.provisioning.active


async def test_diagnostics_are_redacted(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        _, issued = await app_token(c, runtime)
        report = await c.get("/app/diagnostics", headers=bearer(issued))
        assert report.status_code == 200, report.text
        data = report.json()
        assert {"versions", "config", "status", "journal", "logs", "network", "provisioning"} <= set(data)
        assert "lan_key" not in data["config"] and "lan_cert" not in data["config"]
        assert data["config"]["lan_url"] == LAN and data["config"]["backend"] == "simulated"
        assert "samples" not in data["status"].get("profile", {}).get("calibration", {})
        assert isinstance(data["logs"], list) and isinstance(data["journal"]["recent"], list)
        assert data["network"]["saved"] == ["home"] and "psk" not in str(data).lower()


async def test_scope_matrix_and_admin_rate_limit(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        for path in ("/app/status", "/app/operations", "/app/network", "/app/clients"):
            anonymous = await c.get(path)
            assert anonymous.status_code == 401, path
            assert anonymous.json()["error"]["code"] == "LOGIN_REQUIRED"
        assert (await c.post("/app/pause", json={})).status_code == 401
        assert (await c.get("/app/status", headers={"Authorization": "Bearer nope"})).status_code == 401

        _, reader = await app_token(c, runtime, control=False, admin=False, name="Reader")
        assert reader["scope"] == "phone:read"
        headers = bearer(reader)
        assert (await c.get("/app/status", headers=headers)).status_code == 200
        assert (await c.post("/app/pause", json={}, headers=headers)).status_code == 200
        for path in ("/app/operations", "/app/network", "/app/clients", "/app/discover", "/app/diagnostics"):
            denied = await c.get(path, headers=headers)
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "SCOPE_REQUIRED", path
        for path in ("/app/setup/pair", "/app/network/scan", "/app/clients/x/revoke"):
            denied = await c.post(path, json={}, headers=headers)
            assert denied.status_code == 403, path

        # Admin without control: management works, phone setup still needs the control scope.
        _, manager = await app_token(c, runtime, control=False, admin=True, name="Manager")
        assert manager["scope"] == "phone:read owner:admin"
        assert (await c.get("/app/clients", headers=bearer(manager))).status_code == 200
        setup = await c.post("/app/setup/mount_ddi", json={}, headers=bearer(manager))
        assert setup.status_code == 403 and setup.json()["error"]["code"] == "SCOPE_REQUIRED"

        assert ADMIN_CALLS_PER_MINUTE == 300
        _, admin = await app_token(c, runtime, name="Admin")
        codes = {(await c.get("/app/status", headers=bearer(admin))).status_code for _ in range(130)}
        assert codes == {200}


async def test_owner_decides_pending_consents_of_the_remote_entry(lan):
    runtime, app = lan
    public = create_gateway_app(runtime)
    async with running(public), web(app, LAN) as c, web(public, PUBLIC) as pc:
        _, issued = await app_token(c, runtime)
        headers = bearer(issued)
        assert (await c.get("/app/consents", headers=headers)).json() == {"consents": []}

        client_id, request_id, verifier = await public_request(pc)
        listed = await c.get("/app/consents", headers=headers)
        assert listed.status_code == 200, listed.text
        [pending] = listed.json()["consents"]
        assert pending["handle"] == digest(request_id) and request_id not in listed.text
        assert pending["client_name"] == "Claude" and pending["client_id"] == client_id
        assert pending["scopes"] == ["phone:read", "phone:control"] and pending["issuer"] == PUBLIC
        assert 0 < pending["expires_in"] <= 600 and pending["redirect_hosts"] == ["claude.ai"]
        assert (await c.get("/app/status", headers=headers)).json()["pending_consents"] == 1
        assert await status_of(pc, request_id) == {"state": "pending"}

        # Owner allows control for 8h from the box page; the browser's poll picks up the redirect once.
        choice = {"decision": "allow", "control": True, "window": "8h"}
        decided = await c.post(f"/app/consents/{pending['handle']}", json=choice, headers=headers)
        assert decided.status_code == 200, decided.text
        assert decided.json() == {
            "decided": "allow",
            "scopes": ["phone:read", "phone:control"],
            "window": 8 * 3600,
        }
        assert (await c.get("/app/status", headers=headers)).json()["pending_consents"] == 0
        assert (await c.get("/app/consents", headers=headers)).json() == {"consents": []}
        polled = await status_of(pc, request_id)
        assert polled["state"] == "decided" and polled["redirect"].startswith(PUBLIC_CALLBACK + "?")
        query = parse_qs(urlsplit(polled["redirect"]).query)
        assert query["state"] == ["abc"] and query["code"]
        assert await status_of(pc, request_id) == {"state": "gone"}  # Handed over exactly once.

        # The code belongs to the remote issuer even though the LAN entry minted it.
        foreign = await tokens(c, LAN, client_id, query["code"][0], verifier, callback=PUBLIC_CALLBACK)
        assert foreign.status_code in {400, 401}, foreign.text
        exchanged = await tokens(pc, PUBLIC, client_id, query["code"][0], verifier, callback=PUBLIC_CALLBACK)
        assert exchanged.status_code == 200, exchanged.text
        assert exchanged.json()["scope"] == "phone:read phone:control"
        [row] = [r for r in public.provider.list_clients() if r["client_id"] == client_id]
        assert (
            row["approved"] and row["issuer"] == PUBLIC and row["scopes"] == ["phone:control", "phone:read"]
        )
        assert abs(refresh_window(public.provider, client_id) - 8 * 3600) < 60
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await pc.post("/mcp", json=ping, headers=bearer(exchanged.json()))).status_code == 200

        # Deny from the box page: the client gets access_denied, nothing is granted.
        other_id, request_id, _ = await public_request(pc, name="Other agent")
        handle = digest(request_id)
        denied = await c.post(f"/app/consents/{handle}", json={"decision": "deny"}, headers=headers)
        assert denied.status_code == 200 and denied.json() == {"decided": "deny", "scopes": []}
        polled = await status_of(pc, request_id)
        assert polled["state"] == "decided" and "error=access_denied" in polled["redirect"]
        assert not any(r["client_id"] == other_id for r in public.provider.list_clients() if r["approved"])
        # A decided handle is unknown afterwards, as is one that never existed.
        for bad in (handle, "nope", digest("nope")):
            gone = await c.post(f"/app/consents/{bad}", json=choice, headers=headers)
            assert gone.status_code == 404 and gone.json()["error"]["code"] == "CONSENT_EXPIRED", bad
            assert (await c.post(f"/app/consents/{bad}/pin", json={}, headers=headers)).status_code == 404

        # Only the owner's admin scope may see or decide consents.
        _, _, _ = await public_request(pc, name="Waiting")
        _, reader = await app_token(c, runtime, control=False, admin=False, name="Reader")
        [waiting] = (await c.get("/app/consents", headers=headers)).json()["consents"]
        for method, path in (
            ("GET", "/app/consents"),
            ("POST", f"/app/consents/{waiting['handle']}"),
            ("POST", f"/app/consents/{waiting['handle']}/pin"),
        ):
            refused = await c.request(method, path, json=choice, headers=bearer(reader))
            assert refused.status_code == 403 and refused.json()["error"]["code"] == "SCOPE_REQUIRED", path
        assert (await c.get("/app/consents")).status_code == 401
        assert [r["handle"] for r in (await c.get("/app/consents", headers=headers)).json()["consents"]] == [
            waiting["handle"]
        ]


async def test_pin_from_the_box_page_completes_one_request(lan, monkeypatch):
    runtime, app = lan
    public = create_gateway_app(runtime)
    async with running(public), web(app, LAN) as c, web(public, PUBLIC) as pc:
        _, issued = await app_token(c, runtime)
        headers = bearer(issued)
        client_id, request_id, verifier = await public_request(pc)
        handle = digest(request_id)
        minted = await c.post(
            f"/app/consents/{handle}/pin", json={"control": True, "window": "24h"}, headers=headers
        )
        assert minted.status_code == 200, minted.text
        pin = minted.json()["pin"]
        assert re.fullmatch(r"[0-9]{6}", pin) and minted.json()["expires_in"] == PIN_SECONDS == 30
        assert set(minted.json()) == {"pin", "expires_in"}

        # Wrong PINs: four refusals on the page, the fifth kills the request.
        wrong = f"{(int(pin) + 1) % 10**6:06d}"
        for _ in range(4):
            refused = await pin_form(pc, request_id, wrong)
            assert refused.status_code == 401 and "Not accepted." in refused.text, refused.text
            assert await status_of(pc, request_id) == {"state": "pending"}
        killed = await pin_form(pc, request_id, wrong)
        assert killed.status_code == 400 and "Request expired" in killed.text
        assert await status_of(pc, request_id) == {"state": "gone"}
        assert (await c.get("/app/consents", headers=headers)).json() == {"consents": []}
        assert (await pin_form(pc, request_id, pin)).status_code == 400  # The right PIN died with it.
        assert (await pc.get(CONSENT_PATH, params={"request": request_id})).status_code == 400

        # A fresh request with a fresh PIN. The PIN is bound to its request and is not a credential.
        client_id, request_id, verifier = await public_request(pc)
        handle = digest(request_id)
        _, other_request, _ = await public_request(pc, name="Other agent")
        other = await c.post(
            f"/app/consents/{digest(other_request)}/pin", json={"control": True}, headers=headers
        )
        pin = (
            await c.post(
                f"/app/consents/{handle}/pin", json={"control": True, "window": "24h"}, headers=headers
            )
        ).json()["pin"]
        crossed = await pin_form(pc, request_id, other.json()["pin"])
        assert crossed.status_code == 401 and "Not accepted." in crossed.text
        as_credential = await consent_form(pc, PUBLIC, request_id, pin, admin=False, window="24h")
        assert as_credential.status_code == 401 and "Not accepted." in as_credential.text
        malformed = await pin_form(pc, request_id, "12345")
        assert malformed.status_code == 401
        assert await status_of(pc, request_id) == {"state": "pending"}

        approved = await pin_form(pc, request_id, pin)
        assert approved.status_code == 302, approved.text
        query = parse_qs(urlsplit(approved.headers["location"]).query)
        assert approved.headers["location"].startswith(PUBLIC_CALLBACK) and query["state"] == ["abc"]
        assert (await pin_form(pc, request_id, pin)).status_code == 400  # Consumed with the request.
        still = (await c.get("/app/consents", headers=headers)).json()["consents"]
        assert [r["handle"] for r in still] == [digest(other_request)]  # Only the untouched one remains.
        exchanged = await tokens(pc, PUBLIC, client_id, query["code"][0], verifier, callback=PUBLIC_CALLBACK)
        assert exchanged.status_code == 200, exchanged.text
        assert exchanged.json()["scope"] == "phone:read phone:control"  # The owner's choice, not the form's.
        assert abs(refresh_window(public.provider, client_id) - 24 * 3600) < 60

        # A PIN older than 30 seconds is refused; re-minting replaces it.
        client_id, request_id, verifier = await public_request(pc, name="Late")
        handle = digest(request_id)
        pin = (await c.post(f"/app/consents/{handle}/pin", json={"control": True}, headers=headers)).json()[
            "pin"
        ]
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + PIN_SECONDS + 1)
        late = await pin_form(pc, request_id, pin)
        assert late.status_code == 401 and "Not accepted." in late.text
        again = (await c.post(f"/app/consents/{handle}/pin", json={"admin": True}, headers=headers)).json()[
            "pin"
        ]
        assert (await pin_form(pc, request_id, pin)).status_code == 401  # The old PIN stays dead.
        approved = await pin_form(pc, request_id, again)
        assert approved.status_code == 302, approved.text
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        exchanged = await tokens(pc, PUBLIC, client_id, code, verifier, callback=PUBLIC_CALLBACK)
        # Admin is not offered on the remote entry; control always comes with approval.
        assert exchanged.json()["scope"] == "phone:read phone:control"


async def test_consent_page_offers_box_page_approval(lan):
    runtime, app = lan
    async with web(app, LAN) as c:
        registered = await register(c)
        client_id = registered.json()["client_id"]
        started, request_id, _ = await start_authorization(c, LAN, client_id)
        page = await c.get(started.headers["location"])
        assert page.status_code == 200, page.text
        text = html.unescape(page.text)
        assert (
            f"http://openlolo-test.local/access?r={digest(request_id)}" in text
        )  # Box page link (ADR 0012).
        assert "<svg" in text and "name='pin'" in text and "value='pin'" in text
        assert "name='credential'" in text and "<details>" in text  # Credential proof stays, folded away.
        assert all(words in text for words in SCOPE_WORDS.values())
        assert STATUS_PATH in text and "name='admin'" in text

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
