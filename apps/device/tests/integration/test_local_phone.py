import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from openlolo.bootstrap import Runtime
from openlolo.config import Config
from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.http_api import create_app
from openlolo.interfaces.local_phone import BridgeError, LocalPhone
from openlolo.interfaces.mcp_client import PhoneClient


def local(runtime, **kwargs):
    return LocalPhone(runtime.phone, owner=f"client:{uuid4()}", **kwargs)


async def frame_of(bridge):
    return (await bridge.screenshot())["metadata"]


async def test_errors_map_to_codes_and_dispatch_state(runtime, monkeypatch):
    bridge = local(runtime)
    # Invalid arguments: refused before anything was recorded, no input echoed.
    with pytest.raises(BridgeError) as invalid:
        await bridge.action(str(uuid4()), {"kind": "INVALID", "text": "private text"}, False)
    assert invalid.value.code == "INVALID_REQUEST" and invalid.value.public()["dispatched"] is False
    assert "private" not in json.dumps(invalid.value.public())
    # A 4xx state error from the core is a refusal too.
    with pytest.raises(BridgeError) as conflict:
        op = str(uuid4())
        await bridge.action(op, {"kind": "key", "key": "enter"}, False)
        await bridge.action(op, {"kind": "key", "key": "tab"}, False)
    assert conflict.value.code == "OPERATION_CONFLICT" and conflict.value.rejected
    # Anything else is not a refusal: the caller is told to inspect the operation, not replay it.
    op = str(uuid4())

    def broken(*args):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(runtime.phone, "action", broken)
    with pytest.raises(BridgeError) as failed:
        await bridge.action(op, {"kind": "key", "key": "enter"}, False)
    public = failed.value.public()
    assert failed.value.code == "BACKEND_FAILED" and "dispatched" not in public
    assert public["operation_id"] == op and "secret" not in json.dumps(public)
    monkeypatch.setattr(
        runtime.phone, "action", lambda *a: (_ for _ in ()).throw(OpenLoloError("DOWN", status=503))
    )
    with pytest.raises(BridgeError) as down:
        await bridge.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert down.value.status == 503 and not down.value.rejected

    # Reads map the same way and never carry an operation id.
    async def untrusted(binding):
        raise OpenLoloError("TRUST_REQUIRED")

    monkeypatch.setattr(runtime.phone.device, "device_info", untrusted)
    with pytest.raises(BridgeError) as read:
        await bridge.device_info()
    assert read.value.code == "TRUST_REQUIRED" and read.value.public() == {"code": "TRUST_REQUIRED"}
    await bridge.close()


async def test_a_closed_bridge_refuses_every_call(runtime):
    bridge = local(runtime)
    await bridge.ensure_control()
    await bridge.close()
    assert runtime.coordinator.lease is None and bridge.lease is None
    with pytest.raises(BridgeError) as status:
        await bridge.status()
    assert status.value.code == "LOGIN_REQUIRED"
    with pytest.raises(BridgeError) as action:
        await bridge.action(str(uuid4()), {"kind": "key", "key": "enter"}, False)
    assert action.value.code == "LOGIN_REQUIRED" and action.value.public()["dispatched"] is False


async def test_dropping_the_bridge_mid_action_reports_the_true_outcome(runtime):
    bridge = local(runtime)
    await bridge.ensure_control()
    frame = await frame_of(bridge)
    op = str(uuid4())
    task = asyncio.create_task(
        bridge.action(
            op,
            {
                "kind": "long_press",
                "frame_id": frame["frame_id"],
                "geometry_epoch": frame["geometry_epoch"],
                "x": 0.5,
                "y": 0.5,
                "seconds": 2,
            },
            True,
        )
    )
    await asyncio.sleep(0.25)
    await bridge.close()  # what the gateway's sweep does on revocation
    result = await asyncio.wait_for(task, 1)
    assert result["operation"]["state"] == "OUTCOME_UNKNOWN" and "observation" not in result
    assert runtime.phone.device.generation is None and runtime.coordinator.lease is None


async def test_one_operation_id_sent_twice_at_once_dispatches_once(runtime):
    bridge = local(runtime)
    await bridge.ensure_control()
    before = len(runtime.phone.device.reports)
    op = str(uuid4())
    first, second = await asyncio.gather(
        bridge.action(op, {"kind": "key", "key": "enter"}, False),
        bridge.action(op, {"kind": "key", "key": "enter"}, False),
    )
    assert first["operation"]["state"] == second["operation"]["state"] == "SUCCEEDED"
    presses = [v for k, v in list(runtime.phone.device.reports)[before:] if k == "keyboard" and 40 in v]
    assert len(presses) == 1
    await bridge.close()


async def test_results_are_copies_of_what_the_box_keeps(runtime):
    bridge = local(runtime)
    data = await bridge.full_page(str(uuid4()), {"max_pages": 3, "settle": 0})
    assert data["operation"]["state"] == "SUCCEEDED"
    for page in data["pages"]:
        page.pop("image")  # what the MCP layer does with every image
    [kept] = runtime.phone.full_pages.values()
    assert all("image" in page for page in kept["pages"])
    await bridge.close()


async def test_outcome_waits_are_not_polled(runtime):
    """A tap's wait ends when the coordinator finishes it, not at the next 100 ms poll."""
    bridge = local(runtime)
    runtime.phone.device.delay = 0
    await bridge.ensure_control()
    frame = await frame_of(bridge)
    tap = {
        "kind": "tap",
        "frame_id": frame["frame_id"],
        "geometry_epoch": frame["geometry_epoch"],
        "x": 0.5,
        "y": 0.5,
    }
    result = await bridge.action(str(uuid4()), tap, False)
    # 130 ms of HID holds inside; the HTTP bridge's poll made this about 200 ms.
    assert result["operation"]["state"] == "SUCCEEDED" and result["timing"]["action_seconds"] < 0.18
    await bridge.close()


def runtime_at(path):
    return Runtime(
        Config(
            backend="simulated",
            state_dir=path / "state",
            runtime_dir=path / "run",
            allowed_hosts=["testserver"],
        )
    )


async def scenario(bridge):
    await bridge.control("acquire", str(uuid4()))
    frame = await frame_of(bridge)
    tap = {
        "kind": "tap",
        "frame_id": frame["frame_id"],
        "geometry_epoch": frame["geometry_epoch"],
        "x": 0.5,
        "y": 0.5,
    }
    op = str(uuid4())
    await bridge.action(op, tap, False)
    await bridge.action(op, tap, False)
    with pytest.raises(BridgeError):
        await bridge.action(op, {**tap, "x": 0.7}, False)
    await bridge.action(str(uuid4()), {"kind": "send_text", "text": "héllo"}, False)
    await bridge.action(str(uuid4()), {"kind": "launch_app", "bundle_id": "com.apple.Preferences"}, False)
    await bridge.link(str(uuid4()), "com.apple.mobilesafari", "page", {"url": "https://example.org/"}, False)
    with pytest.raises(BridgeError):
        await bridge.link(str(uuid4()), "net.whatsapp.WhatsApp", "chat_with_text", {"phone": "1"}, False)
    await bridge.full_page(str(uuid4()), {"max_pages": 2, "settle": 0})
    await bridge.control("pause", str(uuid4()))
    await bridge.control("resume", str(uuid4()))
    await bridge.control("renew", str(uuid4()))
    await bridge.control("release", str(uuid4()))


def rows(runtime):
    volatile = {
        "process",
        "seconds",
        "remaining_seconds",
        "stream_epoch",
        "received_at",
        "request_seconds",
        "cadence",
        "lags",
        "since",
        "change_count",
        "clipboard",
        "notes",
    }

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in volatile and "frame_id" not in k}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    found = runtime.journal.db.execute(
        "SELECT kind, state, result FROM operations ORDER BY created"
    ).fetchall()
    return [(r["kind"], r["state"], clean(json.loads(r["result"]) if r["result"] else None)) for r in found]


async def test_the_journal_records_the_same_rows_through_either_bridge(tmp_path):
    results = []
    for name in ("http", "local"):
        runtime = runtime_at(tmp_path / name)
        await runtime.start()
        if name == "http":
            session = runtime.auth.open_session()
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(runtime)),
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {session}"},
            )
            bridge = PhoneClient(http)
        else:
            bridge = local(runtime)
        await scenario(bridge)
        await bridge.close()
        results.append(rows(runtime))
        await runtime.close()
    http_rows, local_rows = results
    assert len(local_rows) == 10
    assert http_rows == local_rows
