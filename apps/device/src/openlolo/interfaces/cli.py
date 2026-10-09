import argparse
import asyncio
import base64
import contextlib
import getpass
import json
import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn

from openlolo.adapters.storage.sqlite import Journal
from openlolo.adapters.system.setup_ap import ensure_setup_identity
from openlolo.bootstrap import Runtime, default_config_path
from openlolo.config import Config, load_config
from openlolo.interfaces.auth import Auth
from openlolo.interfaces.http_api import calibration_app, create_app


class SecondaryServer(uvicorn.Server):
    """A second uvicorn server in the same process must leave signal handling to the first."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def serve(config):
    runtime = Runtime(config)
    # The MCP SDK configures the root logger at ERROR when the gateway is built; keep the
    # box's own warnings (autopair, portal access) visible in the journal.
    logging.getLogger("openlolo").setLevel(logging.INFO)
    main = uvicorn.Server(
        uvicorn.Config(
            create_app(runtime),
            host=config.host,
            port=config.port,
            access_log=False,
            log_level="warning",
            limit_concurrency=40,
        )
    )
    from openlolo.interfaces.setup_portal import create_setup_portal

    extras = []
    if config.portal_port:
        extras.append(
            SecondaryServer(
                uvicorn.Config(
                    create_setup_portal(runtime),
                    host="0.0.0.0",
                    port=config.portal_port,
                    access_log=False,
                    log_level="warning",
                    limit_concurrency=16,
                    proxy_headers=False,
                )
            )
        )
    if config.public_url:
        from openlolo.interfaces.mcp_http import create_gateway_app

        extras.append(
            SecondaryServer(
                uvicorn.Config(
                    create_gateway_app(runtime),
                    host="127.0.0.1",
                    port=config.mcp_port,
                    access_log=False,
                    log_level="warning",
                    limit_concurrency=16,
                    proxy_headers=False,
                )
            )
        )
    if config.lan_url:
        from openlolo.interfaces import tls
        from openlolo.interfaces.mcp_http import create_gateway_app

        cert, key = config.lan_files
        if not (cert.exists() and key.exists()):
            raise SystemExit(f"LAN entry needs {cert} and {key}; run `openlolo lan-cert` first")
        print(f"LAN entry {config.lan_url} fingerprint sha256:{tls.fingerprint(cert)}")
        extras.append(
            SecondaryServer(
                uvicorn.Config(
                    create_gateway_app(
                        runtime, config.lan_url, allowed_hosts=config.lan_hosts[1:], app_routes=True
                    ),
                    host=config.lan_bind,
                    port=config.lan_port,
                    ssl_certfile=str(cert),
                    ssl_keyfile=str(key),
                    access_log=False,
                    log_level="warning",
                    limit_concurrency=16,
                    proxy_headers=False,
                )
            )
        )
    feedback = None
    task = None
    if config.calibration_host:
        feedback = SecondaryServer(
            uvicorn.Config(
                calibration_app(runtime),
                host=config.calibration_host,
                port=config.calibration_port,
                access_log=False,
                log_level="warning",
                limit_concurrency=8,
            )
        )
        task = asyncio.create_task(feedback.serve())
    tasks = [asyncio.create_task(server.serve()) for server in extras]
    try:
        await main.serve()
    finally:
        for server in extras:
            server.should_exit = True
        for pending in tasks:
            await pending
        if feedback is not None and task is not None:
            feedback.should_exit = True
            await task


def save_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)


def setup_card_command(args, parser):
    """Print the Wi-Fi join details needed for app-free first setup."""
    config = load_config(args.config)
    state_dir = args.state_dir or config.state_dir
    config.state_dir = state_dir
    name, password = ensure_setup_identity(config, rotate_password=args.rotate)
    page = f"http://{name.lower()}.local"
    print(f"Wi-Fi name (SSID): {name}")
    print(f"Wi-Fi password: {password}")
    print(f"Setup page: {page}  (or http://192.168.4.1 on the setup Wi-Fi)")
    if not args.text_only:
        try:
            import qrcode
        except ImportError:
            parser.error("Install the mcp extra (qrcode) or use --text-only")
        # No captive sheet opens on the setup Wi-Fi (the box answers probes as the open internet
        # would), so the card carries the page's address as its own code: the Camera opens it in
        # Safari, which keeps the page through the Wi-Fi handoff and can add it to the Home Screen.
        for title, data in (
            (
                f"1. Join the setup Wi-Fi: scan with the iPhone camera ({name})",
                f"WIFI:T:WPA;S:{name};P:{password};;",
            ),
            (f"2. Open the setup page: scan with the iPhone camera ({page})", page),
        ):
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1)
            qr.add_data(data)
            print(title)
            qr.print_ascii(invert=True)
    print("After joining, follow the local setup page. Keep these details with the box.")


def main():
    parser = argparse.ArgumentParser(
        description="OpenLolo local control. Typed content should be supplied on stdin."
    )
    parser.add_argument("--config", default=str(default_config_path()))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve")
    run.add_argument("--simulate", action="store_true")
    run.add_argument("--state-dir", type=Path, default=Path(".tmp/simulator/state"))
    run.add_argument("--runtime-dir", type=Path, default=Path(".tmp/simulator/run"))
    run.add_argument("--public-url", help="Simulator only: enable the remote MCP entry at this https origin")
    run.add_argument("--mcp-port", type=int, default=8090)
    run.add_argument("--port", type=int, default=8080, help="Simulator only: control API loopback port")
    run.add_argument("--lan-url", help="Simulator only: enable the LAN MCP entry at this https origin")
    lan_cert = commands.add_parser(
        "lan-cert", help="Create (or rotate) the LAN entry's self-signed certificate"
    )
    lan_cert.add_argument(
        "--rotate", action="store_true", help="Replace an existing pair; every app must re-pair"
    )
    lan_cert.add_argument("--state-dir", type=Path)
    setup_card = commands.add_parser(
        "setup-card", help="Print the box's Wi-Fi setup name, password and join QR"
    )
    setup_card.add_argument("--rotate", action="store_true", help="Replace the setup Wi-Fi password")
    setup_card.add_argument("--text-only", action="store_true", help="Print without a QR code")
    setup_card.add_argument("--state-dir", type=Path)
    credential = commands.add_parser("credential", help="Print a single-use credential, valid five minutes")
    credential.add_argument("--state-dir", type=Path)
    clients = commands.add_parser("clients", help="List or revoke MCP clients")
    clients.add_argument("action", choices=["list", "revoke"])
    clients.add_argument("client_id", nargs="?", help="OAuth client_id to revoke")
    clients.add_argument("--state-dir", type=Path)
    login = commands.add_parser("login", help="Exchange a credential for a local CLI session")
    login.add_argument("--url", default="http://127.0.0.1:8080")
    login.add_argument("--session", type=Path, default=Path.home() / ".config/openlolo/session.json")
    call = commands.add_parser("call", help="Call the shared HTTP Phone API; JSON body from stdin or --json")
    call.add_argument("path")
    call.add_argument("--json")
    call.add_argument("--url", default="http://127.0.0.1:8080")
    call.add_argument("--session", type=Path, default=Path.home() / ".config/openlolo/session.json")
    call.add_argument("--output", type=Path, help="Write screenshot bytes; output must not already exist")
    commands.add_parser("discover", help="List USB-attached phones and Wi-Fi-paired records (worker)")
    commands.add_parser("device-status", help="Print the CoreDevice worker connection state")
    pair = commands.add_parser(
        "pair", help='Explicit setup only; trust prompt on the phone. Reads {"udid": ...} from stdin'
    )
    pair.add_argument("--owner-confirmed", action="store_true", required=True)
    developer = commands.add_parser(
        "developer-mode", help='Explicit setup only; request Developer Mode. Reads {"udid": ...} from stdin'
    )
    developer.add_argument("--owner-confirmed", action="store_true", required=True)
    commands.add_parser("mount-ddi", help="Ask the worker to mount the developer disk image once")
    args = parser.parse_args()
    if args.command == "serve":
        if args.simulate:
            config = Config(
                backend="simulated",
                state_dir=args.state_dir,
                runtime_dir=args.runtime_dir,
                port=args.port,
                allowed_hosts=[f"127.0.0.1:{args.port}", f"localhost:{args.port}"],
                allowed_origins=[f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"],
                public_url=args.public_url,
                mcp_port=args.mcp_port,
                lan_url=args.lan_url,
            )
            if config.lan_url:
                from openlolo.interfaces import tls

                tls.ensure(*config.lan_files, urlsplit(config.lan_url).hostname or "openlolo")
        elif args.public_url or args.lan_url:
            parser.error("--public-url/--lan-url apply to --simulate; set them in box.toml on a Pi")
        else:
            config = load_config(args.config)
        return asyncio.run(serve(config))
    if args.command == "lan-cert":
        from openlolo.interfaces import tls

        config = load_config(args.config)
        if not config.lan_url:
            parser.error("Set lan_url in box.toml first")
        if args.rotate:
            for path in config.lan_files:
                path.unlink(missing_ok=True)
        print(f"sha256:{tls.ensure(*config.lan_files, urlsplit(config.lan_url).hostname or 'openlolo')}")
        return
    if args.command == "setup-card":
        return setup_card_command(args, parser)
    if args.command == "credential":
        state_dir = args.state_dir or load_config(args.config).state_dir
        journal = Journal(state_dir / "openlolo.sqlite3")
        try:
            print(Auth(journal).issue())
        finally:
            journal.close()
        return
    if args.command == "clients":
        from openlolo.interfaces.mcp_http import OAuthProvider

        state_dir = args.state_dir or load_config(args.config).state_dir
        journal = Journal(state_dir / "openlolo.sqlite3")
        try:
            auth = Auth(journal)
            provider = OAuthProvider(journal, auth, "https://unused.invalid")
            if args.action == "list":
                clients = provider.list_clients(every_issuer=True)
                print(json.dumps({"clients": clients}, indent=2))
            elif not args.client_id:
                parser.error("clients revoke requires CLIENT_ID")
            elif provider.revoke_client(args.client_id):
                print("Revoked; the running API drops its session within 30 seconds.")
            else:
                print("Unknown client", file=sys.stderr)
                raise SystemExit(1)
        finally:
            journal.close()
        return
    if args.command in {"discover", "device-status", "pair", "developer-mode", "mount-ddi"}:
        from openlolo.workers.protocol import rpc

        config = load_config(args.config)
        socket_path = config.runtime_dir / "device.sock"
        if args.command == "discover":
            request, timeout = {"op": "discover"}, 30
        elif args.command == "device-status":
            request, timeout = {"op": "status"}, 5
        elif args.command == "mount-ddi":
            request, timeout = {"op": "mount_ddi"}, 10
        else:
            candidate = json.load(sys.stdin)
            request = {
                "op": "pair" if args.command == "pair" else "developer_mode",
                "udid": candidate.get("udid"),
                "owner_confirmed": args.owner_confirmed,
            }
            timeout = 120 if args.command == "pair" else 40
        print(json.dumps(asyncio.run(rpc(socket_path, request, timeout)), indent=2))
        return
    if not args.url.startswith(("http://127.0.0.1:", "http://localhost:")):
        parser.error("Use loopback through an SSH tunnel")
    with httpx.Client(base_url=args.url, headers={"X-OpenLolo-Client": "cli"}, timeout=60) as client:
        if args.command == "login":
            token = getpass.getpass("Local credential: ")
            response = client.post("/api/login", json={"credential": token})
            response.raise_for_status()
            save_private(args.session, {"session": response.cookies["openlolo_session"], "url": args.url})
            print("Logged in. Session expires in eight hours.")
            return
        session = json.loads(args.session.read_text())
        if session["url"] != args.url:
            parser.error("Session belongs to another endpoint; log in again")
        client.cookies.set("openlolo_session", session["session"])
        path = args.path
        if not path.startswith("/api/") or "?" in path or ".." in path:
            parser.error("Use a local /api/ path without query parameters")
        if path in {"/api/status", "/api/capabilities", "/api/apps", "/api/device"} or path.startswith(
            "/api/operations/"
        ):
            response = client.get(path)
        else:
            payload = (
                json.loads(args.json) if args.json else json.load(sys.stdin) if not sys.stdin.isatty() else {}
            )
            if path != "/api/screenshot":
                payload.setdefault("operation_id", str(uuid4()))
                payload.setdefault("lease_token", session.get("lease_token"))
            response = client.post(path, json=payload)
        data = response.json()
        if response.is_error:
            print(json.dumps(data), file=sys.stderr)
            raise SystemExit(1)
        if "control" in data and "lease_token" in data["control"]:
            session["lease_token"] = data["control"]["lease_token"]
            save_private(args.session, session)
            data["control"].pop("lease_token")
        if "image" in data:
            if not args.output:
                parser.error("Screenshot requires --output")
            fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(base64.b64decode(data.pop("image")))
        print(json.dumps(data, indent=2))
