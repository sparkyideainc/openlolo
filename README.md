# OpenLolo

**Let AI use your iPhone. Without your passwords.**

OpenLolo lets an AI such as Claude or ChatGPT do tasks on your iPhone. It looks at the screen,
opens apps, taps and types, in the apps you are already signed in to. It runs on LoloBox, a
pocket-size computer paired with one iPhone. Nothing is installed on the phone, you approve
every AI that connects, and you can stop it at any time.

[Website](https://openlolo.com) · [Docs](https://openlolo.com/docs/introduction) ·
[Shop](https://openlolo.com/shop)

## How it works

```
Your AI (Claude, ChatGPT)  ──  MCP over the internet  ──▶  LoloBox  ──  USB or Wi-Fi  ──▶  Your iPhone
```

- **LoloBox controls the iPhone** through Apple's CoreDevice services: screenshots, touch,
  typing and the hardware buttons. No jailbreak, no app, no profile, no certificate to renew.
- **Your AI connects to LoloBox** as an MCP server. You approve each AI on the box page, and
  can pause or revoke it there.
- **Pair once with a USB cable.** After that, LoloBox can also reach the phone over Wi-Fi.

See [how OpenLolo compares](https://openlolo.com/docs/comparison) with Siri, Shortcuts and other
tools, and [what it can and cannot do](https://openlolo.com/docs/reference/operating-limits).

## Get started

You need an iPhone on iOS 26 or later. Check the full
[requirements](https://openlolo.com/docs/requirements) first.

1. **Get a LoloBox.** [Buy one ready-made](https://openlolo.com/docs/get-your-lolobox/buy-a-lolobox),
   or [build your own](https://openlolo.com/docs/get-your-lolobox/build-one-yourself/introduction)
   with the recommended Radxa ZERO 3W build. Raspberry Pi guides are coming soon.
2. **Pair your iPhone.** Plug it into the box, tap Trust and turn on Developer Mode. The
   [pairing guide](https://openlolo.com/docs/pair-your-lolobox) takes about five minutes.
3. **Connect your AI.** Add your box to
   [Claude or ChatGPT](https://openlolo.com/docs/connect-your-first-ai) and approve it.

Something not working? See [troubleshooting](https://openlolo.com/docs/troubleshoot/troubleshooting-by-symptom).

### Install on your own board

Building your own box? Run this on Debian 13 ARM64 or Raspberry Pi OS 64-bit:

```sh
curl -fsSL https://openlolo.com/install.sh | sudo sh
```

It downloads the latest [release](https://github.com/sparkyideainc/openlolo/releases), verifies its
checksum and starts the services. The [installer guide](https://openlolo.com/docs/get-your-lolobox/build-one-yourself/run-the-installer)
walks through it, and [install options](https://openlolo.com/docs/reference/install-options)
covers flags such as a LAN-only box.

## What's in this repository

Everything that runs on the box.

| Path | What it is |
|---|---|
| [`apps/device`](apps/device) | The box service (Python). Talks to the iPhone and serves the MCP tools and the box page. |
| [`apps/portal`](apps/portal) | The box page (React), where you approve AI and see what it does. |
| [`packages/sdk-python`](packages/sdk-python/README.md) | Python SDK for scripting the iPhone from your own code. |
| [`packages/ui`](packages/ui) | Shared UI components and styles for the box page. |
| [`packages/config`](packages/config) | Shared TypeScript config. |

## Develop

Box service, from `apps/device` (needs [uv](https://docs.astral.sh/uv/)):

```sh
uv sync --frozen --dev --extra mcp
uv run openlolo serve --simulate      # a simulated phone; box page at http://127.0.0.1:8080
uv run --extra mcp pytest -q
```

Box page and shared UI, from the repository root (needs [Bun](https://bun.sh)):

```sh
bun install
bun run --filter portal build          # builds the box page into apps/device
bun run check-types && bun run check   # TypeScript and Biome
```

Set up the Git hooks once with `bun run prepare` and
`uv run --project apps/device pre-commit install`. More on the box service, including the
simulator, is in [`apps/device/README.md`](apps/device/README.md).

Every `v*` tag builds the box tarball and publishes it as a
[GitHub Release](https://github.com/sparkyideainc/openlolo/releases), which the installer uses.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: you need to be vouched before your first pull
request. Everyone follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems
privately as described in [SECURITY.md](SECURITY.md).

## License

MIT ([LICENSE](LICENSE)). The box's CoreDevice worker runs on
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3), which is GPL-3.0, so a worker
distributed together with it is distributed under GPL-3.0 as a whole. See
[THIRD_PARTY_NOTICES.md](apps/device/THIRD_PARTY_NOTICES.md).
