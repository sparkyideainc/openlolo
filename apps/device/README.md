# OpenLolo

This application lives in `apps/device` in the OpenLolo monorepo. Run the Python setup,
development, and deployment commands below from this directory.

OpenLolo is an open-source AI assistant for iOS devices.

The current implementation is a local control prototype that lets AI agents observe and
operate an explicitly bound iPhone through Apple's CoreDevice services: native
screenshots, touch, a virtual keyboard, hardware buttons, and app/device information.
The phone connects to the ARM64 Linux box either over **USB** or over **Wi-Fi**; the two are
interchangeable transports under one device layer. AI agents use the MCP entries and scripts
use the Python SDK (`packages/sdk-python`) over the same MCP tools; the owner approves both
on the box page. Python, asyncio, Starlette, SQLite and pymobiledevice3; no cloud relay or
persistent video in this prototype. First-time Wi-Fi setup uses an offline local portal;
USB pairing requires accepting iOS's Trust prompt.

Previously named PhoneBox, the project now uses `openlolo` for its Python package,
commands, MCP tools, and deployment paths. Installation and operating guides are
published at [openlolo.com](https://openlolo.com).

## Try the simulator

```sh
uv sync --frozen --dev --extra mcp
uv run openlolo serve --simulate --lan-url https://localhost:8443
```

The box page runs on <http://127.0.0.1:8080>; in the simulator your own machine counts as the
owner's phone on the cable, so you can approve clients there. Drive the simulated phone with
the SDK (it prints an approval request, approve it under Access):

```sh
uv run python -c 'from openlolo_sdk import Phone; Phone("https://localhost:8443").home()'
```

The simulated phone's nine-target image is synthetic; simulation is not hardware validation.
Control lapses after 90 seconds without an action or screenshot (`lease_seconds`). Pause
cancels pending input and attempts release-all on a separate worker channel.

```sh
uv run --extra mcp pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

## Deploy on ARM64 Linux

The supported box platforms are Debian 13 ARM64 and Raspberry Pi OS 64-bit. Install with
the command below, then finish owner setup from the box page. Runtime configuration is `/etc/openlolo`,
persistent state `/var/lib/openlolo`, and sockets `/run/openlolo`.

```sh
# On the box: install the latest GitHub Release (add `-s -- --no-remote` for a LAN-only box).
curl -fsSL https://openlolo.com/install.sh | sudo sh
# Or build your own tarball from a commit, scp it, and run scripts/install.sh on the box.
sh scripts/package.sh                      # dist/openlolo-device-YYYYMMDD-<commit>.tar.gz
```

The MCP entries and the box page call the phone core in-process, one bridge per caller owned
by its OAuth client (or the box page), so every door shares the leases, the journal and Pause. The box
page (port 80) is the owner surface; the MCP entries (remote and LAN) are the only other doors.

## Device connection

```text
USB mode                          Wi-Fi mode

ARM64 Linux box                   ARM64 Linux box
     |                                 |
    USB (usbmuxd, lockdown trust)    Wi-Fi (Bonjour + RemotePairing record)
     |                                 |
 CoreDeviceProxy tunnel           RemotePairing tunnel
     |                                 |
 RSD / RemoteXPC services  ======  RSD / RemoteXPC services
   screenshot · touch · keyboard · buttons · apps · device info
                        |
                    OpenLolo
```

The worker process (`openlolo-device`) owns one persistent session: it discovers the
bound phone, checks trust, Developer Mode and the developer disk image (mounting it when
allowed), opens the tunnel without root, keeps the service clients open, watches for loss,
and reconnects after a phone reboot, a Wi-Fi interruption, or a USB unplug/replug.
`openlolo_status` (and the box page) reports the connection state and the reason when the
owner must act. Switching modes is an owner action on the box page; the phone stays bound.

## Remote MCP for AI clients

Claude.ai, ChatGPT, and API-level agents can reach the Pi through a public HTTPS URL
served by Tailscale Funnel (Cloudflare Tunnel remains optional). The Pi keeps the MCP server, an embedded OAuth 2.1
authorization server, and all authorization decisions; the tunnel only carries TLS.
Every client is approved by the owner on the box page (open the request under Access, or
scan the QR code on the client's consent page) for a bounded duration; approval grants the
screen and input together, and control expires with the chosen window. Claude Code and
Claude Desktop connect the same way: add the remote URL as an HTTP MCP server and approve
the request when the browser opens.

## Scripts

`packages/sdk-python` (`openlolo-sdk`) scripts the phone from Python:
`Phone("openlolo-ab12").open_app("com.apple.mobilenotes").type("hello").home()`. The first run
asks the owner to approve it on the box page (OAuth device authorization); the grant is cached
and refreshed until the owner removes it. `scripts/bench.py` (step list) and
`scripts/endurance.py` (bounded tap/capture run with latency percentiles) are built on it.

## Box page

The box page (`apps/portal`, built into the release tarball) is the owner surface: status
and Pause, phone setup, Wi-Fi provisioning, AI-client approvals and revocation, activity,
diagnostics, and reset. It is served on port 80 over the setup access point and the LAN,
and its `/app/*` calls are admitted only while the owner's iPhone is on the box's USB port
or from that phone itself (ADR 0012). AI agents use the remote MCP entry above instead.

## Contributing

Pull requests from unvouched users are closed automatically. Open a
[Vouch Request](https://github.com/sparkyideainc/openlolo/issues/new?template=vouch-request.yml)
issue first, then read the repository [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Development checks

For repository hook setup and cross-app contribution guidance, see the root
[CONTRIBUTING.md](../../CONTRIBUTING.md). Run the Python commands below from `apps/device`.

```sh
uv sync --frozen --dev --extra mcp
uv run ruff check src tests && uv run ruff format --check src tests
uv run --extra mcp ty check src tests
uv run --extra mcp pytest -q
```

CI runs the same checks on Python 3.13, the system Python on the box. Type hints are gradual: `ty`
checks annotated code and infers the rest, so annotate new and touched code. The
`coredevice` worker modules import pymobiledevice3 lazily and are exempt from import
resolution on development machines; `uv sync --extra device` installs it locally.

## Operating limits

- Saved trust only. Explicit owner pairing (`openlolo pair --owner-confirmed`) is the one
  path that shows a prompt; it also creates the Wi-Fi pairing record. Missing trust
  returns `TRUST_REQUIRED`; a missing Wi-Fi record returns `WIFI_PAIRING_REQUIRED`.
- Developer Mode must be enabled on the phone (iOS 16+). The developer disk image is
  mounted automatically after each phone reboot when `auto_mount_ddi` is set; this needs
  internet access. iOS 17.4 or later is required for the USB tunnel.
- Screenshots are the phone's native PNG; JPEG and aspect-preserving resizing are applied
  on the Pi. The phone reports no capture timestamp; source age remains unknown.
- Touch coordinates are normalized within the portrait phone image. Use portrait
  orientation lock. Taps, long presses and single-finger drags only; no multitouch.
- Printable U.S. ASCII only, up to 200 characters, through a virtual U.S. keyboard;
  unsupported text is rejected before any report. Named keys and staged
  Ctrl/Shift/Alt/Cmd chords are available. Named shortcuts require per-device
  owner-recorded validation. Hardware buttons: home, lock, volume up/down, mute, siri.
- Input is authenticated by the phone only while a media stream the worker started is
  alive. The worker keeps that stream open for as long as a control lease lives, so the
  phone shows its screen-recording indicator for the whole session, including between
  actions (`hold_stream_with_lease`). Screenshots decode one keyframe the phone sends on
  request; nothing is decoded between them.
- A dispatched operation interrupted by cancellation or a crash is `OUTCOME_UNKNOWN`.
  It is never replayed automatically. Inspect the phone and choose a new action.
- Images and typed content are not journaled or logged. The journal stores keyed
  request fingerprints and outcomes. Browser previews and screenshots are transient.
- The touch, keyboard and button wire formats come from pymobiledevice3's reverse
  engineering; the pin is exact. Hardware acceptance of this CoreDevice stack is pending.
