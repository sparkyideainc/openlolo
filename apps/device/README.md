# OpenLolo

This application lives in `apps/device` in the OpenLolo monorepo. Run the Python setup,
development, and deployment commands below from this directory.

OpenLolo is an open-source AI assistant for iOS devices.

The current implementation is a local control prototype that lets AI agents observe and
operate an explicitly bound iPhone through Apple's CoreDevice services: native
screenshots, touch, a virtual keyboard, hardware buttons, and app/device information.
The phone connects to the ARM64 Linux box either over **USB** or over **Wi-Fi**; the two are
interchangeable transports under one device layer. The browser UI runs through an SSH
tunnel. Local stdio and remote MCP adapters expose the same API to agents. Python,
asyncio, Starlette, SQLite, pymobiledevice3, and plain browser assets; no cloud relay or
persistent video in this prototype. First-time Wi-Fi setup uses an offline local portal;
USB pairing requires accepting iOS's Trust prompt.

Previously named PhoneBox, the project now uses `openlolo` for its Python package,
commands, MCP tools, and deployment paths. Installation and operating guides are
published at [openlolo.com](https://openlolo.com).

## Try the simulator

```sh
uv sync --frozen --dev
uv run openlolo serve --simulate
# In a second terminal; paste this single-use credential into the login form:
uv run openlolo credential --state-dir .tmp/simulator/state
```

Open <http://127.0.0.1:8080>. Acquire control, refresh the screenshot, then tap, drag,
type, send keys, or press hardware buttons. The simulated phone is clearly labeled. Its
nine-target image is synthetic; simulation is not hardware validation. Control lasts 60
seconds and the UI renews every 15 seconds. Pause cancels pending input and attempts
release-all on a separate worker channel. Resume explicitly before sending more input.

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
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 openlolo@BOX_ADDRESS
# On the box, issue a credential for this browser login:
sudo -u openlolo /opt/openlolo/current/.venv/bin/openlolo credential
```

The control API and browser UI accept only loopback connections and configured Host/Origin
values. Port 80 separately serves setup assets and limited Wi-Fi routes. Credentials go in
the login form, never in URLs. The CLI uses the same HTTP Phone
API and operation coordinator as the browser; it cannot bypass leases or binding.

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
`/api/status` reports the connection state and the reason when the owner must act.
Switch modes with `POST /api/setup/transport` or the UI; the phone stays bound.

## Remote MCP for AI clients

Claude.ai, ChatGPT, and API-level agents can reach the Pi through a public HTTPS URL
served by Tailscale Funnel (Cloudflare Tunnel remains optional). The Pi keeps the MCP server, an embedded OAuth 2.1
authorization server, and all authorization decisions; the tunnel only carries TLS.
Every client is approved by the owner (from the box page, with a PIN, or with a
single-use credential) for a bounded duration; approval grants the screen and input
together, and control expires with the chosen window.

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

CI runs the same checks on Python 3.11, 3.13, and 3.14. Type hints are gradual: `ty`
checks annotated code and infers the rest, so annotate new and touched code. The
`coredevice` worker modules import pymobiledevice3 lazily and are exempt from import
resolution on development machines; `uv sync --extra device` installs it locally.

## Local MCP

Install `uv sync --frozen --extra mcp`, create a dedicated private login, and launch
`uv run --extra mcp openlolo-mcp --session ~/.config/openlolo/mcp-session.json`
from a stdio MCP host. The adapter runs on the Mac over the same SSH tunnel and
preserves the Pi's leases, journal, and frame checks. See the
[host configuration template](config/mcp.example.json).

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
  alive; the phone shows its screen-recording indicator during input.
- A dispatched operation interrupted by cancellation or a crash is `OUTCOME_UNKNOWN`.
  It is never replayed automatically. Inspect the phone and choose a new action.
- Images and typed content are not journaled or logged. The journal stores keyed
  request fingerprints and outcomes. Browser previews and screenshots are transient.
- The touch, keyboard and button wire formats come from pymobiledevice3's reverse
  engineering; the pin is exact. Hardware acceptance of this CoreDevice stack is pending.
