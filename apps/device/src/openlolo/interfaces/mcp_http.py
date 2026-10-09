"""Remote MCP entry: embedded OAuth 2.1 server, bearer-gated Streamable HTTP, one bridge per client.

This is the only public door into the device (ADR 0003). The tunnel terminates TLS and
forwards to this loopback listener; everything security-relevant happens here or in the
unchanged Phone API behind it. Tokens are stored as digests only and never logged.
"""

import asyncio
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.auth import digest
from openlolo.interfaces.http_api import create_app
from openlolo.interfaces.mcp_client import BridgeError, PhoneClient
from openlolo.interfaces.mcp_server import (
    ADMIN_SCOPE,
    ALL_SCOPES,
    CONTROL_SCOPE,
    READ_SCOPE,
    SCOPES,
    create_server,
)

MCP_PATH = "/mcp"
# One line per MCP request on the public listener: JSON-RPC method, tool name, wall time on the box
# and payload sizes. Compared with a result's timing.bridge_seconds it shows what the framework and
# serialization cost here; anything beyond it is the tunnel or the client.
TIMING_LOG = logging.getLogger("openlolo.mcp.entry")
CONSENT_PATH = "/oauth/consent"
ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 30 * 24 * 3600
CODE_SECONDS = 5 * 60
PENDING_SECONDS = 10 * 60
PENDING_CLIENTS = 20
REGISTRATIONS_PER_MINUTE = 10
CALLS_PER_MINUTE = 120
ADMIN_CALLS_PER_MINUTE = 300  # The box page polls status while the owner has it open.
MAX_BODY = 65536
PIN_SECONDS = 30
PIN_ATTEMPTS = 5
STATUS_PATH = "/oauth/consent/status"
CONTROL_WINDOWS = {"1h": 3600, "8h": 8 * 3600, "24h": 24 * 3600}
# An owner-administering client may keep control for the refresh-family lifetime (ADR 0005).
ADMIN_WINDOWS = {**CONTROL_WINDOWS, "30d": REFRESH_SECONDS}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _token() -> str:
    return secrets.token_urlsafe(32)


class OAuthProvider:
    """SQLite-backed OAuthAuthorizationServerProvider; the SDK enforces PKCE S256 and grant rules.

    Grants and clients are bound to the issuer that created them, so a token minted by the
    remote (`public_url`) entry is never accepted by the LAN entry or vice versa even though both
    instances share one journal. Rows written before issuer binding existed belong to
    ``legacy_issuer`` (the remote entry).
    """

    def __init__(
        self,
        journal,
        auth,
        issuer: str,
        *,
        scopes: list[str] | None = None,
        legacy_issuer: str | None = None,
    ):
        self.db = journal.db
        self.auth = auth
        self.issuer = issuer
        self.resource = issuer + MCP_PATH
        self.scopes = list(scopes or SCOPES)
        self.legacy_issuer = legacy_issuer
        self.registrations: list[float] = []
        self.pin_attempts: dict[str, int] = {}

    def _owns(self, issuer: str | None) -> bool:
        return (issuer if issuer is not None else self.legacy_issuer) == self.issuer

    # -- storage helpers -------------------------------------------------------------

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute(
                "DELETE FROM oauth_clients WHERE approved=0 AND created<?", (now - PENDING_SECONDS,)
            )
            self.db.execute("DELETE FROM oauth_grants WHERE expires<?", (now,))

    def _grant(self, kind: str, token: str, now: float | None = None):
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT * FROM oauth_grants WHERE digest=? AND kind=?", (digest(token), kind)
        ).fetchone()
        if row is None or row["expires"] <= now:
            return None
        if not self._owns(json.loads(row["value"]).get("issuer")):
            return None
        return row

    def _store(
        self,
        kind: str,
        token: str,
        client_id: str,
        family: str,
        value: dict,
        expires: float,
        issuer: str | None = None,
    ):
        with self.db:
            self.db.execute(
                "INSERT INTO oauth_grants (digest, kind, client_id, family, value, expires) VALUES (?,?,?,?,?,?)",
                (
                    digest(token),
                    kind,
                    client_id,
                    family,
                    json.dumps({**value, "issuer": issuer or self.issuer}),
                    expires,
                ),
            )

    def _row(self, kind: str, key: str, now: float | None = None):
        """Row by digest, any issuer (owner approval spans both entries)."""
        now = time.time() if now is None else now
        row = self.db.execute("SELECT * FROM oauth_grants WHERE digest=? AND kind=?", (key, kind)).fetchone()
        if row is None or row["expires"] <= now or row["revoked"]:
            return None
        return row

    def revoke_family(self, family: str) -> None:
        with self.db:
            self.db.execute("UPDATE oauth_grants SET revoked=1 WHERE family=?", (family,))

    def revoke_client(self, client_id: str) -> bool:
        with self.db:
            self.db.execute("UPDATE oauth_grants SET revoked=1 WHERE client_id=?", (client_id,))
            deleted = self.db.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,)).rowcount
        return deleted > 0

    def client_active(self, client_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT 1 FROM oauth_clients c WHERE c.client_id=? AND c.approved=1 AND EXISTS ("
            "SELECT 1 FROM oauth_grants g WHERE g.client_id=c.client_id AND g.kind='refresh' "
            "AND g.revoked=0 AND g.expires>?)",
            (client_id, now),
        ).fetchone()
        return row is not None

    def list_clients(self, *, every_issuer: bool = False) -> list[dict[str, Any]]:
        self.prune()
        now = time.time()
        result = []
        for row in self.db.execute("SELECT * FROM oauth_clients ORDER BY created").fetchall():
            if not every_issuer and not self._owns(row["issuer"]):
                continue
            info = OAuthClientInformationFull.model_validate_json(row["value"])
            grants = self.db.execute(
                "SELECT kind, MAX(expires) AS expires FROM oauth_grants WHERE client_id=? AND revoked=0 "
                "AND expires>? GROUP BY kind",
                (row["client_id"], now),
            ).fetchall()
            result.append(
                {
                    "client_id": row["client_id"],
                    "client_name": info.client_name,
                    "redirect_uris": [str(u) for u in info.redirect_uris or []],
                    "approved": bool(row["approved"]),
                    "created": row["created"],
                    "active_until": {g["kind"]: g["expires"] for g in grants},
                    "issuer": row["issuer"] if row["issuer"] is not None else self.legacy_issuer,
                    "scopes": sorted(
                        {
                            s
                            for g in self.db.execute(
                                "SELECT value FROM oauth_grants WHERE client_id=? AND kind='refresh' "
                                "AND revoked=0 AND expires>?",
                                (row["client_id"], now),
                            ).fetchall()
                            for s in json.loads(g["value"]).get("scopes", [])
                        }
                    ),
                }
            )
        return result

    # -- OAuthAuthorizationServerProvider -------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        self.prune()
        row = self.db.execute(
            "SELECT value, issuer FROM oauth_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None or not self._owns(row["issuer"]):
            return None
        return OAuthClientInformationFull.model_validate_json(row["value"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        now = time.time()
        self.registrations = [t for t in self.registrations if t > now - 60]
        if len(self.registrations) >= REGISTRATIONS_PER_MINUTE:
            raise RegistrationError("invalid_client_metadata", "registration rate limit; retry later")
        self.prune(now)
        pending = self.db.execute("SELECT COUNT(*) FROM oauth_clients WHERE approved=0").fetchone()[0]
        if pending >= PENDING_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "too many pending registrations")
        for uri in client_info.redirect_uris or []:
            loopback = uri.scheme == "http" and uri.host in LOOPBACK_HOSTS
            if uri.scheme != "https" and not loopback:
                raise RegistrationError("invalid_redirect_uri", "redirect_uri must be https or loopback http")
        self.registrations.append(now)
        with self.db:
            self.db.execute(
                "INSERT INTO oauth_clients (client_id, value, approved, created, issuer) VALUES (?,?,0,?,?)",
                (client_info.client_id, client_info.model_dump_json(), now, self.issuer),
            )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource != self.resource:
            raise AuthorizeError("invalid_target", "token can only be issued for this device")
        # Clients send no scope (ChatGPT) or register with and repeat only the read scope the 401
        # challenge advertised (Claude). Always offer read and control: the owner decides on the
        # consent page, where control stays unchecked by default. The admin scope of the LAN entry is
        # offered only when the client registered for or requested it. The token response reports the
        # scope actually granted.
        requested = params.scopes or []
        if any(scope not in self.scopes for scope in requested):
            raise AuthorizeError("invalid_scope")
        registered = (client.scope or "").split()
        scopes = [
            scope for scope in self.scopes if scope in SCOPES or scope in requested or scope in registered
        ]
        request_id = _token()
        value = {"client_id": client.client_id, "scopes": scopes, "params": params.model_dump(mode="json")}
        self._store("request", request_id, client.client_id, request_id, value, time.time() + PENDING_SECONDS)
        return f"{self.issuer}{CONSENT_PATH}?request={request_id}"

    def pending_request(self, request_id: str) -> tuple[OAuthClientInformationFull, dict] | None:
        row = self._grant("request", request_id)
        if row is None or row["revoked"]:
            return None
        return self._pending_from_row(row)

    def _pending_from_row(self, row) -> tuple[OAuthClientInformationFull, dict] | None:
        value = json.loads(row["value"])
        client_row = self.db.execute(
            "SELECT value FROM oauth_clients WHERE client_id=?", (value["client_id"],)
        ).fetchone()
        if client_row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(client_row["value"]), value

    def pending_by_handle(self, handle: str) -> tuple[OAuthClientInformationFull, dict] | None:
        """Pending request by its handle (digest of the request id); any issuer."""
        row = self._row("request", handle)
        return self._pending_from_row(row) if row is not None else None

    def list_pending(self) -> list[dict[str, Any]]:
        """Every pending consent request on this box, for the owner's app (any issuer)."""
        self.prune()
        now = time.time()
        result = []
        for row in self.db.execute(
            "SELECT * FROM oauth_grants WHERE kind='request' AND revoked=0 AND expires>? ORDER BY expires",
            (now,),
        ).fetchall():
            pending = self._pending_from_row(row)
            if pending is None:
                continue
            client, value = pending
            result.append(
                {
                    "handle": row["digest"],
                    "client_id": client.client_id,
                    "client_name": client.client_name,
                    "redirect_hosts": sorted({str(u.host or "") for u in client.redirect_uris or []}),
                    "scopes": value["scopes"],
                    "issuer": value.get("issuer", self.legacy_issuer),
                    "expires_in": max(0, int(row["expires"] - now)),
                    "created": row["expires"] - PENDING_SECONDS,
                }
            )
        return result

    def decide(self, request_id: str, granted: list[str] | None, window: int) -> str:
        """Consume the pending request after owner proof; return the client redirect URL."""
        return self.decide_handle(digest(request_id), granted, window)

    def decide_handle(self, handle: str, granted: list[str] | None, window: int) -> str:
        pending = self.pending_by_handle(handle)
        if pending is None:
            raise OpenLoloError("CONSENT_EXPIRED", "Start the connection again from the client", 400)
        client, value = pending
        params = AuthorizationParams.model_validate(value["params"])
        issuer = value.get("issuer", self.legacy_issuer)
        with self.db:
            self.db.execute("DELETE FROM oauth_grants WHERE digest=?", (handle,))
            self.db.execute("DELETE FROM oauth_grants WHERE kind='pin' AND family=?", (handle,))
        if not granted:
            redirect = construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            )
            self.record_decision(handle, client.client_id, redirect)
            return redirect
        code = _token()
        self._store(
            "code",
            code,
            client.client_id,
            code,
            {
                "scopes": granted,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "window": window,
            },
            time.time() + CODE_SECONDS,
            issuer=issuer,
        )
        with self.db:
            self.db.execute("UPDATE oauth_clients SET approved=1 WHERE client_id=?", (client.client_id,))
        redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        self.record_decision(handle, client.client_id, redirect)
        return redirect

    # -- owner approval from the box page: decisions the consent page polls for, and PINs ----

    def record_decision(self, handle: str, client_id: str, redirect: str) -> None:
        self._store(
            "decision",
            "decision:" + handle,
            client_id,
            handle,
            {"redirect": redirect},
            time.time() + CODE_SECONDS,
        )

    def take_decision(self, request_id: str) -> str | None:
        """The redirect for a decided request, once; None while still pending."""
        key = digest("decision:" + digest(request_id))
        row = self._row("decision", key)
        if row is None:
            return None
        with self.db:
            self.db.execute("DELETE FROM oauth_grants WHERE digest=?", (key,))
        return json.loads(row["value"])["redirect"]

    def issue_pin(self, handle: str, choice: dict) -> dict:
        """Six-digit PIN bound to one pending request and the owner's grant choice; 30 s."""
        pending = self.pending_by_handle(handle)
        if pending is None:
            raise OpenLoloError("CONSENT_EXPIRED", status=404)
        client, _ = pending
        pin = f"{secrets.randbelow(10**6):06d}"
        with self.db:
            self.db.execute("DELETE FROM oauth_grants WHERE kind='pin' AND family=?", (handle,))
        self._store("pin", f"pin:{handle}:{pin}", client.client_id, handle, choice, time.time() + PIN_SECONDS)
        return {"pin": pin, "expires_in": PIN_SECONDS}

    def redeem_pin(self, request_id: str, pin: str) -> dict:
        """Grant choice bound to a PIN for this request; 5 wrong tries invalidate the request."""
        handle = digest(request_id)
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{6}", pin):
            raise OpenLoloError("INVALID_CREDENTIAL", "PIN must be six digits", 401)
        row = self._row("pin", digest(f"pin:{handle}:{pin}"))
        if row is None:
            attempts = self.pin_attempts.get(handle, 0) + 1
            self.pin_attempts[handle] = attempts
            if attempts >= PIN_ATTEMPTS:
                with self.db:
                    self.db.execute(
                        "DELETE FROM oauth_grants WHERE digest=? OR (kind='pin' AND family=?)",
                        (handle, handle),
                    )
                self.pin_attempts.pop(handle, None)
                raise OpenLoloError(
                    "CONSENT_EXPIRED", "Too many wrong PINs; start again from the client", 400
                )
            raise OpenLoloError("INVALID_CREDENTIAL", "PIN not accepted", 401)
        self.pin_attempts.pop(handle, None)
        value = json.loads(row["value"])
        return {
            "admin": bool(value.get("admin")),
            "window": str(value.get("window", "1h")),
        }

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._grant("code", authorization_code)
        if row is None or row["revoked"] or row["client_id"] != client.client_id:
            return None
        value = json.loads(row["value"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=value["scopes"],
            expires_at=row["expires"],
            client_id=client.client_id,
            code_challenge=value["code_challenge"],
            redirect_uri=AnyUrl(value["redirect_uri"]),
            redirect_uri_provided_explicitly=value["redirect_uri_provided_explicitly"],
            resource=self.resource,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        row = self._grant("code", authorization_code.code)
        with self.db:
            deleted = self.db.execute(
                "DELETE FROM oauth_grants WHERE digest=? AND kind='code'", (digest(authorization_code.code),)
            ).rowcount
        if row is None or deleted == 0 or row["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        value = json.loads(row["value"])
        now = time.time()
        family = secrets.token_hex(16)
        family_expires = now + min(REFRESH_SECONDS, value["window"])
        return self._issue(client.client_id, value["scopes"], family, family_expires, now)

    def _issue(self, client_id: str, scopes: list[str], family: str, family_expires: float, now: float):
        access, refresh = _token(), _token()
        access_expires = min(now + ACCESS_SECONDS, family_expires)
        value = {"scopes": scopes, "family_expires": family_expires}
        self._store("access", access, client_id, family, value, access_expires)
        self._store("refresh", refresh, client_id, family, value, min(now + REFRESH_SECONDS, family_expires))
        return OAuthToken(
            access_token=access,
            expires_in=max(1, int(access_expires - now)),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = self._grant("refresh", refresh_token)
        if row is None:
            return None
        if row["revoked"]:
            # A rotated refresh token presented again means it leaked: end the whole family.
            self.revoke_family(row["family"])
            return None
        if row["client_id"] != client.client_id:
            return None
        value = json.loads(row["value"])
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=value["scopes"],
            expires_at=int(row["expires"]),
            resource=self.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self._grant("refresh", refresh_token.token)
        if row is None or row["revoked"] or row["client_id"] != client.client_id:
            raise TokenError("invalid_grant")
        now = time.time()
        value = json.loads(row["value"])
        if value["family_expires"] <= now:
            self.revoke_family(row["family"])
            raise TokenError("invalid_grant", "grant expired; the owner must approve again")
        with self.db:
            self.db.execute(
                "UPDATE oauth_grants SET revoked=1 WHERE digest=?", (digest(refresh_token.token),)
            )
        return self._issue(
            client.client_id, scopes or value["scopes"], row["family"], value["family_expires"], now
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._grant("access", token)
        if row is None or row["revoked"]:
            return None
        value = json.loads(row["value"])
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=value["scopes"],
            expires_at=int(row["expires"]),
            resource=self.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self.db.execute(
            "SELECT family FROM oauth_grants WHERE digest=?", (digest(token.token),)
        ).fetchone()
        if row is not None:
            self.revoke_family(row["family"])


class Gateway:
    """Resolves the caller's PhoneClient; one internal Phone API session and lease per OAuth client."""

    def __init__(self, runtime, provider: OAuthProvider):
        self.runtime = runtime
        self.provider = provider
        self.api = create_app(runtime, False)
        self.base_url = "http://" + runtime.config.allowed_hosts[0]
        self.bridges: dict[str, tuple[str, PhoneClient]] = {}
        self.calls: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()
        self.sweeper: asyncio.Task | None = None

    async def bridge(self, scope: str, *, exempt: bool = False) -> PhoneClient:
        token = get_access_token()
        if token is None:
            raise BridgeError("LOGIN_REQUIRED")
        if scope not in token.scopes:
            raise BridgeError("SCOPE_REQUIRED")
        now = time.time()
        recent = [t for t in self.calls.get(token.client_id, []) if t > now - 60]
        limit = ADMIN_CALLS_PER_MINUTE if ADMIN_SCOPE in token.scopes else CALLS_PER_MINUTE
        if len(recent) >= limit and not exempt:  # Pause must never be throttled.
            raise BridgeError("RATE_LIMITED")
        if not exempt:
            recent.append(now)
        self.calls[token.client_id] = recent
        async with self.lock:
            entry = self.bridges.get(token.client_id)
            if entry is not None:
                try:
                    self.runtime.auth.owner(entry[0])
                    return entry[1]
                except OpenLoloError:
                    await self._drop(token.client_id)
            session = self.runtime.auth.login(self.runtime.auth.issue())
            client_info = await self.provider.get_client(token.client_id)
            label = (client_info.client_name if client_info and client_info.client_name else token.client_id)[
                :64
            ]
            self.runtime.journal.label_owner(digest(session), label)
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.api),
                base_url=self.base_url,
                headers={"X-OpenLolo-Client": "cli"},
                cookies={"openlolo_session": session},
            )
            client = PhoneClient(http, transport="streamable-http")
            self.bridges[token.client_id] = (session, client)
            return client

    async def _drop(self, client_id: str) -> None:
        entry = self.bridges.pop(client_id, None)
        if entry is None:
            return
        session, client = entry
        await client.close()
        who = digest(session)
        lease = self.runtime.coordinator.lease
        if lease and lease.owner == who:
            await self.runtime.coordinator.drop_control()
        self.runtime.auth.logout(session)

    async def sweep(self) -> None:
        """Drop bridges whose client was revoked from the CLI or whose grants all expired."""
        async with self.lock:
            for client_id in list(self.bridges):
                if not self.provider.client_active(client_id):
                    await self._drop(client_id)

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.sweep()

    def start(self) -> None:
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self._sweep_loop())

    async def close(self) -> None:
        if self.sweeper is not None:
            self.sweeper.cancel()
            self.sweeper = None
        async with self.lock:
            for client_id in list(self.bridges):
                await self._drop(client_id)


def _rpc_label(body: bytes) -> str:
    """'tools/call openlolo_tap' from a JSON-RPC request body; never any argument or result content."""
    try:
        data = json.loads(body)
    except ValueError:
        return "mcp"
    if not isinstance(data, dict):
        return "mcp batch"
    method = data.get("method")
    if not isinstance(method, str):
        return "mcp response"
    name = data.get("params", {}).get("name") if isinstance(data.get("params"), dict) else None
    return f"{method} {name}" if isinstance(name, str) else method


class EntrySecurity:
    """Host pinning, body cap, hardening headers, and gateway lifecycle for the public listener."""

    def __init__(self, app, hosts: set[str], gateway: Gateway, provider: OAuthProvider):
        self.app, self.hosts, self.gateway, self.provider = app, hosts, gateway, provider

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(scope, receive, send)
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if headers.get("host", "").lower() not in self.hosts:
            return await JSONResponse({"error": {"code": "HOST_REJECTED"}}, status_code=403)(
                scope, receive, send
            )
        if int(headers.get("content-length") or 0) > MAX_BODY:
            return await JSONResponse({"error": {"code": "REQUEST_TOO_LARGE"}}, status_code=413)(
                scope, receive, send
            )
        received = sent = 0
        timed = scope.get("path") == MCP_PATH and scope.get("method") == "POST"
        started = time.monotonic()
        body = bytearray()
        status = 0

        async def capped_receive():
            # Chunked uploads carry no Content-Length; cut them off as a disconnect past the cap.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY:
                    return {"type": "http.disconnect"}
                if timed:
                    body.extend(message.get("body", b""))
            return message

        async def secure_send(message):
            nonlocal sent, status
            if timed and message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
                if not message.get("more_body"):
                    TIMING_LOG.info(
                        "%s %.3fs status=%s in=%dB out=%dB",
                        _rpc_label(bytes(body)),
                        time.monotonic() - started,
                        status,
                        received,
                        sent,
                    )
            if message["type"] == "http.response.start":
                status = message.get("status", 0)
                headers = message.setdefault("headers", [])
                nonce = next((v.decode() for k, v in headers if k == b"x-openlolo-nonce"), None)
                message["headers"] = [(k, v) for k, v in headers if k != b"x-openlolo-nonce"]
                script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
                message["headers"].extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        # "no-referrer" would make browsers send "Origin: null" on the consent form POST.
                        (b"referrer-policy", b"same-origin"),
                        (b"strict-transport-security", b"max-age=31536000"),
                        (
                            b"content-security-policy",
                            (
                                f"default-src 'none'; style-src 'unsafe-inline'; {script}"
                                "form-action 'self' https:; frame-ancestors 'none'; base-uri 'none'"
                            ).encode(),
                        ),
                    ]
                )
            await send(message)

        await self.app(scope, capped_receive, secure_send)

    async def _lifespan(self, scope, receive, send):
        async def wrapped_receive():
            message = await receive()
            if message["type"] == "lifespan.startup":
                self.gateway.start()
            elif message["type"] == "lifespan.shutdown":
                await self.gateway.close()
            return message

        await self.app(scope, wrapped_receive, send)


SCOPE_WORDS = {
    READ_SCOPE: "see the screen and status",
    CONTROL_SCOPE: "touch, type and press buttons",
    ADMIN_SCOPE: "manage this OpenLolo (Wi-Fi, onboarding, other clients)",
}


def _page(title: str, body: str, status: int = 200, script: str | None = None, nonce: str | None = None):
    scripts = f"<script nonce='{nonce}'>{script}</script>" if script and nonce else ""
    response = HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width'><title>OpenLolo</title><style>body{font:16px system-ui;max-width:32rem;"
        "margin:2rem auto;padding:0 1rem}fieldset{margin:1rem 0}button{font:inherit;padding:.5rem 1rem}"
        ".warn{background:#fff3cd;padding:.75rem;border-radius:.5rem}.qr{width:12rem;height:12rem;margin:1rem auto}"
        ".qr svg{width:100%;height:100%}.muted{opacity:.7}details{margin-top:1.5rem}code{word-break:break-all}"
        "input.pin{font:1.5rem monospace;letter-spacing:.3em;width:8em;text-align:center}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>{body}{scripts}</body></html>",
        status_code=status,
    )
    if nonce:
        response.headers["x-openlolo-nonce"] = nonce
    return response


def approve_link(setup_name: str | None, handle: str) -> str | None:
    """Opens the request on the box page (ADR 0012), reachable on the local network."""
    return f"http://{setup_name.lower()}.local/access?r={handle}" if setup_name else None


def qr_svg(payload: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg as svg
    except ImportError:
        return "<p class='muted'>QR unavailable on this box.</p>"
    image = qrcode.make(payload, image_factory=svg.SvgPathImage, box_size=6, border=1)
    return image.to_string(encoding="unicode")


def consent_form(
    request_id: str,
    client: OAuthClientInformationFull,
    scopes: list[str],
    error: str | None,
    *,
    setup_name: str | None = None,
    nonce: str | None = None,
):
    """Owner approval page: approve from the box page (QR or pending list), a PIN from it, or a credential."""
    name = html.escape(client.client_name or client.client_id)
    hosts = ", ".join(sorted({html.escape(str(u.host or "")) for u in client.redirect_uris or []}))
    admin = ADMIN_SCOPE in scopes
    windows = ADMIN_WINDOWS if admin else CONTROL_WINDOWS
    link = approve_link(setup_name, digest(request_id))
    wants = "".join(f"<li>{html.escape(SCOPE_WORDS.get(scope, scope))}</li>" for scope in scopes)
    body = (
        f"<p><strong>{name}</strong> wants to use your iPhone through OpenLolo. "
        f"After approval it returns to <code>{hosts}</code>.</p>"
        f"<p>Approving lets it:</p><ul>{wants}</ul>"
        "<p>You choose for how long. Approving from the box page or with a PIN uses the duration chosen there.</p>"
        "<p class='warn'>Screen contents, including messages, notifications and codes, will be sent to "
        "this client and its AI provider. Phone content is untrusted input; the AI can be misled by it.</p>"
        + (f"<p class='warn'>{html.escape(error)}</p>" if error else "")
        + (
            "<fieldset><legend>Approve from the OpenLolo page</legend>"
            "<p>Open the box page on your phone: this request is listed under <em>Access</em>. "
            "Or scan this code with the phone's camera while it is on the same Wi-Fi.</p>"
            f"<div class='qr'>{qr_svg(link)}</div>"
            f"<p>Reading this on the phone itself? <a href='{html.escape(link)}'>Open the OpenLolo page</a>.</p>"
            "<p id='wait' class='muted'>Waiting for a decision on the box page…</p></fieldset>"
            if link
            else ""
        )
        + f"<form method='post' action='{CONSENT_PATH}'>"
        f"<input type='hidden' name='request' value='{html.escape(request_id)}'>"
        "<fieldset><legend>Or enter the PIN from the box page</legend>"
        "<p>On the box page, open this request, choose the access duration and tap <em>Get PIN</em>.</p>"
        "<label>PIN <input class='pin' name='pin' inputmode='numeric' pattern='[0-9]{6}' maxlength='6' "
        "autocomplete='one-time-code'></label> "
        "<button name='decision' value='pin'>Approve with PIN</button></fieldset>"
        "<details><summary>Advanced: approve with a single-use credential from the box's shell</summary>"
        "<fieldset><legend>Access</legend>"
        f"<p>Screen and status ({READ_SCOPE}) and input ({CONTROL_SCOPE}) for the chosen duration.</p>"
        "<label>Duration <select name='window'>"
        + "".join(f"<option value='{k}'>{k}</option>" for k in windows)
        + "</select></label>"
        + (
            f"<br><label><input type='checkbox' name='admin' value='1'> Manage this OpenLolo: Wi-Fi, "
            f"onboarding, other clients, diagnostics ({ADMIN_SCOPE})</label>"
            if admin
            else ""
        )
        + "</fieldset>"
        "<fieldset><legend>Owner proof</legend>"
        "<label>Single-use credential from <code>openlolo credential</code><br>"
        "<input type='password' name='credential' autocomplete='off' maxlength='256'></label>"
        "</fieldset>"
        "<button name='decision' value='allow'>Allow</button> "
        "<button name='decision' value='deny' formnovalidate>Deny</button></details></form>"
    )
    script = (
        f"const r={json.dumps(request_id)};"
        f"async function poll(){{try{{const x=await fetch({json.dumps(STATUS_PATH)}+'?request='+encodeURIComponent(r),"
        "{cache:'no-store',credentials:'omit'});const d=await x.json();"
        "if(d.state==='decided'&&d.redirect){location.href=d.redirect;return;}"
        "if(d.state==='gone'){document.getElementById('wait').textContent='This request expired. Start again from the client.';return;}}"
        "catch(e){}setTimeout(poll,2000);}poll();"
    )
    return _page(
        "Connect to OpenLolo", body, 401 if error else 200, script=script if link else None, nonce=nonce
    )


async def read_form(request: Request, limit: int = 8192) -> dict[str, str]:
    """Parse a small urlencoded form without pulling in multipart support."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise OpenLoloError("REQUEST_TOO_LARGE", status=413)
    parsed = parse_qs(data.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=16)
    return {key: values[0] for key, values in parsed.items()}


def grant_from_choice(offered: list[str], admin: bool, window: str) -> tuple[list[str], int]:
    """Translate the owner's consent choice into granted scopes and the grant's lifetime.

    Approval always grants the screen and input together (ADR 0003, amendment 2026-09-30): a
    client that may only watch the phone has no use, and the bounded duration is the safety
    lever. The admin scope of the LAN entry stays a separate choice. A client can still narrow
    a grant itself with the ``scope`` parameter of a refresh."""
    granted = [scope for scope in SCOPES if scope in offered] or [READ_SCOPE]
    admin = admin and ADMIN_SCOPE in offered
    windows = ADMIN_WINDOWS if admin else CONTROL_WINDOWS
    seconds = windows.get(window, CONTROL_WINDOWS["1h"])
    if admin:
        granted.append(ADMIN_SCOPE)
    return granted, seconds


def consent_routes(provider: OAuthProvider, origin: str, runtime) -> Callable[[Request], Any]:
    setup_name = runtime.config.setup_name

    async def consent(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = provider.pending_request(request_id) if request_id else None
            if pending is None:
                return _page("Request expired", "<p>Start the connection again from the client.</p>", 400)
            client, value = pending
            return consent_form(
                request_id,
                client,
                value["scopes"],
                None,
                setup_name=setup_name,
                nonce=secrets.token_urlsafe(12),
            )
        content_type = request.headers.get("content-type", "")
        sent = request.headers.get("origin")
        if sent != origin or not content_type.startswith("application/x-www-form-urlencoded"):
            return _page("Rejected", "<p>Cross-site form submission rejected.</p>", 403)
        form = await read_form(request)
        request_id = form.get("request", "")
        pending = provider.pending_request(request_id) if request_id else None
        if pending is None:
            return _page("Request expired", "<p>Start the connection again from the client.</p>", 400)
        client, value = pending
        page = lambda message: consent_form(  # noqa: E731
            request_id,
            client,
            value["scopes"],
            message,
            setup_name=setup_name,
            nonce=secrets.token_urlsafe(12),
        )
        if form.get("decision") == "deny":
            return RedirectResponse(provider.decide(request_id, None, 0), status_code=302)
        try:
            if form.get("decision") == "pin":
                choice = provider.redeem_pin(request_id, form.get("pin", ""))
                granted, seconds = grant_from_choice(value["scopes"], choice["admin"], choice["window"])
            else:
                provider.auth.verify(form.get("credential"))
                granted, seconds = grant_from_choice(
                    value["scopes"], form.get("admin") == "1", form.get("window", "")
                )
        except OpenLoloError as exc:
            if exc.status == 400:
                return _page("Request expired", f"<p>{html.escape(exc.message)}</p>", 400)
            return page("Too many attempts; wait a minute." if exc.status == 429 else "Not accepted.")
        return RedirectResponse(provider.decide(request_id, granted, seconds), status_code=302)

    return consent


def status_route(provider: OAuthProvider) -> Callable[[Request], Any]:
    """Polled by the consent page: pending, decided (with the redirect, once), or gone."""

    async def status(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        if not request_id or len(request_id) > 256:
            return JSONResponse({"state": "gone"})
        redirect = provider.take_decision(request_id)
        if redirect is not None:
            return JSONResponse({"state": "decided", "redirect": redirect})
        if provider.pending_request(request_id) is None:
            return JSONResponse({"state": "gone"})
        return JSONResponse({"state": "pending"})

    return status


def create_gateway_app(
    runtime, origin: str | None = None, *, allowed_hosts: list[str] | None = None, app_routes: bool = False
):
    """Starlette app for one OAuth/MCP entry.

    ``origin`` is the issuer this instance serves; it defaults to ``config.public_url`` (the remote
    entry behind the tunnel). The LAN entry (ADR 0005) passes ``config.lan_url``, the extra Host
    values it accepts, and ``app_routes=True`` to mount ``/app/*`` and offer the ``owner:admin``
    scope.
    """
    config = runtime.config
    origin = origin or config.public_url
    if not origin:
        raise ValueError("public_url is required for the remote MCP entry")
    hosts = {urlsplit(origin).netloc.lower(), *(h.lower() for h in allowed_hosts or [])}
    scopes = ALL_SCOPES if app_routes else SCOPES
    provider = OAuthProvider(
        runtime.journal, runtime.auth, origin, scopes=scopes, legacy_issuer=config.public_url
    )
    gateway = Gateway(runtime, provider)
    if not TIMING_LOG.handlers:
        # uvicorn runs at warning level; the timing line is the one info record this process emits.
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        TIMING_LOG.addHandler(handler)
        TIMING_LOG.setLevel(logging.INFO)
        TIMING_LOG.propagate = False
    server = create_server(
        resolver=gateway,
        auth_server_provider=provider,
        auth=AuthSettings(  # Strings keep the path-less issuer canonical (no trailing slash).
            issuer_url=origin,  # type: ignore[arg-type]
            resource_server_url=provider.resource,  # type: ignore[arg-type]
            validate_token_resource=True,
            # Registration scope is what a client may ask for; the owner grants per consent.
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=scopes, default_scopes=scopes
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[READ_SCOPE],
        ),
    )
    server.custom_route(CONSENT_PATH, methods=["GET", "POST"])(consent_routes(provider, origin, runtime))
    server.custom_route(STATUS_PATH, methods=["GET"])(status_route(provider))
    if app_routes:
        from openlolo.interfaces.app_api import install_app_routes

        install_app_routes(server, runtime, gateway, provider)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts), allowed_origins=[origin]
        ),
    )
    return EntrySecurity(app, hosts, gateway, provider)
