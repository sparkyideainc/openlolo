"""LAN entry: host pinning, issuer isolation from the remote entry, real TLS."""

import asyncio
import hashlib
import ssl
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from support import (
    LAN,
    LAN_CERT,
    LAN_KEY,
    PUBLIC,
    lan_config,
    register,
    running,
    start_authorization,
    tokens,
    web,
)

from openlolo.bootstrap import Runtime
from openlolo.interfaces import tls
from openlolo.interfaces.cli import SecondaryServer
from openlolo.interfaces.mcp_http import create_gateway_app

PUBLIC_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
async def entries(tmp_path):
    config = lan_config(tmp_path)
    runtime = Runtime(config)
    await runtime.start()
    public = create_gateway_app(runtime)
    lan = create_gateway_app(runtime, config.lan_url, allowed_hosts=config.lan_hosts[1:], app_routes=True)
    async with running(public), running(lan):
        yield runtime, public, lan
    await runtime.close()


async def test_lan_listener_pins_every_configured_host(entries):
    runtime, _, lan = entries
    assert runtime.config.lan_hosts == ["openlolo-test.local:8443", "192.168.4.25:8443"]
    async with web(lan, LAN) as c:
        for host in runtime.config.lan_hosts:
            r = await c.get("/.well-known/oauth-authorization-server", headers={"Host": host})
            assert r.status_code == 200, (host, r.text)
            assert r.json()["issuer"] == LAN and r.json()["registration_endpoint"] == LAN + "/register"
        prm = await c.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": "192.168.4.25:8443"})
        assert prm.json()["authorization_servers"] == [LAN]
        for host in ("evil.test", "pi.tailnet.ts.net", "openlolo-test.local", "openlolo-test.local:8444"):
            r = await c.get("/.well-known/oauth-authorization-server", headers={"Host": host})
            assert r.status_code == 403 and r.json()["error"]["code"] == "HOST_REJECTED", host


async def test_issuers_do_not_accept_each_others_grants(entries):
    runtime, public, lan = entries
    async with web(public, PUBLIC) as pc, web(lan, LAN) as lc:
        # Approve a browser client on the remote entry with the HTML consent form.
        registered = await register(
            pc, callback=PUBLIC_CALLBACK, scope="phone:read phone:control", name="Claude"
        )
        assert registered.status_code == 201, registered.text
        public_client = registered.json()["client_id"]
        started, request_id, verifier = await start_authorization(
            pc, PUBLIC, public_client, callback=PUBLIC_CALLBACK, scope="phone:read"
        )
        assert started.status_code == 302 and request_id
        form = {"request": request_id, "credential": runtime.auth.issue(), "decision": "allow"}
        consented = await pc.post("/oauth/consent", data=form, headers={"Origin": PUBLIC})
        assert consented.status_code == 302, consented.text
        code = parse_qs(urlsplit(consented.headers["location"]).query)["code"][0]
        issued = await tokens(pc, PUBLIC, public_client, code, verifier, callback=PUBLIC_CALLBACK)
        assert issued.status_code == 200, issued.text
        access = issued.json()["access_token"]

        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        accepted = await pc.post("/mcp", json=ping, headers={"Authorization": f"Bearer {access}"})
        assert accepted.status_code == 200, accepted.text
        rejected = await lc.post("/mcp", json=ping, headers={"Authorization": f"Bearer {access}"})
        assert rejected.status_code == 401, rejected.text
        assert (await lc.get("/app/status", headers={"Authorization": f"Bearer {access}"})).status_code == 401

        # A client registered on the LAN entry does not exist for the remote entry.
        lan_registered = await register(lc)
        assert lan_registered.status_code == 201, lan_registered.text
        lan_client = lan_registered.json()["client_id"]
        foreign, request_id, _ = await start_authorization(pc, PUBLIC, lan_client)
        assert foreign.status_code in {400, 401} and request_id is None, foreign.text
        own, request_id, _ = await start_authorization(lc, LAN, lan_client)
        assert own.status_code == 302 and request_id
        # Each provider lists only its own clients; the owner's admin view spans both.
        assert {r["client_id"] for r in public.provider.list_clients()} == {public_client}
        assert {r["client_id"] for r in lan.provider.list_clients()} == {lan_client}
        assert {r["issuer"] for r in lan.provider.list_clients(every_issuer=True)} == {PUBLIC, LAN}
        # Redirect schemes: only https (or loopback http) on either entry; a private scheme is refused.
        assert (await register(pc, callback="openlolo://oauth/callback")).status_code == 400
        assert (await register(lc, callback="openlolo://oauth/callback")).status_code == 400
        assert (await register(lc, callback=PUBLIC_CALLBACK, scope="phone:read")).status_code == 201


async def test_real_tls_listener_serves_pinned_certificate(tmp_path):
    config = lan_config(tmp_path)
    runtime = Runtime(config)
    await runtime.start()
    app = create_gateway_app(runtime, config.lan_url, allowed_hosts=config.lan_hosts[1:], app_routes=True)
    server = SecondaryServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_certfile=str(LAN_CERT),
            ssl_keyfile=str(LAN_KEY),
            access_log=False,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context)
        try:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        finally:
            writer.close()
            await writer.wait_closed()
        assert hashlib.sha256(der).hexdigest() == tls.fingerprint(LAN_CERT)

        async with httpx.AsyncClient(verify=False, base_url=f"https://127.0.0.1:{port}") as c:
            pinned = await c.get(
                "/.well-known/oauth-authorization-server", headers={"Host": "openlolo-test.local:8443"}
            )
            assert pinned.status_code == 200 and pinned.json()["issuer"] == LAN
            assert pinned.headers["strict-transport-security"] == "max-age=31536000"
            info = await c.get("/app/info", headers={"Host": "192.168.4.25:8443"})
            assert info.status_code == 200 and info.json()["fingerprint"] == tls.fingerprint(LAN_CERT)
            assert (await c.get("/app/info")).status_code == 403  # Host 127.0.0.1:<port> is not pinned.
    finally:
        server.should_exit = True
        await task
        await runtime.close()
