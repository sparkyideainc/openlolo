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
from openlolo.interfaces.http_api import body

log = logging.getLogger("openlolo.portal")
WEB = Path(__file__).resolve().parents[1] / "web" / "portal"
AP_NET = ipaddress.ip_network("192.168.4.0/24")
SESSION_COOKIE = "openlolo_portal"
SESSION_SECONDS = 86_400
CLIENT_HEADER = "x-openlolo-client"
PORTAL_CLIENT = "portal"
PORTAL_IDENTITY = "Box page"  # Journal owner label for operations started from the page.
CABLE_FREE_WRITES = {"/app/pause"}  # Safety first: pausing never waits for the cable.
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
    """In-memory sessions for the box page; a restart signs everyone out."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.sessions: dict[str, float] = {}

    def issue(self) -> str:
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        token = secrets.token_urlsafe(32)
        self.sessions[_digest(token)] = now + SESSION_SECONDS
        return token

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        expiry = self.sessions.get(_digest(token))
        return expiry is not None and expiry > self.clock()

    def revoke(self, token: str | None) -> None:
        if token:
            self.sessions.pop(_digest(token), None)


def create_setup_portal(runtime, *, web: Path = WEB, clock=time.time) -> ASGIApp:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from openlolo.interfaces.app_api import app_routes
    from openlolo.interfaces.mcp_server import ALL_SCOPES

    sessions = PortalSessions(clock)
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
        return sessions.check(session_token(request))

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
                SESSION_COOKIE, token, max_age=SESSION_SECONDS, path="/", httponly=True, samesite="strict"
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
        the page's identity. Writes other than Pause need the cable right now (ADR 0012)."""

        async def route(request: Request):
            try:
                require_local(request)
                if has_session(request):
                    if request.method != "GET":
                        require_client_header(request)
                        if (
                            request.url.path not in CABLE_FREE_WRITES
                            and not await runtime.phone.cable_present()
                        ):
                            raise OpenLoloError(
                                "CABLE_REQUIRED", "Plug the iPhone into the box to change settings", 403
                            )
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
