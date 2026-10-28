"""The script SDK against the real LAN gateway over TLS: approval, pinning, actions, revocation."""

import asyncio
import json
import socket
import stat

import pytest
import uvicorn
from openlolo_sdk import ApprovalRequired, FrameExpired, Phone, PinMismatch
from support import LAN_CERT, LAN_KEY

from openlolo.bootstrap import Runtime
from openlolo.config import Config
from openlolo.interfaces import device_grant, tls
from openlolo.interfaces.cli import SecondaryServer
from openlolo.interfaces.mcp_http import create_gateway_app, grant_from_choice


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Counter:
    """Counts HTTP requests that reached the app, by path."""

    def __init__(self, app):
        self.app, self.paths = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.paths.append(scope["path"])
        await self.app(scope, receive, send)


@pytest.fixture
async def box(tmp_path, monkeypatch):
    monkeypatch.setattr(device_grant, "INTERVAL", 0.2)
    port = free_port()
    url = f"https://127.0.0.1:{port}"
    config = Config(
        backend="simulated",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        allowed_hosts=["testserver"],
        lan_url=url,
        lan_cert=LAN_CERT,
        lan_key=LAN_KEY,
        setup_name="OpenLolo-TEST",
    )
    runtime = Runtime(config)
    await runtime.start()
    counter = Counter(create_gateway_app(runtime, url, allowed_hosts=config.lan_hosts[1:]))
    server = SecondaryServer(
        uvicorn.Config(
            counter,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(LAN_CERT),
            ssl_keyfile=str(LAN_KEY),
            access_log=False,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    try:
        yield runtime, url, counter
    finally:
        server.should_exit = True
        await task
        await runtime.close()


async def approver(runtime, window="never"):
    """The owner tapping Approve on the box page for every script request."""
    provider = runtime.door_provider
    while True:
        for row in provider.list_pending():
            granted, seconds = grant_from_choice(row["scopes"], window)
            provider.decide_handle(row["handle"], granted, seconds)
        await asyncio.sleep(0.05)


def phone(url, cache, **options):
    options.setdefault("allow_approval", True)
    return Phone(url, cache_dir=cache, name="e2e.py", notify=lambda text: None, approve_timeout=20, **options)


async def test_script_approved_once_then_acts_and_loses_access_on_removal(box, tmp_path):
    runtime, url, counter = box
    cache = tmp_path / "sdk"
    owner = asyncio.create_task(approver(runtime))
    try:

        def first_run():
            with phone(url, cache) as p:
                p.open_app("com.apple.mobilenotes").type("hello").tap(0.5, 0.5).swipe(
                    0.5, 0.8, 0.5, 0.2, hold=0.2
                ).home()
                return p.frame, p.auth.data["client_id"]

        frame, client_id = await asyncio.to_thread(first_run)
    finally:
        owner.cancel()
    assert frame is not None and frame.image
    assert ("launch_app", "com.apple.mobilenotes") in runtime.phone.device.reports
    assert counter.paths.count("/oauth/device_authorization") == 1

    (cached,) = cache.glob("*.json")
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600
    data = json.loads(cached.read_text())
    assert data["fingerprint"] == tls.fingerprint(LAN_CERT)
    (client,) = [
        c for c in runtime.door_provider.list_clients(every_issuer=True) if c["client_id"] == client_id
    ]
    assert client["never_expires"] and client["client_name"] == "e2e.py"

    # Second run: no approval, no device request; a stale frame is refused, never re-tapped.
    def second_run():
        with phone(url, cache, allow_approval=False) as p:
            p.screenshot()
            runtime.phone.frame_time -= 1000
            before = len(runtime.phone.device.reports)
            with pytest.raises(FrameExpired) as info:
                p.tap(0.5, 0.5)
            assert info.value.dispatched is False
            return before, len(runtime.phone.device.reports)

    before, after = await asyncio.to_thread(second_run)
    assert after == before
    assert counter.paths.count("/oauth/device_authorization") == 1

    # The owner removes the script: a headless run fails fast instead of waiting for approval.
    assert runtime.door_provider.revoke_client(client_id)

    def after_removal():
        with phone(url, cache, allow_approval=False) as p:
            p.status()

    with pytest.raises(ApprovalRequired):
        await asyncio.to_thread(after_removal)


async def test_pinned_certificate_mismatch_sends_nothing(box, tmp_path):
    runtime, url, counter = box
    cache = tmp_path / "sdk"
    owner = asyncio.create_task(approver(runtime))

    def first_run():
        with phone(url, cache) as p:
            p.connect()

    try:
        await asyncio.to_thread(first_run)
    finally:
        owner.cancel()
    (cached,) = cache.glob("*.json")
    data = json.loads(cached.read_text())
    cached.write_text(json.dumps({**data, "fingerprint": "00" * 32}))
    seen = len(counter.paths)

    def run():
        with phone(url, cache, allow_approval=False) as p:
            p.status()

    with pytest.raises(PinMismatch):
        await asyncio.to_thread(run)
    assert len(counter.paths) == seen  # Not one request reached the box.

    # An explicit fingerprint wins over the cache.
    def explicit():
        with phone(url, cache, allow_approval=False, fingerprint=tls.fingerprint(LAN_CERT)) as p:
            return p.status()

    assert "device" in await asyncio.to_thread(explicit)
