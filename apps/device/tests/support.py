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
APP_CALLBACK = "https://agent.example/oauth/callback"  # A client of the LAN entry.
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


async def register(c, *, callback=APP_CALLBACK, scope="phone:read phone:control", name="Lolo App"):
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


def owner_approve(runtime, request_id, *, window="30d") -> str:
    """The owner taps Approve on the box page; return the client redirect the consent page polls for."""
    from openlolo.interfaces.auth import digest
    from openlolo.interfaces.mcp_http import grant_from_choice

    # Like /app/consents/{handle}: any entry's request, addressed by its handle.
    provider, handle = runtime.door_provider, digest(request_id)
    pending = provider.pending_by_handle(handle)
    assert pending is not None, "request expired"
    granted, seconds = grant_from_choice(pending[1]["scopes"], window)
    return provider.decide_handle(handle, granted, seconds)


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


async def lan_token(c, runtime, *, window="30d", name="Lolo App"):
    """Register, authorize and approve a client on the LAN entry; return (client_id, token response)."""
    registered = await register(c, name=name)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    _, request_id, verifier = await start_authorization(c, LAN, client_id)
    assert request_id
    code = parse_qs(urlsplit(owner_approve(runtime, request_id, window=window)).query)["code"][0]
    issued = await tokens(c, LAN, client_id, code, verifier)
    assert issued.status_code == 200, issued.text
    return client_id, issued.json()


PHONE_ADDRESS = ("192.168.1.77", 50000)  # The simulated phone's RemotePairing address.


def portal_build(tmp_path):
    """A minimal box page build for the listener to serve."""
    build = tmp_path / "portal"
    (build / "assets").mkdir(parents=True, exist_ok=True)
    (build / "index.html").write_text("<!doctype html><title>OpenLolo</title><div id=app></div>")
    return build


@contextlib.asynccontextmanager
async def box_page(app, *, client=PHONE_ADDRESS, session=True):
    """The box page as the paired phone on the cable sees it, with its session open."""
    from openlolo.interfaces.setup_portal import CLIENT_HEADER

    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://openlolo-test.local", headers={CLIENT_HEADER: "portal"}
    ) as c:
        if session:
            opened = await c.post("/setup/session")
            assert opened.status_code == 200, opened.text
        yield c


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
