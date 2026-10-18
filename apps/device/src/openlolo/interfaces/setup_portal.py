"""Box web app on HTTP port 80 (ADR 0011, ADR 0012).

Serves the built owner page (``web/portal``) and two APIs: the unauthenticated bootstrap
``/setup/*`` routes the captive flow relies on, and the ``/app/*`` owner table from
``app_api`` behind a session. The cable is the key: a session is issued only while one phone
is on the box's USB port, and only to a browser on the setup access point (joined with the
card's WPA2 password) or on the paired phone itself (its LAN address from the RemotePairing
advertisement). There is no password. Nothing here is reachable from outside the local network.
"""

import hashlib
import ipaddress
import logging
import re
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.json_body import body

log = logging.getLogger("openlolo.portal")
WEB = Path(__file__).resolve().parents[1] / "web" / "portal"
AP_NET = ipaddress.ip_network("192.168.4.0/24")
SESSION_COOKIE = "openlolo_portal"
# Sessions never expire on the box. Browsers cap a cookie's Max-Age at 400 days, so each use
# re-sends the cookie with a fresh one.
COOKIE_SECONDS = 400 * 86_400
SESSION_RENEW_SECONDS = 3600  # Record a session's use (and re-send its cookie) at most once an hour.
SESSION_LIMIT = 64  # Stored box-page sessions; issuing past it drops the least recently used.
RENEWED_SCOPE_KEY = "openlolo.renewed_session"
CLIENT_HEADER = "x-openlolo-client"
PORTAL_CLIENT = "portal"
PORTAL_IDENTITY = "Box page"  # Journal owner label for operations started from the page.
CAPTIVE_PROBES = ("/hotspot-detect.html", "/library/test/success.html", "/generate_204")
# Captive probes get the answer the OS expects from the open internet, so no captive sheet ever
# opens: Apple's sheet cannot hand the owner to Safari, and the setup card carries a second QR
# with the page's address instead (ADR 0011, amendment of 6 October 2026).
APPLE_SUCCESS = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
MISSING_BUILD = (
    "<!doctype html><meta charset=utf-8><title>OpenLolo</title>"
    "<p>The box page is not built. Run <code>bun run build -F portal</code> and redeploy.</p>"
)
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PortalSessions:
    """Box-page sessions in the journal database, so a restart or reboot keeps the owner signed in.

    A session never expires: it ends on sign-out, past SESSION_LIMIT, when a factory reset
    deletes the database, or when pairing a different iPhone ends every session
    (``Journal.record_owner``). Only the SHA-256 digest of a token is stored."""

    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    def issue(self) -> str:
        now = self.clock()
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO portal_sessions VALUES (?,?)", (_digest(token), now))
            self.db.execute(
                "DELETE FROM portal_sessions WHERE digest NOT IN "
                "(SELECT digest FROM portal_sessions ORDER BY last_used DESC LIMIT ?)",
                (SESSION_LIMIT,),
            )
        return token

    def check(self, token: str | None) -> bool:
        return self.renew(token) is not None

    def renew(self, token: str | None) -> bool | None:
        """None for no valid session; else whether this use was recorded.

        A read on most requests: the use is written only once the last one is
        SESSION_RENEW_SECONDS old."""
        if not token:
            return None
        now = self.clock()
        key = _digest(token)
        row = self.db.execute("SELECT last_used FROM portal_sessions WHERE digest=?", (key,)).fetchone()
        if row is None:
            return None
        if row[0] > now - SESSION_RENEW_SECONDS:
            return False
        with self.db:
            self.db.execute("UPDATE portal_sessions SET last_used=? WHERE digest=?", (now, key))
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            with self.db:
                self.db.execute("DELETE FROM portal_sessions WHERE digest=?", (_digest(token),))


def create_setup_portal(runtime, *, web: Path = WEB, clock=time.time) -> ASGIApp:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from openlolo.interfaces.app_api import app_routes
    from openlolo.interfaces.mcp_server import ALL_SCOPES

    sessions = PortalSessions(runtime.journal.db, clock)
    index = web / "index.html"

    # -- page -------------------------------------------------------------------------

    async def page(_request):
        if index.exists():
            return FileResponse(index)
        return HTMLResponse(MISSING_BUILD, status_code=503)

    # -- request facts --------------------------------------------------------------

    def peer(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        client = request.client
        if client is None:
            return None
        try:
            return ipaddress.ip_address(client.host)
        except ValueError:
            return None

    def on_setup_ap(request: Request) -> bool:
        """The client is on the box's own setup access point.

        While the setup window is open the single radio runs the AP, not the home station, so
        every client in the AP subnet associated through the card's WPA2 password. (The ASGI
        ``server`` entry is uvicorn's bind address, never the destination, so it cannot be used
        to tell the AP apart from a home LAN that also uses 192.168.4.0/24.)"""
        address = peer(request)
        return bool(address in AP_NET and runtime.provisioning.active)

    async def on_paired_phone(request: Request) -> bool:
        """The request comes from the bound phone's own LAN address (RemotePairing, ADR 0012)."""
        address = peer(request)
        if address is None:
            return False
        known: list[str] = []
        for refresh in (False, True):
            known = await runtime.phone.paired_addresses(refresh=refresh)
            if any(_same_address(address, known_address) for known_address in known):
                return True
        # Owner-facing diagnostics: which address asked, and where the box believes its phone is.
        log.warning(
            "box page: %s is not the paired phone (advertised: %s)", address, ", ".join(known) or "none"
        )
        return False

    async def access(request: Request) -> dict:
        """Who may open the page from here, and why not."""
        address = peer(request)
        if runtime.config.backend == "simulated" and address is not None and address.is_loopback:
            # The simulator has no cable or paired phone; the developer's own machine stands in.
            return {"cable": True, "setup_ap": False, "paired_phone": True, "allowed": True}
        cable = await runtime.phone.cable_present()
        setup_ap = on_setup_ap(request)
        paired_phone = False if setup_ap else await on_paired_phone(request)
        return {
            "cable": cable,
            "setup_ap": setup_ap,
            "paired_phone": paired_phone,
            "allowed": cable and (setup_ap or paired_phone),
        }

    def session_token(request: Request) -> str | None:
        return request.cookies.get(SESSION_COOKIE)

    def has_session(request: Request) -> bool:
        token = session_token(request)
        renewed = sessions.renew(token)
        if renewed:
            request.scope[RENEWED_SCOPE_KEY] = token  # Headers re-sends the cookie with a fresh Max-Age.
        return renewed is not None

    def require_local(request: Request):
        address = peer(request)
        if address is None or not (address.is_private or address.is_loopback):
            raise OpenLoloError("LOCAL_NETWORK_REQUIRED", status=403)

    def require_client_header(request: Request):
        # A custom header forces a CORS preflight, which a foreign origin never passes; with the
        # SameSite=Strict cookie this is the page's CSRF guard for every write.
        if request.headers.get(CLIENT_HEADER, "").lower() != PORTAL_CLIENT:
            raise OpenLoloError(
                "CLIENT_HEADER_REQUIRED", f"{CLIENT_HEADER}: {PORTAL_CLIENT} is required", 403
            )

    def refuse(facts: dict) -> OpenLoloError:
        if not facts["cable"]:
            return OpenLoloError("CABLE_REQUIRED", "Plug the iPhone into the box with its USB cable", 403)
        return OpenLoloError("PHONE_REQUIRED", "Open this page on the iPhone plugged into the box", 403)

    async def require_access(request: Request):
        require_local(request)
        if has_session(request):
            return
        facts = await access(request)
        if not facts["allowed"]:
            raise refuse(facts)

    def set_cookie(response: Response, token: str | None) -> Response:
        if token is None:
            response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        else:
            response.set_cookie(
                SESSION_COOKIE, token, max_age=COOKIE_SECONDS, path="/", httponly=True, samesite="strict"
            )
        return response

    # -- bootstrap routes -------------------------------------------------------------

    async def status(request):
        require_local(request)
        pairing = runtime.autopair.status()
        if pairing.get("last_error"):
            pairing["last_error"] = {"code": pairing["last_error"].get("code")}
        return JSONResponse(
            {
                "device": runtime.config.setup_name,
                "hostname": f"{(runtime.config.setup_name or 'openlolo').lower()}.local",
                "setup_mode": runtime.provisioning.active,
                "bound": runtime.bindings.read() is not None,
                "session": has_session(request),
                "access": await access(request),
                "state": runtime.provisioning.state.value,
                "last_attempt": runtime.provisioning.last_attempt,
                "last_error": {"code": runtime.provisioning.last_error.get("code")}
                if runtime.provisioning.last_error
                else None,
                "usb_pairing": pairing,
            }
        )

    async def open_session(request):
        require_local(request)
        require_client_header(request)
        facts = await access(request)
        if not facts["allowed"]:
            raise refuse(facts)
        return set_cookie(JSONResponse({"session": True}), sessions.issue())

    async def close_session(request):
        require_local(request)
        require_client_header(request)
        sessions.revoke(session_token(request))
        return set_cookie(JSONResponse({"session": False}), None)

    async def scan(request):
        await require_access(request)
        return JSONResponse({"networks": await runtime.network.scan()})

    async def connect(request):
        await require_access(request)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"error": "JSON required"}, status_code=415)
        try:
            data = await body(request, 4096)
        except OpenLoloError:
            return JSONResponse({"error": "Invalid request"}, status_code=400)
        ssid, password = data.get("ssid"), data.get("password", "")
        if (
            not isinstance(ssid, str)
            or not ssid
            or len(ssid.encode()) > 32
            or re.search(r"[\x00-\x1f]", ssid)
        ):
            return JSONResponse({"error": "Choose a Wi-Fi network"}, status_code=400)
        if not isinstance(password, str) or len(password) > 63 or re.search(r"[\x00-\x1f]", password):
            return JSONResponse({"error": "Password is not valid"}, status_code=400)
        try:
            attempt = await runtime.provisioning.connect(ssid, password)
        except OpenLoloError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
        return JSONResponse({"attempt": attempt, "state": "joining"}, status_code=202)

    async def captive_probe(request):
        log.info(
            "captive probe %s %s from %s", request.headers.get("host", "-"), request.url.path, peer(request)
        )
        headers = {"cache-control": "no-store"}
        if request.url.path == "/generate_204":
            return Response(status_code=204, headers=headers)
        return HTMLResponse(APPLE_SUCCESS, headers=headers)

    async def route_fallback(request):
        # Apple captive checks and common Android portal checks must receive a short local page;
        # top-level build files (favicon, manifest) are served as is; every other unknown path
        # is a page route handled by the SPA router.
        name = request.path_params.get("path", "")
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]+", name) and (web / name).is_file():
            media_type = "application/manifest+json" if name.endswith(".webmanifest") else None
            return FileResponse(web / name, media_type=media_type)
        return await page(request)

    def api(handler):
        async def route(request):
            try:
                return await handler(request)
            except OpenLoloError as exc:
                return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)

        return route

    # -- owner routes (/app/*) --------------------------------------------------------

    def owner(handler):
        """Admit the page's session (or a browser that could get one) and run the app route as
        the page's identity. The cable is checked only when a session is issued (ADR 0012): a
        signed-in page changes settings without it, and pairing needs the phone on the cable
        anyway."""

        async def route(request: Request):
            try:
                require_local(request)
                if has_session(request):
                    if request.method != "GET":
                        require_client_header(request)
                else:
                    facts = await access(request)
                    if not facts["allowed"]:
                        raise OpenLoloError("LOGIN_REQUIRED", refuse(facts).message, 401)
                    if request.method != "GET":
                        require_client_header(request)
            except OpenLoloError as exc:
                return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
            user = AuthenticatedUser(
                AccessToken(token="portal", client_id=PORTAL_IDENTITY, scopes=list(ALL_SCOPES))
            )
            context = auth_context_var.set(user)
            try:
                return await handler(request)
            finally:
                auth_context_var.reset(context)

        return route

    routes: list[Route | Mount] = [
        Route("/", page),
        Route("/setup/status", api(status)),
        Route("/setup/session", api(open_session), methods=["POST"]),
        Route("/setup/session", api(close_session), methods=["DELETE"]),
        Route("/setup/scan", api(scan)),
        Route("/setup/connect", api(connect), methods=["POST"]),
    ]
    routes += [
        Route(path, owner(handler), methods=methods)
        for path, methods, handler in app_routes(
            runtime, runtime.door_gateway, runtime.door_provider, entry="portal"
        )
    ]
    routes += [
        Mount("/assets", StaticFiles(directory=web / "assets", check_dir=False), name="assets"),
        *(Route(probe, captive_probe) for probe in CAPTIVE_PROBES),
        Route("/{path:path}", route_fallback),
    ]

    class Headers:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            asset = scope["type"] == "http" and scope.get("path", "").startswith("/assets/")

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    renewed = scope.get(RENEWED_SCOPE_KEY)
                    if renewed and not any(k == b"set-cookie" for k, _ in headers):
                        cookie = set_cookie(Response(), renewed).raw_headers
                        headers.extend(h for h in cookie if h[0] == b"set-cookie")
                    headers.extend(
                        [
                            (
                                b"cache-control",
                                b"public, max-age=31536000, immutable" if asset else b"no-store",
                            ),
                            (b"x-content-type-options", b"nosniff"),
                            (b"referrer-policy", b"no-referrer"),
                            (b"content-security-policy", CSP.encode()),
                        ]
                    )
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, secure_send)

    return Headers(Starlette(routes=routes))


def _same_address(client: ipaddress.IPv4Address | ipaddress.IPv6Address, known: str) -> bool:
    try:
        other = ipaddress.ip_address(known.split("%", 1)[0])  # Drop a link-local zone id.
    except ValueError:
        return False
    if client == other:
        return True
    # Safari may reach the box over IPv4 while the advertisement carries a mapped form.
    mapped = getattr(client, "ipv4_mapped", None)
    return mapped is not None and mapped == other
