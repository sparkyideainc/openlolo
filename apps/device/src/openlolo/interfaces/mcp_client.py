"""In-process HTTP bridge from the MCP gateway to the Phone API. Hardware, identity, leases and journaling stay on the Pi."""

import asyncio
import contextlib
import time
from urllib.parse import urlencode
from uuid import uuid4

import anyio
import httpx

from openlolo.interfaces.local_phone import (  # noqa: F401 - re-exported for the HTTP bridge
    LOST_CONTROL,
    OBSERVATION_MAX_SIZE,
    REJECTED_INSTRUCTIONS,
    BridgeError,
)


class PhoneClient:
    """Bridge to the Phone API. Holds the lease token only; the box keeps the lease alive while
    this client acts or captures with it and lets it lapse after ``lease_seconds`` of quiet, so
    there is no timer here."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        wait_seconds: float = 30,
        transport: str = "streamable-http",
        observation: dict | None = None,
    ):
        self.http = http
        self.transport = transport
        self.wait_seconds = wait_seconds
        # Capture options for the post-action image. Bounded so a phone-resolution frame does not
        # ride every action result; models downscale beyond this anyway.
        self.observation = {"format": "jpeg", "max_size": OBSERVATION_MAX_SIZE, **(observation or {})}
        self.lease: str | None = None
        self.control_lock = asyncio.Lock()

    async def request(self, method: str, path: str, payload=None, *, timeout: float = 30) -> dict:
        op_id = payload.get("operation_id") if payload else None
        try:
            response = await self.http.request(method, "/api/" + path, json=payload, timeout=timeout)
        except httpx.HTTPError:
            raise BridgeError("API_UNREACHABLE", op_id) from None
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
        except ValueError:
            raise BridgeError("INVALID_API_RESPONSE", op_id) from None
        if response.is_error or response.is_redirect:
            code = data.get("error", {}).get("code", "API_REJECTED")
            # Only error codes cross this boundary, never HTTP bodies, cookies or typed content.
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                code = "API_REJECTED"
            raise BridgeError(code, op_id, response.status_code)
        return data

    async def status(self):
        result = await self.request("GET", "status")
        # Keep the saved score useful without flooding the model with 1,000 trial rows.
        result.get("profile", {}).get("calibration", {}).pop("samples", None)
        result["mcp"] = {"transport": self.transport}
        return result

    async def capabilities(self):
        return await self.request("GET", "capabilities")

    async def apps(self, query: str = "", include_hidden: bool = True):
        params = urlencode({"query": query, "include_hidden": str(include_hidden).lower()})
        return await self.request("GET", "apps?" + params)

    async def app_profiles(self):
        return await self.request("GET", "profiles")

    async def app_profile(self, bundle_id: str):
        return await self.request("GET", "profiles/" + bundle_id)

    async def device_info(self):
        return await self.request("GET", "device")

    async def clipboard(self):
        return await self.request("GET", "clipboard")

    async def discover(self):
        return await self.request("GET", "discover", timeout=40)

    async def setup(self, kind: str, operation_id: str, payload: dict):
        body = {"operation_id": operation_id, "lease_token": self.lease, "payload": payload}
        return await self.request("POST", f"setup/{kind}", body, timeout=30)

    async def _control(self, command: str, operation_id: str, target: str | None = None):
        payload = {"operation_id": operation_id, "lease_token": self.lease}
        if target is not None:
            payload["target"] = target
        return await self.request("POST", "control/" + command, payload, timeout=4)

    def forget(self, exc: BridgeError):
        if exc.code in LOST_CONTROL:
            self.lease = None

    async def _acquire(self):
        result = await self._control("acquire", str(uuid4()))
        self.lease = result.get("control", {}).get("lease_token")
        if not self.lease:
            raise BridgeError("INVALID_API_RESPONSE")
        return result

    async def ensure_control(self):
        """Take the lease when this client holds none. CONTROL_BUSY comes back before any dispatch."""
        async with self.control_lock:
            if self.lease is None:
                await self._acquire()

    async def control(self, command: str, operation_id: str, target: str | None = None):
        if command == "pause":
            return await self._control(command, operation_id)
        async with self.control_lock:
            if command == "release" and self.lease is None:
                # Nothing held here (never acquired, or it lapsed): already released.
                return {"released": True, "control": (await self.request("GET", "status"))["control"]}
            if command in {"resume", "cancel"} and self.lease is None:
                await self._acquire()
            try:
                if command == "acquire":
                    result = await self._acquire()
                else:
                    result = await self._control(command, operation_id, target)
            except BridgeError as exc:
                self.forget(exc)
                if command == "release" and exc.code in LOST_CONTROL:
                    return {"released": True, "control": (await self.request("GET", "status"))["control"]}
                raise
            if command == "release":
                self.lease = None
            result.get("control", {}).pop("lease_token", None)
            return result

    async def screenshot(self, options=None):
        # The token marks the capture as the holder's activity; a stale one is ignored by the box.
        payload = {**(options or {}), **({"lease_token": self.lease} if self.lease else {})}
        return await self.request("POST", "screenshot", payload)

    async def operation(self, operation_id: str):
        return await self.request("GET", "operations/" + operation_id)

    async def action(self, operation_id: str, action: dict, observe: bool, settle: float = 0):
        return await self._mutate("actions", operation_id, {"action": action}, observe, settle)

    async def link(
        self, operation_id: str, bundle_id: str, link: str, params: dict, observe: bool, settle: float = 0
    ):
        payload = {"bundle_id": bundle_id, "link": link, "params": params}
        return await self._mutate("links", operation_id, payload, observe, settle)

    async def full_page(self, operation_id: str, options: dict):
        """Scroll-and-capture on the box; the images come back with the operation.

        Each page costs a flick, a settle and a capture (3 s on a Pi 4, up to 15 s on a Pi 3B
        with a slow phone link), so the wait scales with the page budget instead of the
        30-second action deadline. The box keeps going if this wait runs out; the operation is
        then returned as DISPATCHED and its images stay fetchable under its id."""
        pages = int(options.get("max_pages", 20))

        async def images():
            return await self.request("GET", "full_page/" + operation_id, timeout=60)

        return await self._mutate(
            "full_page", operation_id, {"options": options}, False, 0, fetch=images, wait=30 + 15 * pages
        )

    async def _mutate(
        self,
        path: str,
        operation_id: str,
        payload: dict,
        observe: bool,
        settle: float = 0,
        *,
        fetch=None,
        wait: float | None = None,
    ):
        """POST the mutation, wait for its outcome, optionally settle, then capture once.

        ``settle`` is the bounded wait between a completed action and its observation, so the
        returned image shows the screen after the transition the action started (an app
        launch, a search) instead of the frame before it. ``timing`` reports where the seconds
        went so slow calls can be attributed to the phone, the settle, the capture or the path
        outside this bridge. ``fetch`` replaces the observation with the operation's own result
        (a full-page capture's images); ``wait`` overrides the outcome deadline."""
        # First action of a task, or the first after the lease lapsed: the box decides ownership.
        try:
            await self.ensure_control()
        except BridgeError as exc:
            exc.rejected = True
            raise
        started = time.monotonic()
        try:
            try:
                result = await self.request(
                    "POST", path, {"operation_id": operation_id, "lease_token": self.lease, **payload}
                )
            except BridgeError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    # The API answered with a validation or state error before journaling anything.
                    # An expired token is dropped and never replayed; the next call acquires again.
                    exc.rejected = True
                    self.forget(exc)
                raise
            deadline = time.monotonic() + (self.wait_seconds if wait is None else wait)
            while result["state"] in {"QUEUED", "DISPATCHED"} and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                result = await self.operation(operation_id)
            timing = {"action_seconds": round(time.monotonic() - started, 3)}
            output = {"operation": result, "timing": timing}
            if result["state"] == "SUCCEEDED" and fetch is not None:
                fetched = await fetch()
                fetched.pop("operation", None)
                output.update(fetched)
            elif result["state"] == "SUCCEEDED" and observe:
                if settle > 0:
                    await asyncio.sleep(settle)
                    timing["settle_seconds"] = settle
                capture_started = time.monotonic()
                try:
                    output["observation"] = await self.screenshot(self.observation)
                except BridgeError as exc:
                    # A failed post-action observation must never turn a completed action into a retry.
                    output["observation_error"] = exc.public()
                timing["observation_seconds"] = round(time.monotonic() - capture_started, 3)
            timing["bridge_seconds"] = round(time.monotonic() - started, 3)
            return output
        except BridgeError as exc:
            # Polling can fail after a successful POST; retain the original lookup key.
            if exc.operation_id is None:
                exc.operation_id = operation_id
            raise
        except asyncio.CancelledError:
            # MCP request cancellation must reach hardware even inside an AnyIO cancel scope.
            with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                with contextlib.suppress(BridgeError):
                    await self.control("pause", str(uuid4()))
                with contextlib.suppress(BridgeError):
                    await self._control("cancel", str(uuid4()), operation_id)
            raise

    async def close(self):
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            if self.lease:
                with contextlib.suppress(BridgeError):
                    await self._control("release", str(uuid4()))
            await self.http.aclose()
