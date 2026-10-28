"""Remote MCP entry: embedded OAuth 2.1 server, bearer-gated Streamable HTTP, one bridge per client.

This is the only public door into the device (ADR 0003). The tunnel terminates TLS and
forwards to this loopback listener; everything security-relevant happens here or in the phone
core it calls in-process. Tokens are stored as digests only and never logged.
"""

import asyncio
import base64
import functools
import html
import json
import logging
import secrets
import time
from collections.abc import Callable
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
from openlolo.interfaces.local_phone import BridgeError, LocalPhone
from openlolo.interfaces.mcp_server import (
    ADMIN_SCOPE,
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
# A rotated refresh token is accepted once more within this window, so a reply lost on the way
# to the client does not end its grant; any other reuse still revokes the whole family.
REFRESH_GRACE_SECONDS = 60
CODE_SECONDS = 5 * 60
PENDING_SECONDS = 10 * 60
PENDING_CLIENTS = 20
REGISTRATIONS_PER_MINUTE = 10
CALLS_PER_MINUTE = 120
ADMIN_CALLS_PER_MINUTE = 300  # The box page polls status while the owner has it open.
MAX_BODY = 65536
STATUS_PATH = "/oauth/consent/status"
# How long an approval lasts, the same choices for every client (ADR 0005). "24h" has a fixed end.
# "30d" is renewed while used: every refresh moves the end to now + REFRESH_SECONDS, so a client
# unused for 30 days loses access. "never" lasts until the owner removes the client (or resets
# the box); its end is a date no box will reach.
NEVER_SECONDS = 100 * 365 * 24 * 3600
CONTROL_WINDOWS = {"24h": 24 * 3600, "30d": REFRESH_SECONDS, "never": NEVER_SECONDS}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _token() -> str:
    return secrets.token_urlsafe(32)


class OAuthProvider:
    """SQLite-backed OAuthAuthorizationServerProvider; the SDK enforces PKCE S256 and grant rules.

    Grants and clients are bound to the issuer that created them, so a token minted by the
    remote (`public_url`) entry is never accepted by the LAN entry or vice versa even though both
    instances share one journal.
    """

    def __init__(
        self,
        journal,
        issuer: str,
        *,
        scopes: list[str] | None = None,
    ):
        self.db = journal.db
        self.issuer = issuer
        self.resource = issuer + MCP_PATH
        self.scopes = list(scopes or SCOPES)
        self.registrations: list[float] = []

    def _owns(self, issuer: str | None) -> bool:
        return issuer == self.issuer

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
            refresh = [
                json.loads(g["value"])
                for g in self.db.execute(
                    "SELECT value FROM oauth_grants WHERE client_id=? AND kind='refresh' "
                    "AND revoked=0 AND expires>?",
                    (row["client_id"], now),
                ).fetchall()
            ]
            result.append(
                {
                    "client_id": row["client_id"],
                    "client_name": info.client_name,
                    "redirect_uris": [str(u) for u in info.redirect_uris or []],
                    "approved": bool(row["approved"]),
                    "created": row["created"],
                    "last_used": row["last_used"],
                    "active_until": {g["kind"]: g["expires"] for g in grants},
                    "issuer": row["issuer"],
                    "scopes": sorted({s for value in refresh for s in value.get("scopes", [])}),
                    "renewed_while_used": any(value["sliding"] for value in refresh),
                    "never_expires": any(value["never"] for value in refresh),
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
        # challenge advertised (Claude). Always offer read and control together: approval on the box
        # page grants both. The token response reports the scope actually granted.
        requested = params.scopes or []
        if any(scope not in self.scopes for scope in requested):
            raise AuthorizeError("invalid_scope")
        scopes = list(self.scopes)
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
                    "issuer": value.get("issuer"),
                    # A script using the device grant; its user code is shown on both sides.
                    "kind": "device" if value.get("device") else "oauth",
                    "user_code": value.get("user_code"),
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
        if value.get("device"):
            return self._decide_device(handle, client, value, granted, window)
        params = AuthorizationParams.model_validate(value["params"])
        issuer = value.get("issuer")
        with self.db:
            self.db.execute("DELETE FROM oauth_grants WHERE digest=?", (handle,))
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
                "sliding": window == REFRESH_SECONDS,
                "never": window >= NEVER_SECONDS,
            },
            time.time() + CODE_SECONDS,
            issuer=issuer,
        )
        with self.db:
            self.db.execute("UPDATE oauth_clients SET approved=1 WHERE client_id=?", (client.client_id,))
        redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        self.record_decision(handle, client.client_id, redirect)
        return redirect

    def _decide_device(self, handle: str, client, value: dict, granted: list[str] | None, window: int) -> str:
        """A device request has no redirect: its script collects the decision by polling."""
        from openlolo.interfaces.device_grant import record_decision

        with self.db:
            self.db.execute("DELETE FROM oauth_grants WHERE digest=?", (handle,))
            if granted:
                self.db.execute("UPDATE oauth_clients SET approved=1 WHERE client_id=?", (client.client_id,))
        record_decision(self, handle, client.client_id, value, granted or [], window)
        return ""

    # -- owner approval from the box page: decisions the consent page polls for ---------------

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
        family_expires = now + value["window"]
        sliding, never = value["sliding"], value["never"]
        return self._issue(client.client_id, value["scopes"], family, family_expires, now, sliding, never)

    def _issue(
        self,
        client_id: str,
        scopes: list[str],
        family: str,
        family_expires: float,
        now: float,
        sliding: bool,
        never: bool,
    ):
        access, refresh = _token(), _token()
        access_expires = min(now + ACCESS_SECONDS, family_expires)
        value = {"scopes": scopes, "family_expires": family_expires, "sliding": sliding, "never": never}
        # Tokens are issued at the code exchange and every refresh (about every ACCESS_SECONDS
        # while a client works), so this is "last used" without a write per MCP request.
        with self.db:
            self.db.execute("UPDATE oauth_clients SET last_used=? WHERE client_id=?", (now, client_id))
        self._store("access", access, client_id, family, value, access_expires)
        # The refresh token lives as long as its family: 24 hours, 30 days from this use, or no end.
        self._store("refresh", refresh, client_id, family, value, family_expires)
        return OAuthToken(
            access_token=access,
            expires_in=max(1, int(access_expires - now)),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    @staticmethod
    def _in_grace(row, value: dict, client_id: str, now: float) -> bool:
        """A retired refresh token presented again within REFRESH_GRACE_SECONDS of its rotation,
        once, by its own client: the reply carrying its successor was most likely lost."""
        retired = value.get("retired")
        return (
            isinstance(retired, dict)
            and row["client_id"] == client_id
            and not retired.get("grace_used")
            and now - float(retired.get("at", 0)) <= REFRESH_GRACE_SECONDS
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = self._grant("refresh", refresh_token)
        if row is None:
            return None
        value = json.loads(row["value"])
        if row["revoked"] and not self._in_grace(row, value, client.client_id, time.time()):
            # A rotated refresh token presented again means it leaked: end the whole family.
            self.revoke_family(row["family"])
            return None
        if row["client_id"] != client.client_id:
            return None
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
        if row is None or row["client_id"] != client.client_id:
            raise TokenError("invalid_grant")
        now = time.time()
        value = json.loads(row["value"])
        retry = bool(row["revoked"])
        if retry and not self._in_grace(row, value, client.client_id, now):
            self.revoke_family(row["family"])
            raise TokenError("invalid_grant")
        if value["family_expires"] <= now:
            self.revoke_family(row["family"])
            raise TokenError("invalid_grant", "grant expired; the owner must approve again")
        if retry:
            # Lost reply: the successor pair was never received. Retire it and rotate again from
            # this token, once; the grace clock stays at the first rotation, so it cannot chain.
            retired = value["retired"]
            with self.db:
                self.db.execute(
                    "UPDATE oauth_grants SET revoked=1 WHERE digest IN (?, ?)",
                    (retired["refresh"], retired["access"]),
                )
        sliding, never = value["sliding"], value["never"]
        family_expires = now + REFRESH_SECONDS if sliding else value["family_expires"]
        issued = self._issue(
            client.client_id,
            scopes or value["scopes"],
            row["family"],
            family_expires,
            now,
            sliding,
            never,
        )
        assert issued.refresh_token is not None
        retired = {
            "at": value["retired"]["at"] if retry else now,
            "refresh": digest(issued.refresh_token),
            "access": digest(issued.access_token),
            "grace_used": retry,
        }
        with self.db:
            self.db.execute(
                "UPDATE oauth_grants SET revoked=1, value=? WHERE digest=?",
                (json.dumps({**value, "retired": retired}), digest(refresh_token.token)),
            )
        return issued

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


def owner_of(client_id: str) -> str:
    """The journal and lease owner for one caller: stable across bridge drops and restarts, and
    never equal to a pre-upgrade session digest (64 bare hex characters)."""
    return "client:" + digest(client_id)


class Gateway:
    """Resolves the caller's LocalPhone: one owner identity and lease per OAuth client, plus the
    box page's."""

    def __init__(self, runtime, provider: OAuthProvider):
        self.runtime = runtime
        self.provider = provider
        # client_id -> (bridge, oauth). The box page's identity is not an OAuth client, so the
        # revocation sweep must leave it alone.
        self.bridges: dict[str, tuple[LocalPhone, bool]] = {}
        self.calls: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()
        self.sweeper: asyncio.Task | None = None

    async def bridge(self, scope: str, *, exempt: bool = False) -> LocalPhone:
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
                return entry[0]
            owner = owner_of(token.client_id)
            client_info = await self.provider.get_client(token.client_id)
            label = (client_info.client_name if client_info and client_info.client_name else token.client_id)[
                :64
            ]
            self.runtime.journal.label_owner(owner, label)
            client = LocalPhone(self.runtime.phone, owner=owner, transport="streamable-http")
            # owner:admin is held only by the box page (ADR 0012); every other caller is an OAuth client.
            self.bridges[token.client_id] = (client, ADMIN_SCOPE not in token.scopes)
            return client

    async def _drop(self, client_id: str) -> None:
        entry = self.bridges.pop(client_id, None)
        if entry is None:
            return
        client, _ = entry
        await client.close()
        lease = self.runtime.coordinator.lease
        if lease and lease.owner == client.owner:
            await self.runtime.coordinator.drop_control()

    async def sweep(self) -> None:
        """Drop bridges whose client was revoked or whose grants all expired. The box page's
        bridge has no OAuth client behind it and is never swept."""
        async with self.lock:
            for client_id, (_, oauth) in list(self.bridges.items()):
                if oauth and not self.provider.client_active(client_id):
                    await self._drop(client_id)
            # Rate-limit windows of callers quiet for a minute carry nothing.
            cutoff = time.time() - 60
            for client_id, recent in list(self.calls.items()):
                if not any(t > cutoff for t in recent):
                    del self.calls[client_id]

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
                                f"default-src 'none'; style-src 'unsafe-inline'; img-src data:; {script}"
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
    READ_SCOPE: ("See the screen", "Screenshots and status"),
    CONTROL_SCOPE: ("Touch and type", "Taps, swipes, keys, buttons"),
}

# Box page colors and type (design system "OpenLolo"), inlined: the CSP allows no other source.
PAGE_STYLE = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:#f4f1ec;color:#2b2521;font:16px/24px -apple-system,BlinkMacSystemFont,"
    "'Segoe UI',system-ui,sans-serif;-webkit-font-smoothing:antialiased}"
    "main{max-width:640px;margin:0 auto;padding:56px 16px 48px;display:flex;flex-direction:column;"
    "align-items:center;gap:32px;text-align:center}"
    "a{color:#c8632b}a:hover{color:#a84f1f}"
    "a:focus-visible,button:focus-visible{outline:2px solid #c8632b;outline-offset:2px}"
    "h1{margin:0;font-weight:600;font-size:44px;line-height:48px;letter-spacing:-.015em}"
    "h2{margin:0;font-weight:600;font-size:20px;line-height:26px}"
    "p{margin:0}.muted{color:#6b625b}"
    ".pair{display:flex;align-items:center;gap:12px}"
    ".pair .who,.pair img{width:72px;height:72px;box-shadow:0 20px 45px -20px rgba(43,37,33,.25)}"
    ".who{border-radius:50%;background:#fff;color:#c8632b;display:flex;align-items:center;"
    "justify-content:center;font-weight:600;font-size:30px}"
    ".pair img{border-radius:20px}"
    ".flow{animation:flow .8s linear infinite}"
    "@keyframes flow{to{stroke-dashoffset:-12}}"
    ".head{display:flex;flex-direction:column;align-items:center;gap:12px}"
    ".lede{font-size:19px;line-height:28px;color:#6b625b;max-width:520px}"
    ".pill{display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:999px;"
    "font-size:13px;line-height:18px;font-weight:600;background:#fbede3;color:#c8632b}"
    ".pill i{width:8px;height:8px;border-radius:50%;background:#c8632b;animation:pulse 1.6s ease-out infinite}"
    ".approved .pill{background:#e6f4ea;color:#2e8b47}.approved .pill i{background:#34c759;animation:none}"
    ".gone .pill{background:#e9e4dd;color:#6b625b}.gone .pill i{background:#c7c0b8;animation:none}"
    ".approved .flow,.gone .flow{animation:none}.gone .flow{stroke:#c7c0b8}"
    "@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(200,99,43,.45)}70%{box-shadow:0 0 0 8px rgba(200,99,43,0)}"
    "100%{box-shadow:0 0 0 0 rgba(200,99,43,0)}}"
    ".approved .waiting,.gone .waiting{display:none}"
    ".scan{width:100%;display:flex;align-items:center;gap:24px;padding:20px;border-radius:24px;"
    "background:#fff;text-align:left;flex-wrap:wrap}"
    ".qr{flex:none;width:132px;height:132px;padding:10px;border-radius:12px;border:1px solid #e6e0d8}"
    ".qr svg{width:100%;height:100%;display:block}"
    ".scan div{flex:1 1 240px;display:flex;flex-direction:column;gap:6px}"
    ".scan a{font-size:14px;line-height:20px;min-height:32px;display:inline-flex;align-items:center}"
    ".asks{width:100%;display:flex;flex-direction:column;gap:12px}"
    ".label{font-size:13px;line-height:16px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;"
    "color:#6b625b}"
    ".tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0;"
    "padding:0;list-style:none;text-align:left}"
    ".tiles li{display:flex;gap:12px;align-items:flex-start;padding:16px;border-radius:16px;background:#fff}"
    ".tiles svg{flex:none;margin-top:2px}.tiles span{display:flex;flex-direction:column;gap:2px}"
    ".tiles small{font-size:13px;line-height:18px;color:#6b625b}"
    ".note{font-size:13px;line-height:18px;color:#6b625b}"
    ".error{width:100%;padding:10px 14px;border-radius:12px;background:#fff1e3;color:#b8540a;font-size:14px;"
    "line-height:20px;font-weight:500}"
    "footer{width:100%;padding-top:24px;border-top:1px solid #e6e0d8;display:flex;flex-direction:column;"
    "align-items:center;gap:12px;font-size:14px;line-height:20px;color:#6b625b}"
    "code{font:14px ui-monospace,'SF Mono',Menlo,monospace;word-break:break-all}"
    "form{margin:0}"
    ".cancel{height:44px;padding:0 16px;border:0;border-radius:12px;background:transparent;color:#2b2521;"
    "font:500 14px/1 inherit;text-decoration:underline;text-underline-offset:3px;cursor:pointer}"
    "@media (max-width:520px){main{padding-top:40px}h1{font-size:34px;line-height:40px}"
    ".scan{justify-content:center;text-align:center}.scan div{align-items:center}}"
    "@media (prefers-reduced-motion:reduce){.flow,.pill i{animation:none}}"
)
CHECK_ICON = (
    "<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='#c8632b' stroke-width='2' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M5 12.5 10 17l9-10'/></svg>"
)
LINK_LINE = (
    "<svg width='88' height='12' viewBox='0 0 88 12' fill='none' aria-hidden='true'><path class='flow' "
    "d='M4 6h80' stroke='#c8632b' stroke-width='2' stroke-linecap='round' stroke-dasharray='2 10'/></svg>"
)


@functools.cache
def _logo() -> str:
    data = (resources.files("openlolo.interfaces") / "assets" / "logo.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _page(
    title: str,
    body: str,
    status: int = 200,
    script: str | None = None,
    nonce: str | None = None,
    *,
    lede: str = "",
    pill: str | None = None,
    state: str = "gone",
    who: str = "",
):
    """One centered status column: the client and the box, a status pill, a headline, then ``body``."""
    scripts = f"<script nonce='{nonce}'>{script}</script>" if script and nonce else ""
    pair = (
        f"<div class='pair' aria-hidden='true'><span class='who'>{html.escape(who[:1].upper())}</span>"
        f"{LINK_LINE}<img src='{_logo()}' alt=''></div>"
        if who
        else f"<div class='pair' aria-hidden='true'><img src='{_logo()}' alt=''></div>"
    )
    response = HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><meta name='color-scheme' content='light'>"
        f"<title>{html.escape(title)} · OpenLolo</title><style>{PAGE_STYLE}</style></head>"
        f"<body><main id='page' class='{state}'>{pair}<div class='head'>"
        + (
            f"<span class='pill' id='pill' aria-live='polite'><i></i><b id='pilltext'>{html.escape(pill)}</b></span>"
            if pill
            else ""
        )
        + f"<h1 id='headline'>{html.escape(title)}</h1>"
        + (f"<p class='lede' id='lede'>{html.escape(lede)}</p>" if lede else "")
        + f"</div>{body}</main>{scripts}</body></html>",
        status_code=status,
    )
    if nonce:
        response.headers["x-openlolo-nonce"] = nonce
    return response


def expired_page() -> Response:
    return _page(
        "This request expired",
        "",
        400,
        lede="Go back to the client and connect again.",
        pill="Request expired",
    )


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
    """Owner approval page: the owner approves on the box page (QR or the Access list); this page waits."""
    raw_name = client.client_name or client.client_id
    name = html.escape(raw_name)
    hosts = ", ".join(sorted({html.escape(str(u.host or "")) for u in client.redirect_uris or []}))
    link = approve_link(setup_name, digest(request_id))
    tiles = "".join(
        f"<li>{CHECK_ICON}<span><strong>{html.escape(SCOPE_WORDS[scope][0])}</strong>"
        f"<small>{html.escape(SCOPE_WORDS[scope][1])}</small></span></li>"
        for scope in scopes
        if scope in SCOPE_WORDS
    )
    box = f", uses <strong>{html.escape(setup_name)}</strong>." if setup_name else "."
    body = (
        (f"<p class='error' role='alert'>{html.escape(error)}</p>" if error else "")
        + (
            "<section class='scan waiting'>"
            f"<span class='qr'>{qr_svg(link)}</span>"
            "<div><h2>Scan to open the request</h2>"
            "<p class='muted'>Use the iPhone camera, on the same Wi-Fi as the box. It is also under "
            "<strong>Access</strong> on the box page.</p>"
            f"<a href='{html.escape(link)}'>On the iPhone? Open the OpenLolo page</a></div></section>"
            if link
            else "<p class='muted waiting'>Open the box page on your iPhone. "
            "This request is under <strong>Access</strong>.</p>"
        )
        + (
            f"<section class='asks waiting'><span class='label'>{name} asks to</span>"
            f"<ul class='tiles'>{tiles}</ul>"
            "<p class='note'>On the box page you choose how long: 24 hours, 30 days renewed while "
            f"{name} uses it, or until you remove it.</p></section>"
            if tiles
            else ""
        )
        + f"<footer><p>{name}, returning to <code>{hosts}</code>{box} What it sees goes to its AI provider, "
        "including messages, notifications and codes. The AI can be misled by what is on screen.</p>"
        f"<form method='post' action='{CONSENT_PATH}' class='waiting'>"
        f"<input type='hidden' name='request' value='{html.escape(request_id)}'>"
        "<button class='cancel' name='decision' value='deny'>Cancel this request</button></form></footer>"
    )
    states = {
        "approved": [f"{raw_name} is connected", f"Taking you back to {hosts or 'the client'}.", "Approved"],
        "gone": ["This request expired", "Go back to the client and connect again.", "Request expired"],
    }
    script = (
        f"const r={json.dumps(request_id)},S={json.dumps(states)};"
        "function show(k){const s=S[k];document.getElementById('page').className=k;"
        "document.getElementById('headline').textContent=s[0];document.getElementById('lede').textContent=s[1];"
        "document.getElementById('pilltext').textContent=s[2];document.title=s[0]+' · OpenLolo';}"
        f"async function poll(){{try{{const x=await fetch({json.dumps(STATUS_PATH)}+'?request='+encodeURIComponent(r),"
        "{cache:'no-store',credentials:'omit'});const d=await x.json();"
        "if(d.state==='decided'&&d.redirect){if(new URL(d.redirect).searchParams.has('code')){show('approved');"
        "setTimeout(()=>{location.href=d.redirect},900);}else location.href=d.redirect;return;}"
        "if(d.state==='gone'){show('gone');return;}}"
        "catch(e){}setTimeout(poll,2000);}poll();"
    )
    return _page(
        f"Approve {raw_name} on your iPhone",
        body,
        400 if error else 200,
        script=script,
        nonce=nonce,
        lede="The box page has the request. Allow it there and this tab carries on by itself.",
        pill="Waiting for your iPhone",
        state="waiting",
        who=raw_name,
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


def window_seconds(window: str) -> int:
    if window not in CONTROL_WINDOWS:
        raise OpenLoloError("INVALID_WINDOW", f"window must be one of {', '.join(CONTROL_WINDOWS)}", 422)
    return CONTROL_WINDOWS[window]


def grant_from_choice(offered: list[str], window: str) -> tuple[list[str], int]:
    """Translate the owner's consent choice into granted scopes and the grant's lifetime.

    Approval always grants the screen and input together (ADR 0003, amendment 2026-09-30): a
    client that may only watch the phone has no use, and the bounded duration is the safety
    lever. A client can still narrow a grant itself with the ``scope`` parameter of a refresh."""
    granted = [scope for scope in SCOPES if scope in offered] or [READ_SCOPE]
    return granted, window_seconds(window)


def consent_routes(provider: OAuthProvider, origin: str, runtime) -> Callable[[Request], Any]:
    setup_name = runtime.config.setup_name

    async def consent(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = provider.pending_request(request_id) if request_id else None
            if pending is None:
                return expired_page()
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
            return _page("Request rejected", "", 403, lede="Cross-site form submission rejected.")
        form = await read_form(request)
        request_id = form.get("request", "")
        pending = provider.pending_request(request_id) if request_id else None
        if pending is None:
            return expired_page()
        client, value = pending
        if form.get("decision") == "deny":
            return RedirectResponse(provider.decide(request_id, None, 0), status_code=302)
        return consent_form(
            request_id,
            client,
            value["scopes"],
            "Approve this request on the box page.",
            setup_name=setup_name,
            nonce=secrets.token_urlsafe(12),
        )

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


def create_gateway_app(runtime, origin: str | None = None, *, allowed_hosts: list[str] | None = None):
    """Starlette app for one OAuth/MCP entry.

    ``origin`` is the issuer this instance serves; it defaults to ``config.public_url`` (the remote
    entry behind the tunnel). The LAN entry (ADR 0005) passes ``config.lan_url`` and the extra Host
    values it accepts. Both offer only the phone scopes; owner routes live on the box page.
    """
    config = runtime.config
    origin = origin or config.public_url
    if not origin:
        raise ValueError("public_url is required for the remote MCP entry")
    hosts = {urlsplit(origin).netloc.lower(), *(h.lower() for h in allowed_hosts or [])}
    scopes = SCOPES
    provider = OAuthProvider(runtime.journal, origin, scopes=scopes)
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
    from openlolo.interfaces.device_grant import DeviceGrant, DeviceGrantRoutes

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts), allowed_origins=[origin]
        ),
    )
    app = DeviceGrantRoutes(app, DeviceGrant(provider, config.setup_name))
    return EntrySecurity(app, hosts, gateway, provider)
