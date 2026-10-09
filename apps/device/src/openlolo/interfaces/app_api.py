"""Owner JSON routes (``/app/*``, ADR 0005 contract).

The routes are served two ways from one table: on the box page (``setup_portal`` runs them
behind its own session as the page's identity, ADR 0012) and on the LAN OAuth/MCP gateway
(bearer-gated by the MCP SDK's middleware) while ``lan_url`` is configured. Phone operations
go through the same per-client bridge the MCP tools use, so leases, journaling, pause and
revocation apply identically whichever way a request arrived.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from openlolo.adapters.system.network import run as run_command
from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.http_api import body
from openlolo.interfaces.mcp_client import BridgeError
from openlolo.interfaces.mcp_server import ADMIN_SCOPE, ALL_SCOPES, CONTROL_SCOPE, READ_SCOPE

APP_PREFIX = "/app"
SETUP_KINDS = {"bind", "transport", "recover", "pair", "developer_mode", "mount_ddi", "unbind"}
NETWORK_COMMANDS = {"scan", "connect", "forget", "setup_mode"}
RESET_UNIT = "openlolo-reset.service"  # Root unit: deploy/reset/openlolo-reset.sh

Handler = Callable[[Request], Awaitable[Response]]
RouteSpec = tuple[str, list[str], Handler]


def require(scope: str | None) -> AccessToken:
    token = get_access_token()
    if token is None:
        raise OpenLoloError("LOGIN_REQUIRED", status=401)
    if scope is not None and scope not in token.scopes:
        raise OpenLoloError("SCOPE_REQUIRED", f"{scope} not granted", 403)
    return token


def guarded(handler: Callable[[Request], Awaitable[Any]]) -> Handler:
    """Wrap a coroutine returning JSON data or a Response; map errors to the shared envelope."""

    async def route(request: Request) -> Response:
        try:
            result = await handler(request)
        except BridgeError as exc:
            # Bridged Phone API errors keep their HTTP status; gateway-level ones map by code.
            status = exc.status or {"LOGIN_REQUIRED": 401, "SCOPE_REQUIRED": 403, "RATE_LIMITED": 429}.get(
                exc.code, 409
            )
            return JSONResponse({"error": exc.public()}, status_code=status)
        except OpenLoloError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
        except ValueError:
            return JSONResponse(
                {"error": {"code": "INVALID_REQUEST", "message": "Request fields are invalid"}},
                status_code=400,
            )
        if isinstance(result, Response):
            return result
        return JSONResponse(result)

    return route


def app_routes(runtime, gateway, provider, *, entry: str) -> list[RouteSpec]:
    """Every ``/app/*`` route as (path, methods, guarded handler); ``entry`` names the door."""
    from openlolo import __version__
    from openlolo.interfaces import tls
    from openlolo.interfaces.mcp_http import grant_from_choice

    config = runtime.config
    network = getattr(runtime, "network", None)
    provisioning = getattr(runtime, "provisioning", None)
    routes: list[RouteSpec] = []

    def add(path: str, methods: list[str]):
        def decorator(fn):
            routes.append((APP_PREFIX + path, methods, guarded(fn)))
            return fn

        return decorator

    def need_network():
        if network is None or provisioning is None:
            raise OpenLoloError("NETWORK_UNAVAILABLE", "Wi-Fi management is not enabled on this box", 503)
        return network, provisioning

    async def operation_body(request: Request) -> dict:
        data = await body(request)
        operation_id = data.get("operation_id") or str(uuid4())
        if not isinstance(operation_id, str) or len(operation_id) > 64:
            raise OpenLoloError("INVALID_OPERATION_ID", status=400)
        data["operation_id"] = operation_id
        return data

    @add("/info", ["GET"])
    async def info(request: Request):
        fingerprint = None
        if config.lan_cert is not None and config.lan_cert.exists():
            fingerprint = tls.fingerprint(config.lan_cert)
        return {
            "version": __version__,
            "board": config.board,
            "entry": entry,
            "issuer": provider.issuer,
            "fingerprint": fingerprint,
            "scopes": ALL_SCOPES,
            "setup_mode": bool(provisioning.active) if provisioning is not None else False,
            "features": {
                "network": network is not None and provisioning is not None,
                "diagnostics": True,
            },
        }

    @add("/status", ["GET"])
    async def status(request: Request):
        client = await gateway.bridge(READ_SCOPE)
        result = await client.status()
        if provisioning is not None:
            result["setup_mode"] = bool(provisioning.active)
        result["pending_consents"] = len(provider.list_pending())
        return result

    @add("/capabilities", ["GET"])
    async def capabilities(request: Request):
        client = await gateway.bridge(READ_SCOPE)
        return await client.request("GET", "capabilities")

    @add("/pause", ["POST"])
    async def pause(request: Request):
        client = await gateway.bridge(READ_SCOPE, exempt=True)
        data = await operation_body(request)
        return await client.control("pause", data["operation_id"])

    @add("/operations", ["GET"])
    async def operations(request: Request):
        """Journal page of summaries with a ``(created, id)`` cursor; ``before`` is the older
        timestamp-only form, accepted for one release."""
        require(ADMIN_SCOPE)
        limit = int(request.query_params.get("limit", "20"))
        cursor = request.query_params.get("cursor")
        before = request.query_params.get("before")
        if cursor is None and before:
            cursor = f"{float(before)!r}:"
        return runtime.journal.page_operations(limit, cursor)

    @add("/operations/{id}", ["GET"])
    async def operation(request: Request):
        client = await gateway.bridge(READ_SCOPE)
        return await client.operation(request.path_params["id"])

    @add("/clients", ["GET"])
    async def clients(request: Request):
        """OAuth clients of every issuer, each flagged when it is the caller."""
        token = require(ADMIN_SCOPE)
        result = provider.list_clients(every_issuer=True)
        for row in result:
            row["kind"] = "oauth"
            row["self"] = row["client_id"] == token.client_id
        return {"clients": result}

    @add("/clients/{id}/revoke", ["POST"])
    async def revoke(request: Request):
        token = require(ADMIN_SCOPE)
        client_id = request.path_params["id"]
        if client_id == token.client_id:
            raise OpenLoloError("CANNOT_REVOKE_SELF", "A client cannot revoke its own access", 409)
        if not provider.revoke_client(client_id):
            raise OpenLoloError("UNKNOWN_CLIENT", status=404)
        await gateway.sweep()
        return {"revoked": client_id}

    @add("/consents", ["GET"])
    async def consents(request: Request):
        """Pending MCP-client consent requests on this box (any entry), for the owner to decide."""
        require(ADMIN_SCOPE)
        return {"consents": provider.list_pending()}

    @add("/consents/{handle}", ["POST"])
    async def decide_consent(request: Request):
        require(ADMIN_SCOPE)
        handle = request.path_params["handle"]
        data = await body(request)
        pending = provider.pending_by_handle(handle)
        if pending is None:
            raise OpenLoloError("CONSENT_EXPIRED", status=404)
        _, value = pending
        if data.get("decision") != "allow":
            provider.decide_handle(handle, None, 0)
            return {"decided": "deny", "scopes": []}
        granted, seconds = grant_from_choice(
            value["scopes"], data.get("admin") is True, str(data.get("window", ""))
        )
        provider.decide_handle(handle, granted, seconds)
        return {"decided": "allow", "scopes": granted, "window": seconds}

    @add("/consents/{handle}/pin", ["POST"])
    async def consent_pin(request: Request):
        """Six-digit, 30-second PIN that completes exactly this request with the choice made here."""
        require(ADMIN_SCOPE)
        data = await body(request)
        choice = {
            "admin": data.get("admin") is True,
            "window": str(data.get("window", "1h")),
        }
        return provider.issue_pin(request.path_params["handle"], choice)

    @add("/discover", ["GET"])
    async def discover(request: Request):
        client = await gateway.bridge(ADMIN_SCOPE)
        return await client.request("GET", "discover", timeout=40)

    @add("/setup/{kind}", ["POST"])
    async def setup(request: Request):
        require(ADMIN_SCOPE)
        kind = request.path_params["kind"]
        if kind not in SETUP_KINDS:
            raise OpenLoloError("UNKNOWN_SETUP_ACTION", status=404)
        client = await gateway.bridge(CONTROL_SCOPE)
        data = await operation_body(request)
        if client.lease is None or client.lease_error:
            await client.control("acquire", str(uuid4()))
        result = await client.request(
            "POST",
            f"setup/{kind}",
            {
                "operation_id": data["operation_id"],
                "lease_token": client.lease,
                "payload": data.get("payload", {}),
            },
            timeout=30,
        )
        return JSONResponse(result, status_code=202 if result.get("state") == "QUEUED" else 200)

    @add("/reset", ["POST"])
    async def reset(request: Request):
        """Back to first-time setup. The owner types the box's name; the server checks it too."""
        require(ADMIN_SCOPE)
        data = await body(request)
        typed = data.get("device")
        expected = config.setup_name or ""
        if not isinstance(typed, str) or not expected or typed.strip().casefold() != expected.casefold():
            raise OpenLoloError(
                "RESET_NAME_MISMATCH", "Type the box's name exactly as printed on its card", 400
            )
        if config.backend == "simulated":
            runtime.bindings.delete()
            if provisioning is not None and not provisioning.active:
                await provisioning.enter("reset")
            return JSONResponse({"state": "reset"})
        # The unit stops this service, so it must run on its own: --no-block returns at once.
        code, output = await run_command(
            "systemctl", "start", "--no-block", RESET_UNIT, timeout=10, quiet=False
        )
        if code != 0:
            raise OpenLoloError("RESET_FAILED", output or "Could not start the reset", 502)
        return JSONResponse({"state": "resetting"}, status_code=202)

    @add("/network", ["GET"])
    async def network_status(request: Request):
        require(ADMIN_SCOPE)
        net, prov = need_network()
        return {**await net.status(), "provisioning": prov.health()}

    @add("/network/{command}", ["POST"])
    async def network_command(request: Request):
        require(ADMIN_SCOPE)
        net, prov = need_network()
        command = request.path_params["command"]
        if command not in NETWORK_COMMANDS:
            raise OpenLoloError("UNKNOWN_OPERATION", status=404)
        data = await body(request)
        if command == "scan":
            return {"networks": await net.scan()}
        if command == "forget":
            ssid = data.get("ssid")
            if not isinstance(ssid, str) or not ssid:
                raise OpenLoloError("INVALID_REQUEST", "ssid is required", 400)
            await net.forget(ssid, force=data.get("force") is True)
            return {"forgotten": ssid}
        if command == "setup_mode":
            if data.get("enabled") is True:
                await prov.enter(reason="api")
            else:
                await prov.exit()
            return prov.health()
        ssid, psk = data.get("ssid"), data.get("psk")
        if not isinstance(ssid, str) or not ssid or not isinstance(psk, str):
            raise OpenLoloError("INVALID_REQUEST", "ssid and psk are required", 400)
        attempt = await prov.connect(ssid, psk)
        return JSONResponse({"attempt_id": attempt, **prov.health()}, status_code=202)

    @add("/diagnostics", ["GET"])
    async def diagnostics(request: Request):
        require(ADMIN_SCOPE)
        from openlolo.application.diagnostics import export

        return await export(runtime)

    return routes


def install_app_routes(server, runtime, gateway, provider) -> None:
    """Mount the app routes on the LAN gateway (ADR 0005)."""
    for path, methods, handler in app_routes(runtime, gateway, provider, entry="lan"):
        server.custom_route(path, methods=methods)(handler)


def redact(text: str) -> str:
    """Drop token-shaped runs and phone identifiers from log excerpts."""
    import re

    text = re.sub(r"[A-Za-z0-9_-]{32,}", "<redacted>", text)
    text = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b", "<udid>", text)
    return text
