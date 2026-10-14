"""SDK behaviour against a fake box (httpx.MockTransport); the real box is covered in apps/device."""

import json
import os
import stat
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest

from openlolo_sdk import (
    ApprovalDenied,
    ApprovalRequired,
    FrameExpired,
    OutcomeUnknown,
    Phone,
    PinMismatch,
    RateLimited,
    ResponseTooLarge,
)
from openlolo_sdk._auth import Store
from openlolo_sdk._tls import Pin
from openlolo_sdk.phone import base_url, is_lan

BOX = "https://box.example"
PNG = "iVBORw0KGgo="  # Any base64 works; the SDK does not decode images.


class FakeBox:
    """Just enough OAuth and MCP to drive the SDK; records every tool call."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.device_requests = 0
        self.refreshes = 0
        self.polls: list[str] = ["authorization_pending"]
        self.tool_results: list = []  # Callables or dicts consumed per tools/call, then default.
        self.drop_next = 0
        self.frame = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": BOX,
                    "token_endpoint": BOX + "/token",
                    "device_authorization_endpoint": BOX + "/oauth/device_authorization",
                },
            )
        if path == "/oauth/device_authorization":
            self.device_requests += 1
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "BCDF-GHJK",
                    "verification_uri": BOX,
                    "expires_in": 600,
                    "interval": 0.1,
                    "client_id": "cid",
                },
            )
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                if form["refresh_token"] == "dead":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self.tokens(f"r{self.refreshes}"))
            outcome = self.polls.pop(0) if self.polls else "ok"
            if outcome != "ok":
                return httpx.Response(400, json={"error": outcome})
            return httpx.Response(200, json=self.tokens("r0"))
        if path == "/mcp":
            if self.drop_next:
                self.drop_next -= 1
                raise httpx.ConnectError("dropped")
            message = json.loads(request.content)
            if message["method"] == "tools/list":
                tools = [{"name": "openlolo_launch_app", "inputSchema": {"properties": {"name": {}}}}]
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}}
                )
            name, arguments = message["params"]["name"], message["params"]["arguments"]
            self.calls.append((name, arguments))
            result = self.tool_results.pop(0) if self.tool_results else None
            if callable(result):
                result = result(name, arguments)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "result": result or self.ok(arguments)}
            )
        return httpx.Response(404)

    @staticmethod
    def tokens(refresh: str) -> dict:
        return {
            "access_token": "a-" + refresh,
            "expires_in": 900,
            "refresh_token": refresh,
            "scope": "phone:read",
        }

    def observation(self) -> dict:
        self.frame += 1
        return {
            "metadata": {"frame_id": f"f{self.frame}", "geometry_epoch": "g", "width": 100, "height": 200}
        }

    def ok(self, arguments: dict) -> dict:
        data = {"operation": {"id": arguments.get("operation_id"), "state": "SUCCEEDED"}}
        content: list = [{"type": "text", "text": "{}"}]
        if arguments.get("observe", True):
            data["observation"] = self.observation()
            content.append({"type": "image", "data": PNG, "mimeType": "image/jpeg"})
        return {"content": content, "structuredContent": data, "isError": False}


@pytest.fixture
def box():
    return FakeBox()


def phone_for(box, tmp_path, **options):
    options.setdefault("allow_approval", True)
    return Phone(
        BOX,
        cache_dir=tmp_path,
        transport=httpx.MockTransport(box.handler),
        notify=lambda text: None,
        **options,
    )


def screenshot_result(box):
    observation = box.observation()
    return {
        "content": [{"type": "image", "data": PNG, "mimeType": "image/jpeg"}],
        "structuredContent": observation,
        "isError": False,
    }


def test_addresses():
    assert base_url("openlolo-ab12") == "https://openlolo-ab12.local"
    assert base_url("OpenLolo-AB12.local") == "https://openlolo-ab12.local"
    assert base_url("https://pi.tailnet.ts.net/") == "https://pi.tailnet.ts.net"
    for bad in ("http://box.local", "https://u:p@box.local", "https://box.local/x"):
        with pytest.raises(ValueError):
            base_url(bad)
    assert is_lan("openlolo-ab12.local") and is_lan("192.168.1.4") and is_lan("127.0.0.1")
    assert not is_lan("pi.tailnet.ts.net") and not is_lan("8.8.8.8")


def test_first_run_approves_caches_privately_and_second_run_does_not_ask(box, tmp_path):
    with phone_for(box, tmp_path, name="nightly.py") as phone:
        phone.open_app("com.apple.mobilenotes").type("hello").tap(0.5, 0.5).swipe(0.5, 0.8, 0.5, 0.2).home()
    assert box.device_requests == 1
    names = [name for name, _ in box.calls]
    assert names[:5] == [
        "openlolo_launch_app",
        "openlolo_type_text",
        "openlolo_tap",
        "openlolo_swipe",
        "openlolo_button",
    ]
    assert names[-1] == "openlolo_control"  # close() releases control.
    launch, typed, tap, swipe = (args for _, args in box.calls[:4])
    assert launch["bundle_id"] == "com.apple.mobilenotes"
    assert typed["text"] == "hello"
    # Each touch refers to the frame the previous action returned.
    assert tap["frame_id"] == "f2" and swipe["frame_id"] == "f3"
    assert len({args["operation_id"] for _, args in box.calls}) == len(box.calls)

    (cache,) = tmp_path.glob("*.json")
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert json.loads(cache.read_text())["client_id"] == "cid"

    with phone_for(box, tmp_path, allow_approval=False) as again:
        again.home()
    assert box.device_requests == 1 and box.refreshes == 0


def test_expired_access_refreshes_headlessly_and_dead_grant_needs_approval(box, tmp_path):
    with phone_for(box, tmp_path) as phone:
        phone.status()
    store = Store(BOX, tmp_path)
    data = store.load()
    store.save({**data, "access_expires": 0})
    with phone_for(box, tmp_path, allow_approval=False) as phone:
        phone.status()
    assert box.refreshes == 1 and box.device_requests == 1

    store.save({**store.load(), "access_expires": 0, "refresh_token": "dead"})
    started = time.monotonic()
    with pytest.raises(ApprovalRequired):
        phone_for(box, tmp_path, allow_approval=False).status()
    assert time.monotonic() - started < 1


def test_denied_approval(box, tmp_path):
    box.polls = ["authorization_pending", "slow_down", "access_denied"]
    phone = phone_for(box, tmp_path)
    phone.auth.sleep = lambda seconds: None
    with pytest.raises(ApprovalDenied):
        phone.status()
    assert not Store(BOX, tmp_path).load().get("access_token")


def test_concurrent_refresh_rotates_once(box, tmp_path):
    with phone_for(box, tmp_path) as phone:
        phone.status()
    store = Store(BOX, tmp_path)
    store.save({**store.load(), "access_expires": 0})
    phones = [phone_for(box, tmp_path, allow_approval=False) for _ in range(4)]
    gate = threading.Barrier(len(phones))

    def run(p):
        gate.wait()
        p.auth.token()

    threads = [threading.Thread(target=run, args=(p,)) for p in phones]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # One process rotated; the others found the fresh pair under the lock.
    assert box.refreshes == 1


def test_dropped_connection_retries_same_operation_then_reads_journal(box, tmp_path):
    phone = phone_for(box, tmp_path, observe=False)
    phone.status()
    box.drop_next = 1
    phone.home()
    # The dropped attempt never reached the fake box; the retry did, exactly once.
    assert [name for name, _ in box.calls].count("openlolo_button") == 1

    box.drop_next = 2
    box.tool_results = [
        {"content": [], "structuredContent": {"id": "x", "state": "SUCCEEDED"}, "isError": False}
    ]
    with pytest.raises(OutcomeUnknown) as info:
        phone.key("enter")
    assert info.value.detail["operation"]["state"] == "SUCCEEDED"
    looked_up = box.calls[-1]
    assert looked_up[0] == "openlolo_operation_status"


def test_frame_expired_is_raised_not_retried(box, tmp_path):
    phone = phone_for(box, tmp_path)
    box.tool_results = [screenshot_result(box)]
    frame = phone.screenshot()
    assert frame.id == "f1" and frame.image
    refused = {"code": "FRAME_EXPIRED", "dispatched": False}
    box.tool_results = [{"content": [], "structuredContent": {"error": refused}, "isError": True}]
    with pytest.raises(FrameExpired) as info:
        frame.tap(10, 20)
    assert info.value.dispatched is False
    taps = [args for name, args in box.calls if name == "openlolo_tap"]
    assert len(taps) == 1
    assert taps[0]["x"] == pytest.approx(10.5 / 100) and taps[0]["y"] == pytest.approx(20.5 / 200)
    with pytest.raises(ValueError):
        frame.tap(1.5, 0.5)
    with pytest.raises(ValueError):
        frame.tap(100, 5)


def test_rate_limit_backoff_then_error(box, tmp_path):
    phone = phone_for(box, tmp_path)
    phone.mcp.sleep = lambda seconds: None
    limited = {"content": [], "structuredContent": {"error": {"code": "RATE_LIMITED"}}, "isError": True}
    box.tool_results = [limited, limited, {"content": [], "structuredContent": {"ok": 1}, "isError": False}]
    assert phone.status() == {"ok": 1}
    box.tool_results = [limited] * 4
    with pytest.raises(RateLimited):
        phone.status()


def test_type_routes_long_or_unicode_text_through_clipboard(box, tmp_path):
    phone = phone_for(box, tmp_path, observe=False)
    phone.type("hi there").type("héllo").type("x" * 201)
    assert [name for name, _ in box.calls] == [
        "openlolo_type_text",
        "openlolo_send_text",
        "openlolo_send_text",
    ]
    with pytest.raises(ValueError):
        phone.paste("x" * 70_000)
    with pytest.raises(ValueError):
        phone.open_app("Notes")  # Bundle ids only; apps("notes") finds it.


def test_response_cap(box, tmp_path, monkeypatch):
    import openlolo_sdk._mcp as mcp

    phone = phone_for(box, tmp_path)
    phone.status()
    monkeypatch.setattr(mcp, "MAX_RESPONSE", 10)
    with pytest.raises(ResponseTooLarge):
        phone.status()


def test_pin_trust_on_first_use_then_strict():
    pin = Pin()
    pin.check(b"cert-a")
    pin.check(b"cert-a")
    with pytest.raises(PinMismatch):
        pin.check(b"cert-b")
    strict = Pin("ab" * 32)
    with pytest.raises(PinMismatch):
        strict.check(b"cert-a")
    with pytest.raises(ValueError):
        Pin("abc")


def test_cache_ignores_other_boxes_and_garbage(tmp_path):
    store = Store(BOX, tmp_path)
    store.save({"client_id": "x"})
    assert Store("https://other.example", tmp_path).load() == {}
    store.path.write_text("not json")
    assert store.load() == {}
    fresh = Store(BOX, tmp_path / "new")
    fresh.save({"client_id": "x"})
    assert stat.S_IMODE(os.stat(tmp_path / "new").st_mode) == 0o700
