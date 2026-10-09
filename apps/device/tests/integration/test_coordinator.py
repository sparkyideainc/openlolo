import asyncio
from uuid import uuid4

import pytest

from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Action


async def terminal(runtime, op):
    async with asyncio.timeout(3):
        while (result := runtime.phone.operation_status(op, "owner"))["state"] in {"QUEUED", "DISPATCHED"}:
            await asyncio.sleep(0.005)
    return result


async def test_duplicate_once_and_observation_order(runtime, lease):
    op = str(uuid4())
    action = Action(kind="key", key="enter")
    runtime.phone.action(op, "owner", lease, action)
    assert runtime.phone.action(op, "owner", lease, action)["id"] == op
    frame = await runtime.phone.screenshot()
    assert (await terminal(runtime, op))["state"] == "SUCCEEDED"
    reports = [r for kind, r in runtime.phone.device.reports if kind == "keyboard" and 40 in r]
    assert len(reports) == 1 and frame.metadata.frame_id


async def test_pause_bypasses_blocked_capture(runtime, lease):
    runtime.phone.device.delay = 10
    capture = asyncio.create_task(runtime.phone.screenshot())
    await asyncio.sleep(0.02)
    op = str(uuid4())
    runtime.phone.action(op, "owner", lease, Action(kind="key", key="enter"))
    async with asyncio.timeout(0.2):
        await runtime.phone.pause()
    assert runtime.phone.operation_status(op, "owner")["state"] == "CANCELLED"
    assert runtime.phone.device.generation is None
    capture.cancel()


async def test_lease_expiry_drops_queued_work(runtime, lease):
    runtime.phone.device.delay = 0.3
    screenshot = asyncio.create_task(runtime.phone.screenshot())
    await asyncio.sleep(0.01)
    op = str(uuid4())
    runtime.phone.action(op, "owner", lease, Action(kind="key", key="enter"))
    runtime.coordinator.lease.expires = 0
    await screenshot
    assert (await terminal(runtime, op))["state"] == "CANCELLED"
    assert not any(kind == "keyboard" and 40 in value for kind, value in runtime.phone.device.reports)


async def test_cancel_dispatched_is_unknown(runtime, lease):
    op = str(uuid4())
    runtime.phone.action(op, "owner", lease, Action(kind="type_text", text="lots of characters"))
    await asyncio.sleep(0.07)
    await runtime.phone.cancel(op, "owner")
    assert (await terminal(runtime, op))["state"] == "OUTCOME_UNKNOWN"
    assert runtime.phone.device.generation is None


async def test_capture_failure_invalidates_pointer_frame(runtime, lease):
    frame = await runtime.phone.screenshot()
    runtime.phone.device.failure = OpenLoloError("TRUST_REQUIRED")
    with pytest.raises(OpenLoloError):
        await runtime.phone.screenshot()
    with pytest.raises(OpenLoloError, match="screenshot"):
        runtime.phone.action(
            str(uuid4()),
            "owner",
            lease,
            Action(
                kind="tap",
                x=0.5,
                y=0.5,
                frame_id=frame.metadata.frame_id,
                geometry_epoch=frame.metadata.geometry_epoch,
            ),
        )


async def test_device_failure_reported_independently_of_network(runtime):
    runtime.phone.device.failure = OpenLoloError("DEVICE_DISCONNECTED")
    status = await runtime.phone.status()
    assert status["components"]["device"]["state"] == "unavailable"
    assert status["connection"]["state"] == "connected" and status["transport"] == "usb"
    assert "network" in status["components"]


async def test_queue_bounded(runtime, lease):
    runtime.phone.device.delay = 0.3
    task = asyncio.create_task(runtime.phone.screenshot())
    await asyncio.sleep(0.01)
    results = [
        runtime.phone.action(str(uuid4()), "owner", lease, Action(kind="key", key="enter")) for _ in range(20)
    ]
    assert sum(r["state"] == "FAILED" for r in results) == 4
    await runtime.phone.pause()
    await task


async def test_launch_app_foregrounds_without_hid(runtime, lease):
    op = str(uuid4())
    before = len(runtime.phone.device.reports)
    runtime.phone.action(op, "owner", lease, Action(kind="launch_app", bundle_id="com.apple.mobilenotes"))
    result = await terminal(runtime, op)
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["process"]["bundle_id"] == "com.apple.mobilenotes"
    assert list(runtime.phone.device.reports)[before:] == [("launch_app", "com.apple.mobilenotes")]
    assert runtime.phone.device.generation is None  # no HID generation was opened

    missing = str(uuid4())
    runtime.phone.action(missing, "owner", lease, Action(kind="launch_app", bundle_id="com.example.absent"))
    failed = await terminal(runtime, missing)
    assert failed["state"] == "OUTCOME_UNKNOWN" and failed["result"]["code"] == "APP_NOT_FOUND"

    with pytest.raises(ValueError):
        Action(kind="launch_app", bundle_id="notes")
    assert "launch_app" in runtime.phone.capabilities()["actions"]


async def test_send_text_sets_clipboard_then_pastes(runtime, lease):
    op = str(uuid4())
    before = len(runtime.phone.device.reports)
    text = "Pasteboard test 中文 🚀\nline2"
    runtime.phone.action(op, "owner", lease, Action(kind="send_text", text=text))
    result = await terminal(runtime, op)
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["clipboard_replaced"] is True and result["result"]["characters"] == len(text)
    new = list(runtime.phone.device.reports)[before:]
    assert new[0] == ("clipboard_set", len(text))
    assert ("keyboard", [0xE3, 0x19]) in new  # Cmd held, then V
    assert (await runtime.phone.clipboard())["text"] == text

    with pytest.raises(ValueError):
        Action(kind="send_text", text="")
    with pytest.raises(OpenLoloError, match="ASCII"):
        runtime.phone.action(str(uuid4()), "owner", lease, Action(kind="type_text", text="x" * 201))


async def test_open_url_routes_web_to_safari_and_schemes_to_apps(runtime, lease):
    op = str(uuid4())
    runtime.phone.action(op, "owner", lease, Action(kind="open_url", url="https://example.net/"))
    assert (await terminal(runtime, op))["state"] == "SUCCEEDED"
    assert (
        "open_url",
        "com.apple.mobilesafari",
        "https://example.net/",
        False,
    ) in runtime.phone.device.reports

    op = str(uuid4())
    runtime.phone.action(
        op,
        "owner",
        lease,
        Action(kind="open_url", url="notes://show?id=1", bundle_id="com.apple.mobilenotes"),
    )
    assert (await terminal(runtime, op))["state"] == "SUCCEEDED"
    assert ("open_url", "com.apple.mobilenotes", "notes://show?id=1", True) in runtime.phone.device.reports

    for bad in ("instagram://user?username=x", "example.net", "https://a b"):
        with pytest.raises(ValueError):
            Action(kind="open_url", url=bad)


async def test_open_link_resolves_profile_and_opens_url(runtime, lease):
    op = str(uuid4())
    runtime.phone.open_link(
        op, "owner", lease, "com.apple.mobilesafari", "page", {"url": "https://example.net/"}
    )
    assert (await terminal(runtime, op))["state"] == "SUCCEEDED"
    assert (
        "open_url",
        "com.apple.mobilesafari",
        "https://example.net/",
        False,
    ) in runtime.phone.device.reports
    with pytest.raises(OpenLoloError, match="No app profile"):
        runtime.phone.open_link(str(uuid4()), "owner", lease, "com.example.none", "page", {})
    apps = await runtime.phone.apps()
    assert {a["bundle_id"]: a["has_profile"] for a in apps}["com.apple.mobilesafari"] is True
    assert (
        runtime.phone.app_profile("com.burbn.instagram")["workflows"]["send_dm"]["steps"][0]["kind"] == "open"
    )
