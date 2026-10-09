import asyncio
import base64
import contextlib
import hashlib
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openlolo.config import Config
from openlolo.domain.errors import OpenLoloError


@contextlib.contextmanager
def raises_code(code: str):
    """Assert an OpenLoloError by its stable code rather than its human message."""
    with pytest.raises(OpenLoloError) as info:
        yield info
    assert info.value.code == code, info.value.code


# -- OAuth helpers parameterized by issuer (the remote entry and the LAN entry share one Runtime) --

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LAN_CERT = FIXTURES / "tls" / "openlolo-test.crt"
LAN_KEY = FIXTURES / "tls" / "openlolo-test.key"
PUBLIC = "https://pi.tailnet.ts.net"
LAN = "https://openlolo-test.local:8443"
APP_CALLBACK = "https://admin.example/oauth/callback"  # An owner-administering LAN client.
CONSENT_PATH = "/oauth/consent"


def lan_config(tmp_path, **overrides):
    return Config(
        backend="simulated",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        public_url=PUBLIC,
        lan_url=LAN,
        lan_cert=LAN_CERT,
        lan_key=LAN_KEY,
        lan_extra_hosts=["192.168.4.25"],
        **overrides,
    )


def web(app, origin, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, **kwargs)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def register(
    c, *, callback=APP_CALLBACK, scope="phone:read phone:control owner:admin", name="Lolo App"
):
    metadata = {"client_name": name, "redirect_uris": [callback], "token_endpoint_auth_method": "none"}
    if scope is not None:
        metadata["scope"] = scope
    return await c.post("/register", json=metadata)


async def start_authorization(c, origin, client_id, *, callback=APP_CALLBACK, scope=None, state="abc"):
    """GET /authorize; return (response, request_id or None, verifier)."""
    verifier, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": origin + "/mcp",
    }
    if scope is not None:
        params["scope"] = scope
    r = await c.get("/authorize", params=params)
    request_id = None
    if r.status_code == 302 and r.headers["location"].startswith(origin + CONSENT_PATH + "?request="):
        request_id = parse_qs(urlsplit(r.headers["location"]).query)["request"][0]
    return r, request_id, verifier


async def consent_form(c, origin, request_id, credential, *, admin=True, window="30d", headers=None):
    """The browser consent form submitted with a single-use credential (same-origin POST)."""
    form = {"request": request_id, "decision": "allow", "window": window, "credential": credential}
    if admin:
        form["admin"] = "1"
    return await c.post(CONSENT_PATH, data=form, headers={"Origin": origin, **(headers or {})})


async def tokens(c, origin, client_id, code, verifier, *, callback=APP_CALLBACK):
    return await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": callback,
            "resource": origin + "/mcp",
        },
    )


async def app_token(c, runtime, *, control=True, admin=True, window="30d", name="Lolo App"):
    """Register, authorize and consent on the LAN entry; return (client_id, token response dict)."""
    registered = await register(c, name=name)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    _, request_id, verifier = await start_authorization(c, LAN, client_id)
    assert request_id
    consented = await consent_form(c, LAN, request_id, runtime.auth.issue(), admin=admin, window=window)
    assert consented.status_code == 302, consented.text
    code = parse_qs(urlsplit(consented.headers["location"]).query)["code"][0]
    issued = await tokens(c, LAN, client_id, code, verifier)
    assert issued.status_code == 200, issued.text
    if not control:
        # Approval always grants control; a client narrows its own grant on refresh.
        issued = await narrow(c, LAN, client_id, issued.json(), drop="phone:control")
        assert issued.status_code == 200, issued.text
    return client_id, issued.json()


async def narrow(c, origin, client_id, issued: dict, *, drop: str):
    """Refresh with a smaller scope: the OAuth way to hold a read-only token."""
    scope = " ".join(s for s in issued["scope"].split() if s != drop)
    return await c.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": client_id,
            "scope": scope,
            "resource": origin + "/mcp",
        },
    )


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


@contextlib.asynccontextmanager
async def running(app):
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(lifespan(app, ready, stop))
    await ready.wait()
    try:
        yield app
    finally:
        stop.set()
        await task
