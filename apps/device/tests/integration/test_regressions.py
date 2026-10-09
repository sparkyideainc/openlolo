import asyncio
from uuid import uuid4

import pytest

from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Action, CaptureOptions


async def test_duplicate_pointer_returns_record_after_new_frame(runtime, lease):
    frame = await runtime.phone.screenshot()
    op = str(uuid4())
    action = Action(
        kind="tap",
        x=0.25,
        y=0.75,
        frame_id=frame.metadata.frame_id,
        geometry_epoch=frame.metadata.geometry_epoch,
    )
    runtime.phone.action(op, "owner", lease, action)
    await runtime.phone.screenshot()
    result = runtime.phone.action(op, "owner", lease, action)
    assert result["state"] == "SUCCEEDED"
    presses = [v for k, v in runtime.phone.device.reports if k == "touch" and v[0] == "contact"]
    assert len(presses) == 1


async def test_capture_resize_and_jpeg_preserve_ratio(runtime):
    full = await runtime.phone.screenshot()
    small = await runtime.phone.screenshot(CaptureOptions(max_size=400, format="jpeg"))
    assert (small.metadata.width, small.metadata.height) == (185, 400)
    assert small.image.startswith(b"\xff\xd8")
    assert full.metadata.geometry_epoch == small.metadata.geometry_epoch
    assert full.metadata.stream_epoch != small.metadata.stream_epoch


async def test_expired_lease_cannot_replay_or_resume(runtime, lease):
    runtime.coordinator.lease.expires = 0
    with pytest.raises(OpenLoloError):
        runtime.phone.resume("owner", lease)
    with pytest.raises(OpenLoloError):
        runtime.phone.action(str(uuid4()), "owner", lease, Action(kind="key", key="enter"))
    new = (await runtime.phone.acquire_control("owner"))["lease_token"]
    assert new != lease


async def test_cancel_does_not_cancel_next_observation(runtime, lease):
    op = str(uuid4())
    runtime.phone.action(op, "owner", lease, Action(kind="type_text", text="abcdefghi"))
    await asyncio.sleep(0.02)
    observation = asyncio.create_task(runtime.phone.screenshot())
    await runtime.phone.cancel(op, "owner")
    assert (await observation).image
    assert runtime.phone.operation_status(op, "owner")["state"] == "OUTCOME_UNKNOWN"


async def test_single_api_process_lock(runtime):
    from openlolo.bootstrap import Runtime

    another = Runtime(runtime.config)
    try:
        with pytest.raises(RuntimeError, match="Another OpenLolo"):
            await another.start()
    finally:
        another.journal.close()


async def test_shortcuts_disabled_until_device_validation(runtime, lease):
    assert not runtime.phone.capabilities()["shortcuts"]
    with pytest.raises(OpenLoloError):
        runtime.phone.shortcut(str(uuid4()), "owner", lease, "home")
    runtime.calibration.validate_shortcut(
        runtime.phone.binding(),
        {
            "name": "home",
            "owner_confirmed": True,
            "evidence": "Synthetic test: home screen observed",
            "prerequisites": "U.S. keyboard; simulator only",
        },
    )
    assert runtime.phone.capabilities()["shortcuts"] == ["home"]


async def test_recovery_reports_failure_without_replaying_input(runtime):
    from openlolo.application.recovery import recover

    async def fail(binding):
        raise OpenLoloError("DEVICE_DISCONNECTED")

    original = runtime.phone.device.recover
    runtime.phone.device.recover = fail
    result = await recover({"device": runtime.phone.device}, runtime.phone.binding())
    assert result["device"]["error"]["code"] == "DEVICE_DISCONNECTED"
    runtime.phone.device.recover = original
    before = runtime.phone.device.releases
    result = await recover({"device": runtime.phone.device}, runtime.phone.binding())
    assert result["device"]["state"] == "ready" and runtime.phone.device.releases > before


async def settle(runtime, op):
    async with asyncio.timeout(3):
        while runtime.phone.operation_status(op, "owner")["state"] in {"QUEUED", "DISPATCHED"}:
            await asyncio.sleep(0.005)
    return runtime.phone.operation_status(op, "owner")


async def test_transport_switch_rebinds_and_invalidates_frame(runtime, lease):
    await runtime.phone.screenshot()
    op = str(uuid4())
    runtime.phone.setup(op, "owner", lease, "transport", {"transport": "wifi"})
    record = await settle(runtime, op)
    assert record["state"] == "SUCCEEDED" and record["result"]["bound"]
    assert runtime.phone.binding().transport == "wifi" and runtime.phone.frame is None
    status = await runtime.phone.status()
    assert status["transport"] == "wifi" and status["connection"]["transport"] == "wifi"
    bad = str(uuid4())
    runtime.phone.setup(bad, "owner", lease, "transport", {"transport": "serial"})
    assert (await settle(runtime, bad))["result"]["code"] == "INVALID_REQUEST"
