import asyncio
import base64
import json
from uuid import uuid4

import httpx
import pytest
from mcp import Client

from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.http_api import create_app
from openlolo.interfaces.mcp_client import BridgeError, PhoneClient
from openlolo.interfaces.mcp_server import create_server


async def bridge_for(runtime, **kwargs):
    cookie = runtime.auth.login(runtime.auth.issue())
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, False)),
        base_url="http://testserver",
        headers={"X-OpenLolo-Client": "cli"},
        cookies={"openlolo_session": cookie},
    )
    return PhoneClient(http, **kwargs)


async def acquire(client):
    result = await client.call_tool("openlolo_control", {"command": "acquire", "operation_id": str(uuid4())})
    assert not result.is_error, result
    assert "lease_token" not in json.dumps(result.structured_content)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_observe_act_observe_dedup_and_validation(runtime, mode):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge), mode=mode) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert {"openlolo_pause", "openlolo_button", "openlolo_apps", "openlolo_launch_app"} <= names
        assert {"openlolo_send_text", "openlolo_clipboard", "openlolo_open_url"} <= names
        assert {"openlolo_app_profiles", "openlolo_app_profile", "openlolo_open_link"} <= names
        assert "openlolo_move" not in names
        await acquire(client)
        screenshot = await client.call_tool("openlolo_screenshot", {})
        assert not screenshot.is_error
        image = next(b for b in screenshot.content if b.type == "image")
        assert base64.b64decode(image.data).startswith(b"\xff\xd8")
        frame = screenshot.structured_content["metadata"]
        request = dict(
            operation_id=str(uuid4()),
            frame_id=frame["frame_id"],
            geometry_epoch=frame["geometry_epoch"],
            x=0.5,
            y=0.5,
        )
        tapped = await client.call_tool("openlolo_tap", request)
        assert not tapped.is_error, tapped
        assert tapped.structured_content["operation"]["state"] == "SUCCEEDED"
        assert tapped.structured_content["observation"]["metadata"]["frame_id"] != frame["frame_id"]
        reports = len(runtime.phone.device.reports)
        repeated = await client.call_tool("openlolo_tap", request)
        assert not repeated.is_error and len(runtime.phone.device.reports) == reports
        conflict = await client.call_tool("openlolo_tap", {**request, "x": 0.7})
        assert conflict.is_error and conflict.structured_content["error"]["code"] == "OPERATION_CONFLICT"
        stale = await client.call_tool("openlolo_tap", {**request, "operation_id": str(uuid4())})
        assert stale.is_error and stale.structured_content["error"]["code"] == "FRAME_EXPIRED"
        invalid = await client.call_tool(
            "openlolo_type_text", {"operation_id": str(uuid4()), "text": "private\ntext"}
        )
        assert invalid.is_error and len(runtime.phone.device.reports) == reports
        assert "private" not in json.dumps(invalid.structured_content)
        pressed = await client.call_tool(
            "openlolo_button", {"operation_id": str(uuid4()), "button": "home", "observe": False}
        )
        assert not pressed.is_error and pressed.structured_content["operation"]["state"] == "SUCCEEDED"
        assert ("button", [12, 64, "down"]) in runtime.phone.device.reports
        launched = await client.call_tool(
            "openlolo_launch_app",
            {"operation_id": str(uuid4()), "bundle_id": "com.apple.Preferences", "observe": False},
        )
        assert not launched.is_error and launched.structured_content["operation"]["state"] == "SUCCEEDED"
        assert ("launch_app", "com.apple.Preferences") in runtime.phone.device.reports
        sent = await client.call_tool(
            "openlolo_send_text", {"operation_id": str(uuid4()), "text": "héllo 🚀", "observe": False}
        )
        assert not sent.is_error and sent.structured_content["operation"]["result"]["clipboard_replaced"]
        clipboard = await client.call_tool("openlolo_clipboard", {})
        assert clipboard.structured_content["text"] == "héllo 🚀"
        profile = await client.call_tool("openlolo_app_profile", {"bundle_id": "com.apple.mobilesafari"})
        assert profile.structured_content["deep_links"]["page"]["params"] == ["url"]
        linked = await client.call_tool(
            "openlolo_open_link",
            {
                "operation_id": str(uuid4()),
                "bundle_id": "com.apple.mobilesafari",
                "link": "page",
                "params": {"url": "https://example.org/"},
                "observe": False,
            },
        )
        assert not linked.is_error and linked.structured_content["operation"]["state"] == "SUCCEEDED"
        resources = await client.list_resource_templates()
        assert any("openlolo://apps/" in t.uri_template for t in resources.resource_templates)
        apps = await client.call_tool("openlolo_apps", {})
        assert not apps.is_error and apps.structured_content["apps"][0]["bundle_id"]
        info = await client.call_tool("openlolo_device_info", {})
        assert not info.is_error and info.structured_content["transport"] == "usb"
        assert (await client.call_tool("openlolo_pause", {"operation_id": str(uuid4())})).is_error is False
        paused = await client.call_tool("openlolo_key", {"operation_id": str(uuid4()), "key": "enter"})
        assert paused.is_error and paused.structured_content["error"]["code"] == "PAUSED"
    assert runtime.coordinator.lease is None


async def test_mcp_generated_operation_ids_settle_timing_and_app_query(runtime):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge)) as client:
        acquired = await client.call_tool("openlolo_control", {"command": "acquire"})
        assert not acquired.is_error, acquired
        schema = next(t for t in (await client.list_tools()).tools if t.name == "openlolo_tap").input_schema
        assert "operation_id" not in schema.get("required", [])
        screenshot = await client.call_tool("openlolo_screenshot", {})
        frame = screenshot.structured_content["metadata"]
        assert frame["width"] <= 1536 and frame["height"] <= 1536
        tapped = await client.call_tool(
            "openlolo_tap",
            {"frame_id": frame["frame_id"], "geometry_epoch": frame["geometry_epoch"], "x": 0.5, "y": 0.5},
        )
        assert not tapped.is_error, tapped
        timing = tapped.structured_content["timing"]
        assert timing["settle_seconds"] == 0.5 and timing["bridge_seconds"] >= 0.5
        assert timing["observation_seconds"] >= 0 and timing["action_seconds"] >= 0
        assert tapped.structured_content["observation"]["metadata"]["width"] <= 1536
        quick = await client.call_tool(
            "openlolo_tap",
            {
                "frame_id": tapped.structured_content["observation"]["metadata"]["frame_id"],
                "geometry_epoch": frame["geometry_epoch"],
                "x": 0.5,
                "y": 0.5,
                "settle": 0,
            },
        )
        assert not quick.is_error and "settle_seconds" not in quick.structured_content["timing"]
        rejected = await client.call_tool(
            "openlolo_open_link",
            {"bundle_id": "net.whatsapp.WhatsApp", "link": "chat_with_text", "params": {"phone": "1"}},
        )
        assert rejected.is_error
        error = rejected.structured_content["error"]
        assert error["code"] == "LINK_PARAMS" and error["dispatched"] is False
        assert "operation_id" not in error and "Correct the arguments" in error["instruction"]
        linked = await client.call_tool(
            "openlolo_open_link",
            {
                "bundle_id": "com.apple.mobilesafari",
                "link": "page",
                "params": {"url": "https://example.org/"},
                "observe": False,
            },
        )
        assert not linked.is_error and linked.structured_content["operation"]["state"] == "SUCCEEDED"
        assert "settle_seconds" not in linked.structured_content["timing"]
        apps = await client.call_tool("openlolo_apps", {"query": "SAF"})
        assert not apps.is_error and apps.structured_content["matched"] == 1
        assert apps.structured_content["apps"][0]["bundle_id"] == "com.apple.mobilesafari"
        none = await client.call_tool("openlolo_apps", {"query": "youtube"})
        assert not none.is_error and none.structured_content["apps"] == []
        everything = await client.call_tool("openlolo_apps", {})
        assert everything.structured_content["matched"] == 3
        assert not (await client.call_tool("openlolo_pause", {})).is_error


async def test_mcp_post_action_capture_failure_preserves_success(runtime):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge)) as client:
        await acquire(client)
        runtime.phone.device.failure = OpenLoloError("CAPTURE_TIMEOUT")
        result = await client.call_tool("openlolo_key", {"operation_id": str(uuid4()), "key": "enter"})
        assert not result.is_error
        assert result.structured_content["operation"]["state"] == "SUCCEEDED"
        assert result.structured_content["observation_error"]["code"] == "CAPTURE_TIMEOUT"
        assert not any(b.type == "image" for b in result.content)


async def test_mcp_pause_bypasses_slow_screenshot(runtime):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge)) as client:
        await acquire(client)
        runtime.phone.device.delay = 1
        capture = asyncio.create_task(client.call_tool("openlolo_screenshot", {}))
        await asyncio.sleep(0.1)
        stopped = await asyncio.wait_for(
            client.call_tool("openlolo_pause", {"operation_id": str(uuid4())}), 0.5
        )
        assert not stopped.is_error and runtime.coordinator.paused
        assert not capture.done()
        await capture


async def test_lapsed_lease_is_rejected_once_then_reacquired(runtime):
    runtime.coordinator.lease_seconds = 0.25
    bridge = await bridge_for(runtime)
    # No client-side timer of any kind.
    assert not [value for value in vars(bridge).values() if isinstance(value, asyncio.Task)]
    async with Client(create_server(bridge)) as client:
        assert runtime.coordinator.lease is None
        result = await client.call_tool("openlolo_key", {"key": "enter", "settle": 0})
        assert not result.is_error, result
        assert result.structured_content["operation"]["state"] == "SUCCEEDED"
        first = bridge.lease
        assert runtime.coordinator.lease is not None and first is not None
        status = await client.call_tool("openlolo_status", {})
        assert status.structured_content["control"]["owned"] is True
        assert "lease_renewal_error" not in status.structured_content["mcp"]
        await asyncio.sleep(0.45)
        # The box let the idle lease lapse; the bridge still holds the stale token.
        assert runtime.coordinator.lease is None and bridge.lease == first
        before = len(runtime.journal.list_operations())
        rejected = await client.call_tool("openlolo_key", {"key": "enter", "settle": 0})
        error = rejected.structured_content["error"]
        assert rejected.is_error and error["code"] == "LEASE_EXPIRED" and error["dispatched"] is False
        assert "screenshot" in error["instruction"] and bridge.lease is None
        # Nothing was journaled or replayed for the rejected call.
        assert len(runtime.journal.list_operations()) == before
        again = await client.call_tool("openlolo_key", {"key": "enter", "settle": 0})
        assert not again.is_error, again
        assert bridge.lease not in {None, first} and runtime.coordinator.lease is not None


async def test_bridge_screenshots_keep_control_and_release_hands_over(runtime):
    runtime.coordinator.lease_seconds = 0.3
    holder, other = await bridge_for(runtime), await bridge_for(runtime)
    await holder.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    for _ in range(3):
        await asyncio.sleep(0.2)
        await holder.screenshot({"max_size": 200})
    # 0.6 s since the action, kept alive by the holder's captures; another client's capture adds nothing.
    assert runtime.coordinator.lease is not None
    expires = runtime.coordinator.lease.expires
    await other.screenshot({"max_size": 200})
    assert runtime.coordinator.lease.expires == expires
    with pytest.raises(BridgeError) as busy:
        await other.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert busy.value.code == "CONTROL_BUSY" and busy.value.rejected
    # Owner override from the client that does not hold control.
    await other.control("pause", str(uuid4()))
    assert runtime.coordinator.paused
    with pytest.raises(BridgeError) as paused:
        await holder.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert paused.value.code == "PAUSED"
    await holder.control("resume", str(uuid4()))
    # renew is a harmless no-op.
    renewed = await holder.control("renew", str(uuid4()))
    assert renewed["state"] == "SUCCEEDED" and runtime.coordinator.lease.expires >= expires
    released = await holder.control("release", str(uuid4()))
    assert released["state"] == "SUCCEEDED" and holder.lease is None and runtime.coordinator.lease is None
    # Released twice, or after a lapse: already free, not an error.
    assert (await holder.control("release", str(uuid4())))["released"] is True
    await other.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert other.lease is not None and runtime.coordinator.lease is not None
    await holder.close()
    await other.close()
    assert runtime.coordinator.lease is None


async def test_busy_or_paused_phone_rejects_before_dispatch(runtime):
    holder = await bridge_for(runtime)
    await holder.control("acquire", str(uuid4()))
    bridge = await bridge_for(runtime)
    with pytest.raises(BridgeError) as busy:
        await bridge.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    public = busy.value.public()
    assert busy.value.code == "CONTROL_BUSY" and public["dispatched"] is False
    assert "Another client" in public["instruction"] and bridge.lease is None
    async with Client(create_server(bridge)) as client:  # closes the bridge on exit
        result = await client.call_tool("openlolo_key", {"key": "enter"})
        assert result.is_error and result.structured_content["error"]["code"] == "CONTROL_BUSY"
    await holder.close()
    assert runtime.coordinator.lease is None
    await runtime.coordinator.pause()
    late = await bridge_for(runtime)
    with pytest.raises(BridgeError) as paused:
        await late.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert paused.value.code == "PAUSED" and paused.value.public()["dispatched"] is False
    # The automatic acquire took the lease but never unpaused, and no key operation was journaled.
    assert runtime.coordinator.paused and late.lease is not None
    assert not [row for row in runtime.journal.list_operations() if row["kind"] == "key"]
    await late.close()


async def test_lost_action_response_is_not_replayed(runtime):
    asgi = httpx.ASGITransport(app=create_app(runtime, False))
    submissions = []

    class LoseResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await asgi.handle_async_request(request)
            if request.url.path == "/api/actions":
                submissions.append(request)
                raise httpx.ReadError("Response lost after dispatch")
            return response

    cookie = runtime.auth.login(runtime.auth.issue())
    http = httpx.AsyncClient(
        transport=LoseResponse(),
        base_url="http://testserver",
        headers={"X-OpenLolo-Client": "cli"},
        cookies={"openlolo_session": cookie},
    )
    bridge = PhoneClient(http)
    await bridge.control("acquire", str(uuid4()))
    op = str(uuid4())
    with pytest.raises(BridgeError) as error:
        await bridge.action(op, {"kind": "key", "key": "tab"}, False)
    assert error.value.public()["operation_id"] == op
    for _ in range(100):
        state = (await bridge.operation(op))["state"]
        if state == "SUCCEEDED":
            break
        await asyncio.sleep(0.01)
    assert state == "SUCCEEDED" and len(submissions) == 1
    runtime.auth.logout(cookie)
    with pytest.raises(BridgeError, match="LOGIN_REQUIRED"):
        await bridge.status()
    await bridge.close()


async def test_bridge_cancellation_stops_input(runtime):
    bridge = await bridge_for(runtime)
    await bridge.control("acquire", str(uuid4()))
    await bridge.screenshot()
    frame = runtime.phone.frame
    op = str(uuid4())
    task = asyncio.create_task(
        bridge.action(
            op,
            {
                "kind": "long_press",
                "frame_id": frame.frame_id,
                "geometry_epoch": frame.geometry_epoch,
                "x": 0.5,
                "y": 0.5,
                "seconds": 2,
            },
            False,
        )
    )
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.coordinator.paused and runtime.phone.device.generation is None
    for _ in range(50):
        if (await bridge.operation(op))["state"] != "DISPATCHED":
            break
        await asyncio.sleep(0.01)
    assert (await bridge.operation(op))["state"] in {"CANCELLED", "OUTCOME_UNKNOWN"}
    await bridge.close()


async def test_mcp_request_cancellation_reaches_hardware(runtime):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge), mode="legacy") as client:
        await acquire(client)
        frame = (await client.call_tool("openlolo_screenshot", {})).structured_content["metadata"]
        task = asyncio.create_task(
            client.call_tool(
                "openlolo_long_press",
                {
                    "operation_id": str(uuid4()),
                    "frame_id": frame["frame_id"],
                    "geometry_epoch": frame["geometry_epoch"],
                    "x": 0.5,
                    "y": 0.5,
                    "seconds": 2,
                },
            )
        )
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if runtime.coordinator.paused:
                break
            await asyncio.sleep(0.01)
        assert runtime.coordinator.paused and runtime.phone.device.generation is None


async def test_mcp_full_page_returns_one_image_per_page(runtime):
    bridge = await bridge_for(runtime)
    async with Client(create_server(bridge)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "openlolo_full_page" in names
        result = await client.call_tool("openlolo_full_page", {"max_pages": 8, "settle": 0, "output": "both"})
        assert not result.is_error, result
        data = result.structured_content
        assert data["operation"]["state"] == "SUCCEEDED" and data["stopped"] == "end"
        images = [b for b in result.content if b.type == "image"]
        assert len(images) == data["page_count"] + 1 >= 4
        assert base64.b64decode(images[0].data).startswith(b"\xff\xd8")
        assert '"image":' not in json.dumps(data)
        assert data["stitched"]["height"] > data["pages"][0]["metadata"]["height"]
        # The current frame is valid for the next touch without a new screenshot.
        current = data["current"]
        tap = await client.call_tool(
            "openlolo_tap",
            {
                "frame_id": current["frame_id"],
                "geometry_epoch": current["geometry_epoch"],
                "x": 0.5,
                "y": 0.5,
            },
        )
        assert not tap.is_error, tap
        runtime.phone.device.scroll = 0  # back to the top; the screen stayed at the end
        pages_only = await client.call_tool("openlolo_full_page", {"max_pages": 2, "settle": 0})
        assert not pages_only.is_error
        assert len([b for b in pages_only.content if b.type == "image"]) == 2
        assert pages_only.structured_content["stopped"] == "pages"
        assert pages_only.structured_content["stitched"] is None
