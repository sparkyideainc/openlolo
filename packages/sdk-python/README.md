# openlolo-sdk

Script the iPhone behind an OpenLolo box from Python.

```python
from openlolo_sdk import Phone

with Phone("openlolo-ab12") as phone:          # or "https://<box>.ts.net" for remote access
    phone.apps("notes")                          # [{"bundle_id": "com.apple.mobilenotes", ...}]
    phone.open_app("com.apple.mobilenotes").type("hello").home()
    frame = phone.screenshot()
    frame.save("screen.jpg")
    frame.tap(200, 400)                          # pixels of that image
    phone.tap(0.5, 0.9)                          # or fractions of the screen
```

Only the box name, a LAN address, or the remote URL is needed.

## First run: approve on the box page

The first call asks the box for access and prints:

```
OpenLolo: approve 'nightly.py on mac-mini' on the box page (Access). Code BCDF-GHJK.
  http://openlolo-ab12.local/access?r=…
  For unattended scripts choose 'Until I remove it'.
```

Open the box page on your phone, check the code, and tap Approve. You pick how long the access
lasts: 24 hours, 30 days renewed while used, or until you remove it under Access. Scripts get
screen and input access, never box settings.

The grant is cached in `~/.config/openlolo/sdk/` (one file per box, mode 0600) and refreshed
without asking again. Without a terminal (cron, CI) the SDK never waits for approval: it raises
`ApprovalRequired`. Run once by hand, or use `openlolo-sdk login <box>`.

## Security

- **LAN** (`openlolo-xxxx.local`, private IPs): the box serves a self-signed certificate. The SDK
  pins its SHA-256 on the first approval and refuses any other certificate afterwards
  (`PinMismatch`), checked during the TLS handshake, before any token is sent. Someone on your
  network during the very first approval could still relay it; to rule that out, pass
  `Phone(box, fingerprint="…")` with the fingerprint read on the box itself:
  `openssl x509 -in /var/lib/openlolo/tls/lan.crt -outform der | sha256sum`.
- **Remote** (`https://….ts.net`): normal certificate verification.
- The SDK calls the same MCP tools AI agents use, so control leases, pause, the activity journal,
  and removal under Access apply to scripts as well.

## Behaviour worth knowing

- **Touches refer to a frame.** Every action returns a fresh screenshot by default
  (`observe=True`), and the next touch refers to it. If the screen was captured more than 120 s
  ago or another client captured since, the box refuses with `FrameExpired`; take a
  `screenshot()` and decide again. The SDK never re-taps on its own.
- **`type(text)`** types short printable ASCII key by key; anything longer or non-ASCII is pasted
  through the phone clipboard (`paste`), which replaces the clipboard.
- **Retries.** A dropped connection retries the call once with the same operation id; the box
  runs each id at most once. If that fails too, the SDK reads the journal and raises
  `OutcomeUnknown`: check the screen, do not replay.
- **Rate limit.** 120 calls a minute; `RATE_LIMITED` is retried after 1, 2 and 4 s.
- **Control.** The first action takes control; it lapses after 90 s without activity.
  `close()` (or leaving the `with` block) releases it. `ControlBusy` means another client has it.

## Command line

```
openlolo-sdk login openlolo-ab12
openlolo-sdk status openlolo-ab12
openlolo-sdk screenshot openlolo-ab12 screen.png
openlolo-sdk call openlolo-ab12 openlolo_button button=home observe=false
```

`call` takes any MCP tool name and prints its JSON result, so any language can drive the phone
through this command. The wire protocol itself is documented in `docs/sdk-protocol.md`.
