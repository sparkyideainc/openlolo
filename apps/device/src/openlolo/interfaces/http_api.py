import base64
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import (
    Action,
    ActionRequest,
    CaptureOptions,
    ControlRequest,
    FullPageRequest,
    LinkRequest,
    SetupRequest,
)

WEB = Path(__file__).resolve().parents[1] / "web"


async def body(request, limit=16384):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise OpenLoloError("REQUEST_TOO_LARGE", status=413)
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise OpenLoloError("INVALID_JSON", status=400) from None


class Security:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; img-src 'self' blob: data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                        ),
                    ]
                )
            await send(message)

        try:
            if request.headers.get("host") not in self.runtime.config.allowed_hosts:
                raise OpenLoloError("HOST_REJECTED", status=403)
            origin = request.headers.get("origin")
            if origin and origin not in self.runtime.config.allowed_origins:
                raise OpenLoloError("ORIGIN_REJECTED", status=403)
            if request.method not in {"GET", "HEAD"}:
                if not request.headers.get("content-type", "").startswith("application/json"):
                    raise OpenLoloError("JSON_REQUIRED", status=415)
                if origin is None and request.headers.get("x-openlolo-client") != "cli":
                    raise OpenLoloError("ORIGIN_REQUIRED", status=403)
            await self.app(scope, receive, secure_send)
        except OpenLoloError as exc:
            await JSONResponse({"error": exc.as_dict()}, status_code=exc.status)(scope, receive, secure_send)


def create_app(runtime, manage_lifespan=True):
    phone, auth = runtime.phone, runtime.auth

    @asynccontextmanager
    async def lifespan(_):
        if manage_lifespan:
            await runtime.start()
        try:
            yield
        finally:
            if manage_lifespan:
                await runtime.close()

    def owner(request):
        return auth.owner(request.cookies.get("openlolo_session"))

    async def error_handler(request, exc):
        if isinstance(exc, OpenLoloError):
            return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
        # Pydantic details include raw inputs. Do not echo text or credentials.
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "Request fields are invalid"}}, status_code=400
        )

    async def static(request):
        filename = request.path_params.get("name", "index.html")
        if filename not in {"index.html", "app.js", "styles.css"}:
            return Response(status_code=404)
        return FileResponse(WEB / filename)

    async def login(request):
        data = await body(request)
        token = data.get("credential")
        if not isinstance(token, str) or len(token) > 256:
            raise OpenLoloError("INVALID_CREDENTIAL", status=401)
        session = auth.login(token)
        label = "cli" if request.headers.get("x-openlolo-client") == "cli" else "browser"
        runtime.journal.label_owner(auth.owner(session), label)
        response = JSONResponse({"authenticated": True})
        response.set_cookie("openlolo_session", session, httponly=True, samesite="strict", max_age=8 * 3600)
        return response

    async def logout(request):
        who = owner(request)
        if runtime.coordinator.lease and runtime.coordinator.lease.owner == who:
            await runtime.coordinator.drop_control()
        auth.logout(request.cookies["openlolo_session"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("openlolo_session")
        return response

    async def status(request):
        return JSONResponse(await phone.status(owner(request)))

    async def capabilities(request):
        owner(request)
        return JSONResponse(phone.capabilities())

    async def screenshot(request):
        owner(request)
        frame = await phone.screenshot(CaptureOptions.model_validate(await body(request)))
        return JSONResponse(
            {"metadata": frame.metadata.model_dump(), "image": base64.b64encode(frame.image).decode()}
        )

    async def full_page(request):
        who = owner(request)
        data = FullPageRequest.model_validate(await body(request))
        result = phone.full_page(data.operation_id, who, data.lease_token, data.options)
        return JSONResponse(result, status_code=202 if result["state"] == "QUEUED" else 200)

    async def full_page_result(request):
        return JSONResponse(phone.full_page_result(request.path_params["id"], owner(request)))

    async def app_profiles(request):
        owner(request)
        return JSONResponse({"profiles": phone.app_profiles()})

    async def app_profile(request):
        owner(request)
        return JSONResponse(phone.app_profile(request.path_params["bundle_id"]))

    async def open_link(request):
        who = owner(request)
        data = LinkRequest.model_validate(await body(request)).model_dump()
        result = phone.open_link(
            data["operation_id"], who, data["lease_token"], data["bundle_id"], data["link"], data["params"]
        )
        return JSONResponse(result, status_code=202 if result["state"] == "QUEUED" else 200)

    async def clipboard(request):
        owner(request)
        return JSONResponse(await phone.clipboard())

    async def apps(request):
        owner(request)
        return JSONResponse({"apps": await phone.apps()})

    async def device(request):
        owner(request)
        return JSONResponse(await phone.device_info())

    async def discover(request):
        owner(request)
        return JSONResponse(await phone.discover())

    async def action(request):
        who = owner(request)
        data = ActionRequest.model_validate(await body(request)).model_dump()
        result = phone.action(
            data["operation_id"], who, data["lease_token"], Action.model_validate(data["action"])
        )
        return JSONResponse(result, status_code=202 if result["state"] == "QUEUED" else 200)

    async def operation(request):
        return JSONResponse(phone.operation_status(request.path_params["id"], owner(request)))

    async def calibration_status(request):
        owner(request)
        try:
            session = runtime.calibration.current()
            return JSONResponse(
                {**runtime.calibration.public(session["token"]), "geometry": session["geometry"]}
            )
        except OpenLoloError:
            return JSONResponse({"active": False})

    async def setup(request):
        who = owner(request)
        data = SetupRequest.model_validate(await body(request)).model_dump()
        result = phone.setup(
            data["operation_id"],
            who,
            data["lease_token"],
            request.path_params["kind"],
            data.get("payload", {}),
        )
        return JSONResponse(result, status_code=202 if result["state"] == "QUEUED" else 200)

    async def control(request):
        who = owner(request)
        data = ControlRequest.model_validate(await body(request)).model_dump()
        command = request.path_params["command"]
        token = data.get("lease_token")
        if command == "cancel" and data.get("target") is None:
            raise OpenLoloError("INVALID_REQUEST", "Cancellation requires a target operation ID", 400)
        coordinator = runtime.coordinator
        # Stop is always available to an authenticated local owner, including after lease loss.
        if command not in {"acquire", "pause"}:
            coordinator.check(who, token, allow_paused=True)
        if command not in {"acquire", "renew", "release", "pause", "resume", "cancel"}:
            raise OpenLoloError("UNKNOWN_OPERATION", status=404)
        record, fresh = runtime.journal.reserve(
            data["operation_id"], who, command, {"target": data.get("target")}
        )
        if not fresh:
            result = record
            if command == "acquire" and record["state"] == "SUCCEEDED":
                result = {**record, "control": coordinator.lease_status(who, include_token=True)}
            return JSONResponse(result)
        methods = {
            "acquire": lambda: phone.acquire_control(who),
            "renew": lambda: phone.renew_control(who, token),
            "release": lambda: phone.release_control(who, token),
            "pause": phone.pause,
            "resume": lambda: phone.resume(who, token),
            "cancel": lambda: phone.cancel(data["target"], who),
        }
        runtime.journal.update(data["operation_id"], "DISPATCHED")
        try:
            result = methods[command]()
            if inspect.isawaitable(result):
                result = await result
            public = {k: v for k, v in result.items() if k != "lease_token"}
            record = runtime.journal.update(data["operation_id"], "SUCCEEDED", public)
            if command in {"acquire", "renew"}:
                record["control"] = result
            return JSONResponse(record)
        except OpenLoloError as exc:
            runtime.journal.update(data["operation_id"], "FAILED", exc.as_dict())
            raise

    # Static filenames are fixed, never interpreted as filesystem paths.
    async def asset(request):
        return FileResponse(WEB / request.url.path.lstrip("/"))

    routes = [
        Route("/", static),
        Route("/app.js", asset),
        Route("/styles.css", asset),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/status", status),
        Route("/api/calibration", calibration_status),
        Route("/api/capabilities", capabilities),
        Route("/api/screenshot", screenshot, methods=["POST"]),
        Route("/api/full_page", full_page, methods=["POST"]),
        Route("/api/full_page/{id}", full_page_result),
        Route("/api/apps", apps),
        Route("/api/clipboard", clipboard),
        Route("/api/profiles", app_profiles),
        Route("/api/profiles/{bundle_id}", app_profile),
        Route("/api/links", open_link, methods=["POST"]),
        Route("/api/device", device),
        Route("/api/discover", discover),
        Route("/api/actions", action, methods=["POST"]),
        Route("/api/operations/{id}", operation),
        Route("/api/control/{command}", control, methods=["POST"]),
        Route("/api/setup/{kind}", setup, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={
            OpenLoloError: error_handler,
            ValidationError: error_handler,
            KeyError: error_handler,
            TypeError: error_handler,
        },
    )
    return Security(app, runtime)


def calibration_app(runtime):
    calibration = runtime.calibration

    async def route(request):
        token = request.path_params["token"]
        try:
            calibration.current(token)
            resource = request.path_params.get("resource", "")
            if resource == "events" and request.method == "POST":
                expected = f"http://{runtime.config.calibration_host}:{runtime.config.calibration_port}"
                if request.headers.get("origin") != expected:
                    raise OpenLoloError("ORIGIN_REJECTED", status=403)
                calibration.telemetry(token, await body(request, 4096))
                return Response(status_code=204)
            if request.method != "GET":
                return Response(status_code=404)
            if resource == "state":
                return JSONResponse(calibration.public(token), headers={"Cache-Control": "no-store"})
            if resource in {"", "calibration.js"}:
                return FileResponse(
                    WEB / ("calibration.html" if not resource else resource),
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            return Response(status_code=404)
        except OpenLoloError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
        except (ValueError, TypeError):
            return Response(status_code=400)

    return Starlette(
        routes=[Route("/{token}/", route), Route("/{token}/{resource}", route, methods=["GET", "POST"])]
    )
