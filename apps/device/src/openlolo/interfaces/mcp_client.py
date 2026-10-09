"""Loopback HTTP bridge. Hardware, identity, leases and journaling stay on the Pi."""

import asyncio
import contextlib
import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx

# Long-edge pixel bound for images the bridge returns: the screenshot tool default and every
# post-action observation. Touch coordinates are normalized, so a smaller image loses nothing
# but transfer time; pass a larger max_size to openlolo_screenshot when small text matters.
OBSERVATION_MAX_SIZE = 1536
# Seconds without a mutation after which the bridge gives the lease back on its own, so a
# finished or abandoned task does not keep the phone busy for other clients.
IDLE_RELEASE_SECONDS = 90.0

# Pre-dispatch refusals that are not argument mistakes, so the model does not "fix and resend".
REJECTED_INSTRUCTIONS = {
    "CONTROL_BUSY": "Another client controls the phone right now; nothing ran. Wait for its lease to "
    "lapse (see status.control.remaining_seconds) and retry, or report it.",
    "CONTROL_REQUIRED": "Control was lost after an interruption (status.mcp.lease_renewal_error); nothing "
    "ran. Take a screenshot, then call openlolo_control acquire explicitly to continue.",
    "PAUSED": "Input is paused; nothing ran. Only resume through openlolo_control when the user intends it.",
}


class BridgeError(Exception):
    def __init__(
        self,
        code: str,
        operation_id: str | None = None,
        status: int | None = None,
        *,
        rejected: bool = False,
    ):
        super().__init__(code)
        self.code, self.operation_id, self.status = code, operation_id, status
        # The API refused the mutation before recording it: nothing reached the phone.
        self.rejected = rejected

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code}
        if self.rejected:
            result.update(
                dispatched=False,
                instruction=REJECTED_INSTRUCTIONS.get(
                    self.code,
                    "Rejected before dispatch; nothing ran on the phone and no operation was "
                    "recorded. Correct the arguments and send the request again.",
                ),
            )
        elif self.operation_id:
            result.update(
                operation_id=self.operation_id, instruction="Inspect operation_status; do not replay input."
            )
        return result


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("Use a literal loopback HTTP endpoint through an SSH tunnel")
    return f"http://{parsed.hostname}:{parsed.port}"


@contextlib.contextmanager
def private_session(path: Path):
    """Lock the dedicated session inode for this stdio process; never expose its cookie."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("MCP session must be an owner-only regular file (chmod 600)")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This session is already in use by another MCP process") from None
        with os.fdopen(os.dup(fd)) as handle:
            data = json.load(handle)
        url = endpoint(data["url"])
        cookie = data["session"]
        if not isinstance(cookie, str) or not 1 <= len(cookie) <= 256:
            raise ValueError("Invalid local session")
        yield url, cookie
    finally:
        os.close(fd)


class PhoneClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        renew_seconds: float = 15,
        wait_seconds: float = 30,
        idle_seconds: float = IDLE_RELEASE_SECONDS,
        transport: str = "stdio",
        observation: dict | None = None,
    ):
        self.http = http
        self.transport = transport
        self.renew_seconds, self.wait_seconds = renew_seconds, wait_seconds
        # Checked on each renewal tick, so release happens within one renew interval of the deadline.
        self.idle_seconds = idle_seconds
        self.last_mutation = time.monotonic()
        self.in_flight = 0
        # Capture options for the post-action image. Bounded so a phone-resolution frame does not
        # ride every action result; models downscale beyond this anyway.
        self.observation = {"format": "jpeg", "max_size": OBSERVATION_MAX_SIZE, **(observation or {})}
        self.lease: str | None = None
        self.lease_error: str | None = None
        self.renew_task: asyncio.Task | None = None
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
        result["mcp"] = {"lease_renewal_error": self.lease_error, "transport": self.transport}
        return result

    async def _control(self, command: str, operation_id: str, target: str | None = None):
        payload = {"operation_id": operation_id, "lease_token": self.lease}
        if target is not None:
            payload["target"] = target
        return await self.request("POST", "control/" + command, payload, timeout=4)

    def stop_renewing(self):
        if self.renew_task and self.renew_task is not asyncio.current_task():
            self.renew_task.cancel()
        self.renew_task = None

    def start_renewing(self):
        self.stop_renewing()
        self.renew_task = asyncio.create_task(self._renew())

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(self.renew_seconds)
                async with self.control_lock:
                    idle = time.monotonic() - self.last_mutation
                    if self.idle_seconds and not self.in_flight and idle >= self.idle_seconds:
                        # Idle release is not a loss of control: the next action acquires again.
                        with contextlib.suppress(BridgeError):
                            await self._control("release", str(uuid4()))
                        self.lease = None
                        self.renew_task = None
                        return
                    await self._control("renew", str(uuid4()))
        except BridgeError as exc:
            self.lease_error = exc.code
            # Do not reacquire or resume automatically after expiry, revocation or API failure.
            with contextlib.suppress(BridgeError):
                await self._control("release", str(uuid4()))
        except asyncio.CancelledError:
            pass

    async def control(self, command: str, operation_id: str, target: str | None = None):
        if command == "pause":
            self.stop_renewing()
            return await self._control(command, operation_id)
        async with self.control_lock:
            if command not in {"acquire", "release"} and (self.lease is None or self.lease_error):
                raise BridgeError("CONTROL_REQUIRED")
            result = await self._control(command, operation_id, target)
            if command == "acquire":
                self.lease = result.get("control", {}).get("lease_token")
                if not self.lease:
                    raise BridgeError("CONTROL_REQUIRED")
                # An already-owned lease can have less than 15 seconds left.
                await self._control("renew", str(uuid4()))
                self.lease_error = None
                if not result["control"].get("paused"):
                    self.start_renewing()
            elif command == "resume":
                await self._control("renew", str(uuid4()))
                self.start_renewing()
            elif command == "release":
                self.stop_renewing()
                self.lease = None
            result.get("control", {}).pop("lease_token", None)
            return result

    async def screenshot(self, options=None):
        return await self.request("POST", "screenshot", options or {})

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
        if self.lease_error:
            # Lost after an interruption (expiry, revocation, owner takeover): never continue silently.
            raise BridgeError("CONTROL_REQUIRED", rejected=True)
        if self.lease is None:
            # First action of a task, or the first after an idle release: the box decides ownership.
            # CONTROL_BUSY (another client holds the lease) comes back before anything is dispatched.
            try:
                await self.control("acquire", str(uuid4()))
            except BridgeError as exc:
                exc.rejected = True
                raise
        self.last_mutation = started = time.monotonic()
        self.in_flight += 1
        try:
            try:
                result = await self.request(
                    "POST", path, {"operation_id": operation_id, "lease_token": self.lease, **payload}
                )
            except BridgeError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    # The API answered with a validation or state error before journaling anything.
                    exc.rejected = True
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
        finally:
            # Idle time counts from the end of the last mutation; never release one in flight.
            self.in_flight -= 1
            self.last_mutation = time.monotonic()

    async def close(self):
        task = self.renew_task
        self.stop_renewing()
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            if self.lease:
                with contextlib.suppress(BridgeError):
                    await self._control("release", str(uuid4()))
            await self.http.aclose()
