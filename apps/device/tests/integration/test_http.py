import asyncio
import base64
import json
from uuid import uuid4

import httpx

from openlolo.interfaces.http_api import calibration_app, create_app


async def client(runtime):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, False)),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    )


async def login(c, runtime):
    credential = runtime.auth.issue()
    r = await c.post("/api/login", json={"credential": credential})
    assert r.status_code == 200
    return credential


async def test_auth_origin_host_cookie_and_single_use(runtime):
    async with await client(runtime) as c:
        assert (await c.get("/api/status")).status_code == 401
        credential = await login(c, runtime)
        assert (await c.get("/api/status")).status_code == 200
        assert (await c.post("/api/login", json={"credential": credential})).status_code == 401
        assert (
            await c.post("/api/screenshot", json={}, headers={"Origin": "http://evil.test"})
        ).status_code == 403
        assert (await c.get("/api/status", headers={"Host": "evil.test"})).status_code == 403
        assert (
            await c.post("/api/screenshot", content="{}", headers={"Content-Type": "text/plain"})
        ).status_code == 415
        assert (await c.get("/")).headers["cache-control"] == "no-store"
        assert (await c.post("/api/logout", json={})).status_code == 200
        assert (await c.get("/api/status")).status_code == 401


async def test_screenshot_and_control_flow(runtime):
    async with await client(runtime) as c:
        credential = runtime.auth.issue()
        r = await c.post("/api/login", json={"credential": credential})
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        op = str(uuid4())
        acquire = (await c.post("/api/control/acquire", json={"operation_id": op})).json()
        token = acquire["control"]["lease_token"]
        duplicate = (await c.post("/api/control/acquire", json={"operation_id": op})).json()
        assert duplicate["control"]["lease_token"] == token
        screenshot = (await c.post("/api/screenshot", json={})).json()
        assert base64.b64decode(screenshot["image"]).startswith(b"\xff\xd8")
        assert screenshot["metadata"]["source_age_seconds"] is None
        status = (await c.get("/api/status")).json()
        assert status["connection"]["state"] == "connected" and status["transport"] == "usb"
        assert (await c.get("/api/apps")).json()["apps"][0]["bundle_id"] == "com.apple.Preferences"
        assert (await c.get("/api/device")).json()["product_type"] == "Simulator"
        assert (
            await c.post(
                "/api/actions",
                json={
                    "operation_id": str(uuid4()),
                    "lease_token": token,
                    "action": {"kind": "type_text", "text": "secret\ninvalid"},
                },
            )
        ).status_code == 400
        assert "secret" not in (await c.post("/api/actions", json={"action": {"kind": "INVALID"}})).text


async def test_calibration_listener_exposes_no_control(runtime):
    runtime.config.calibration_host = "192.168.1.2"
    session = runtime.calibration.start(
        runtime.phone.binding(), {"native_width": 390, "native_height": 844, "viewport_top": 130}
    )
    token = session["session"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=calibration_app(runtime)), base_url="http://192.168.1.2:8081"
    ) as c:
        assert (await c.get("/api/status")).status_code == 404
        assert (await c.get(f"/{token}/")).status_code == 200
        assert (await c.get("/wrong/")).status_code == 404
        assert (
            await c.post(
                f"/{token}/events", json={"type": "geometry", "width": 390, "height": 616, "scale": 1}
            )
        ).status_code == 403
        assert (
            await c.post(
                f"/{token}/events",
                headers={"Origin": "http://192.168.1.2:8081"},
                json={"type": "geometry", "width": 390, "height": 616, "scale": 1},
            )
        ).status_code == 204


async def wait_for(c, op, seconds=2.0):
    for _ in range(int(seconds / 0.01)):
        record = (await c.get(f"/api/operations/{op}")).json()
        if record["state"] not in {"QUEUED", "DISPATCHED"}:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"operation {op} still {record['state']}")


async def test_discover_and_onboarding_setup_kinds(runtime):
    async with await client(runtime) as c:
        assert (await c.get("/api/discover")).status_code == 401
        await login(c, runtime)
        discovered = (await c.get("/api/discover")).json()
        assert discovered["usb"] == [{"udid": "simulated-phone", "connection": "USB"}]
        assert discovered["wifi_paired"] == [] and discovered["simulated"] is True

        acquire = (await c.post("/api/control/acquire", json={"operation_id": str(uuid4())})).json()
        token = acquire["control"]["lease_token"]

        def setup(kind, payload):
            return c.post(
                f"/api/setup/{kind}",
                json={"operation_id": str(uuid4()), "lease_token": token, "payload": payload},
            )

        for kind in ("pair", "developer_mode"):
            for payload in (
                {"udid": "simulated-phone"},
                {"udid": "simulated-phone", "owner_confirmed": "yes"},
                {},
            ):
                r = await setup(kind, payload)
                assert r.status_code == 400, (kind, payload, r.text)
                assert r.json()["error"]["code"] == "OWNER_CONFIRMATION_REQUIRED"
                assert "usb" not in r.text  # Nothing queued or journalled, no device call leaked.

        r = await setup("pair", {"udid": "simulated-phone", "owner_confirmed": True})
        assert r.status_code == 202 and r.json()["state"] == "QUEUED"
        record = await wait_for(c, r.json()["id"])
        assert record["state"] == "SUCCEEDED"
        assert record["result"] == {
            "trusted": True,
            "wifi_pairing": True,
            "udid": "simulated-phone",
            "simulated": True,
        }

        r = await setup("developer_mode", {"udid": "simulated-phone", "owner_confirmed": True})
        assert r.status_code == 202
        assert (await wait_for(c, r.json()["id"]))["result"]["developer_mode"] is True

        r = await setup("mount_ddi", {})
        assert r.status_code == 202
        assert (await wait_for(c, r.json()["id"]))["result"]["mounted"] is True

        # Owner sessions opened through the browser are labelled for the journal.
        assert runtime.journal.list_operations(limit=1)[0]["client"] == "browser"


async def test_full_page_scrolls_joins_and_serves_images_once(runtime):
    async with await client(runtime) as c:
        await login(c, runtime)
        acquire = (await c.post("/api/control/acquire", json={"operation_id": str(uuid4())})).json()
        token = acquire["control"]["lease_token"]
        op = str(uuid4())
        body = {"operation_id": op, "lease_token": token, "options": {"max_pages": 8, "settle": 0}}
        # Images are not ready while the capture runs.
        queued = await c.post("/api/full_page", json=body)
        assert queued.status_code == 202
        pending = await c.get(f"/api/full_page/{op}")
        assert pending.status_code == 409 and pending.json()["error"]["code"] == "OPERATION_PENDING"
        # Each page is a 0.8 s drag, a 0.3 s rest and a capture; eight pages take a while.
        record = await wait_for(c, op, seconds=30)
        assert record["state"] == "SUCCEEDED", record
        summary = record["result"]
        # The simulator scrolls 1200 points of content: several pages, then the end.
        assert summary["stopped"] == "end" and 3 <= summary["page_count"] <= 8
        assert '"image":' not in json.dumps(summary)
        assert all(page["matched"] for page in summary["pages"])
        # The pages cover the scrollable content. The sum can overshoot: the simulator's sparse
        # circles leave blank overlaps where the matcher settles on the flick prior, and the
        # last flick is clamped at the end of the content. The join is not pixel perfect.
        total = sum(page["shift"] for page in summary["pages"])
        assert runtime.phone.device.SCROLL_MAX - 40 <= total <= runtime.phone.device.SCROLL_MAX + 300
        assert summary["current"]["frame_id"] == runtime.phone.frame.frame_id
        # The identical request replays the record without touching the phone.
        swipes = len([r for r in runtime.phone.device.reports if r[0] == "touch" and r[1][0] == "release"])
        replay = await c.post("/api/full_page", json=body)
        assert replay.status_code == 200 and replay.json()["id"] == op
        assert swipes == len(
            [r for r in runtime.phone.device.reports if r[0] == "touch" and r[1][0] == "release"]
        )
        images = (await c.get(f"/api/full_page/{op}")).json()
        assert len(images["pages"]) == summary["page_count"]
        assert base64.b64decode(images["pages"][0]["image"]).startswith(b"\xff\xd8")
        stitched = images["stitched"]
        assert stitched["width"] == summary["pages"][0]["metadata"]["width"]
        assert (
            844 + runtime.phone.device.SCROLL_MAX - 40
            <= stitched["height"]
            <= 844 + runtime.phone.device.SCROLL_MAX + 300
        )
        # The last page is where the screen stays, so its frame is valid for the next touch.
        current = summary["current"]
        tap = await c.post(
            "/api/actions",
            json={
                "operation_id": str(uuid4()),
                "lease_token": token,
                "action": {
                    "kind": "tap",
                    "x": 0.5,
                    "y": 0.5,
                    "frame_id": current["frame_id"],
                    "geometry_epoch": current["geometry_epoch"],
                },
            },
        )
        assert tap.status_code in {200, 202}
        # Without the lease token nothing runs.
        refused = await c.post("/api/full_page", json={"operation_id": str(uuid4()), "options": {}})
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "CONTROL_NOT_OWNED"
        # Another session cannot read the images.
        async with await client(runtime) as other:
            await login(other, runtime)
            assert (await other.get(f"/api/full_page/{op}")).status_code == 404
