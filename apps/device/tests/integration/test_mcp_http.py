import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from support import narrow

from openlolo.bootstrap import Runtime
from openlolo.config import Config
from openlolo.interfaces.mcp_http import CONSENT_PATH, TIMING_LOG, create_gateway_app

PUBLIC = "https://pi.tailnet.ts.net"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
async def remote(tmp_path):
    config = Config(
        backend="simulated",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        public_url=PUBLIC,
    )
    runtime = Runtime(config)
    await runtime.start()
    app = create_gateway_app(runtime)
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(lifespan(app, ready, stop))
    await ready.wait()
    yield runtime, app
    stop.set()
    await task
    await runtime.close()


async def lifespan(app, ready, stop):
    """ASGITransport never runs lifespan; the session manager and gateway sweeper need it."""
    pending = [{"type": "lifespan.startup"}]

    async def receive():
        if pending:
            return pending.pop()
        await stop.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"].endswith(".failed"):
            raise RuntimeError(message)
        if message["type"] == "lifespan.startup.complete":
            ready.set()

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)


def web(app, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC, **kwargs)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def register(c, scope="phone:read phone:control"):
    metadata = {"client_name": "Claude", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}
    if scope is not None:
        metadata["scope"] = scope
    r = await c.post("/register", json=metadata)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def authorize(c, runtime, client_id, *, window="1h", scope="phone:read phone:control"):
    verifier, challenge = pkce()
    r = await c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc",
            "scope": scope,
            "resource": PUBLIC + "/mcp",
        },
    )
    assert r.status_code == 302, r.text
    consent = r.headers["location"]
    assert consent.startswith(PUBLIC + CONSENT_PATH + "?request=")
    page = await c.get(consent)
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    request_id = parse_qs(urlsplit(consent).query)["request"][0]
    assert "name='control'" not in page.text and "name='window'" in page.text
    form = {"request": request_id, "credential": runtime.auth.issue(), "decision": "allow", "window": window}
    r = await c.post(CONSENT_PATH, data=form, headers={"Origin": PUBLIC})
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(CALLBACK) and query["state"] == ["abc"]
    return query["code"][0], verifier


async def tokens(c, client_id, code, verifier):
    r = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "resource": PUBLIC + "/mcp",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


async def connect(app, access):
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=PUBLIC,
        headers={"Authorization": f"Bearer {access}"},
    )
    return Client(streamable_http_client(PUBLIC + "/mcp", http_client=http)), http


async def test_discovery_registration_consent_and_tool_scopes(remote):
    runtime, app = remote
    async with web(app) as c:
        prm = await c.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200 and prm.json()["authorization_servers"] == [PUBLIC]
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["registration_endpoint"] == PUBLIC + "/register"
        unauthenticated = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert unauthenticated.status_code == 401
        assert "oauth-protected-resource" in unauthenticated.headers["www-authenticate"]
        assert unauthenticated.headers["cache-control"] == "no-store"
        assert unauthenticated.headers["referrer-policy"] == "same-origin"
        assert (
            await c.get("/.well-known/oauth-authorization-server", headers={"Host": "evil.test"})
        ).status_code == 403

        client_id = await register(c)
        code, verifier = await authorize(c, runtime, client_id)
        issued = await tokens(c, client_id, code, verifier)
        # Approval grants the screen and input together; the client may narrow on refresh.
        assert issued["scope"] == "phone:read phone:control" and issued["expires_in"] <= 900
        reused = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": CALLBACK,
            },
        )
        assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
        narrowed = await narrow(c, PUBLIC, client_id, issued, drop="phone:control")
        assert narrowed.status_code == 200 and narrowed.json()["scope"] == "phone:read"

    client, http = await connect(app, narrowed.json()["access_token"])
    async with http, client:
        status = await client.call_tool("openlolo_status", {})
        assert not status.is_error and status.structured_content["mcp"]["transport"] == "streamable-http"
        shot = await client.call_tool("openlolo_screenshot", {})
        assert not shot.is_error
        image = next(b for b in shot.content if b.type == "image")
        assert base64.b64decode(image.data).startswith(b"\xff\xd8")
        denied = await client.call_tool(
            "openlolo_control", {"command": "acquire", "operation_id": str(uuid4())}
        )
        assert denied.is_error and denied.structured_content["error"]["code"] == "SCOPE_REQUIRED"
        assert (await client.call_tool("openlolo_pause", {"operation_id": str(uuid4())})).is_error is False
    assert runtime.coordinator.lease is None


async def test_control_scope_flow_rotation_reuse_and_revocation(remote, caplog):
    runtime, app = remote
    TIMING_LOG.addHandler(caplog.handler)  # the entry logger does not propagate to the root logger
    async with web(app) as c:
        client_id = await register(c)
        code, verifier = await authorize(c, runtime, client_id, window="1h")
        issued = await tokens(c, client_id, code, verifier)
        assert issued["scope"] == "phone:read phone:control"

        client, http = await connect(app, issued["access_token"])
        async with http, client:
            # No explicit acquire: the first action takes the lease for this OAuth client.
            assert runtime.coordinator.lease is None
            shot = await client.call_tool("openlolo_screenshot", {"format": "jpeg", "max_size": 512})
            frame = shot.structured_content["metadata"]
            tapped = await client.call_tool(
                "openlolo_tap",
                {
                    "operation_id": str(uuid4()),
                    "frame_id": frame["frame_id"],
                    "geometry_epoch": frame["geometry_epoch"],
                    "x": 0.5,
                    "y": 0.5,
                },
            )
            assert not tapped.is_error and tapped.structured_content["operation"]["state"] == "SUCCEEDED"
            assert runtime.coordinator.lease is not None
            assert "lease_token" not in str(tapped.structured_content)
            timed = [r.getMessage() for r in caplog.records if r.name == "openlolo.mcp.entry"]
            assert any(m.startswith("tools/call openlolo_tap ") and "status=200" in m for m in timed), timed
            assert not any("frame_id" in m or "0.5" in m for m in timed)
            assert runtime.coordinator.lease is not None

        refreshed = await c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        fresh = refreshed.json()
        assert fresh["access_token"] != issued["access_token"]
        # Old access token stays valid until it expires; rotated refresh token is dead.
        replay = await c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": client_id,
            },
        )
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
        # Reuse revoked the whole family, including the fresh access token.
        client, http = await connect(app, fresh["access_token"])
        async with http:
            with pytest.raises(Exception):
                async with client:
                    await client.call_tool("openlolo_status", {})

        # Approve again, then revoke from the CLI path: the sweeper drops the lease.
        code, verifier = await authorize(c, runtime, client_id)
        again = await tokens(c, client_id, code, verifier)
        client, http = await connect(app, again["access_token"])
        async with http, client:
            acquired = await client.call_tool(
                "openlolo_control", {"command": "acquire", "operation_id": str(uuid4())}
            )
            assert not acquired.is_error
            assert app.provider.revoke_client(client_id)
            await app.gateway.sweep()
            assert runtime.coordinator.lease is None
            # The bearer token is dead at the middleware: 401 before any tool runs.
            with pytest.raises(Exception):
                await client.call_tool("openlolo_status", {})
        assert app.provider.list_clients() == []


async def test_no_scope_request_offers_registered_scopes(remote):
    runtime, app = remote
    async with web(app) as c:
        client_id = await register(c, scope=None)  # registration falls back to default scopes
        _, challenge = pkce()
        started = await c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        page = await c.get(started.headers["location"])
        assert "touch, type and press buttons" in page.text and "name='window'" in page.text
        # A client registered for read only (Claude copies the 401 challenge's scope) is still
        # offered control; the owner decides.
        read_only = await register(c, scope="phone:read")
        started = await c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": read_only,
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "phone:read",
            },
        )
        page = await c.get(started.headers["location"])
        assert "touch, type and press buttons" in page.text
        # A client that only asks for read still gets control offered; the owner decides.
        started = await c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "phone:read",
            },
        )
        page = await c.get(started.headers["location"])
        assert "touch, type and press buttons" in page.text


async def test_consent_rejections_and_pkce_downgrade(remote):
    runtime, app = remote
    async with web(app) as c:
        client_id = await register(c)
        _, challenge = pkce()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "state": "s",
        }
        plain = await c.get("/authorize", params={**params, "code_challenge_method": "plain"})
        assert plain.status_code in {302, 400}
        if plain.status_code == 302:
            assert "error=" in plain.headers["location"]
        wrong_target = await c.get(
            "/authorize",
            params={**params, "code_challenge_method": "S256", "resource": "https://other.test/mcp"},
        )
        assert "error=invalid_target" in wrong_target.headers["location"]
        started = await c.get("/authorize", params={**params, "code_challenge_method": "S256"})
        request_id = parse_qs(urlsplit(started.headers["location"]).query)["request"][0]
        cross_site = await c.post(
            CONSENT_PATH,
            data={"request": request_id, "credential": "x", "decision": "allow"},
            headers={"Origin": "https://evil.test"},
        )
        assert cross_site.status_code == 403
        bad = await c.post(
            CONSENT_PATH,
            data={"request": request_id, "credential": "not-a-credential", "decision": "allow"},
            headers={"Origin": PUBLIC},
        )
        assert bad.status_code == 401 and "Not accepted." in bad.text
        denied = await c.post(
            CONSENT_PATH, data={"request": request_id, "decision": "deny"}, headers={"Origin": PUBLIC}
        )
        assert denied.status_code == 302 and "error=access_denied" in denied.headers["location"]
        expired = await c.get(CONSENT_PATH, params={"request": request_id})
        assert expired.status_code == 400
        query_token = await c.post(
            "/mcp?access_token=abc", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert query_token.status_code == 401


async def test_expired_access_token_and_pending_client_pruning(remote):
    runtime, app = remote
    async with web(app) as c:
        client_id = await register(c)
        code, verifier = await authorize(c, runtime, client_id)
        issued = await tokens(c, client_id, code, verifier)
        runtime.journal.db.execute(
            "UPDATE oauth_grants SET expires=? WHERE kind='access'", (time.time() - 1,)
        )
        runtime.journal.db.commit()
        client, http = await connect(app, issued["access_token"])
        async with http:
            with pytest.raises(Exception):
                async with client:
                    await client.call_tool("openlolo_status", {})
        pending = await register(c)
        runtime.journal.db.execute(
            "UPDATE oauth_clients SET created=? WHERE client_id=?", (time.time() - 700, pending)
        )
        runtime.journal.db.commit()
        listed = {row["client_id"] for row in app.provider.list_clients()}
        assert client_id in listed and pending not in listed
