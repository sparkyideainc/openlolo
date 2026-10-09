# OpenLolo

OpenLolo is an open-source AI assistant for iOS devices by [Sparky Idea Inc.](https://openlolo.com)
A small ARM64 Linux box (a Raspberry Pi works) is bound to one iPhone and lets AI agents
observe and operate it through Apple's CoreDevice services: native screenshots, touch,
a virtual keyboard, hardware buttons, and app and device information. Agents connect over
MCP; the owner approves every client and keeps control from the box page.

This repository holds everything that runs on the box:

```
openlolo/
├── apps/
│   ├── device/      # Box service: Python, uv. Ships as the GitHub Release tarball.
│   └── portal/      # Box page served by the device (React), built into the tarball
└── packages/
    ├── ui/          # Shared shadcn/ui components and styles
    └── config/      # Shared TypeScript config
```

## Install on a box

Supported platforms: Debian 13 ARM64 and Raspberry Pi OS 64-bit.

```sh
curl -fsSL https://openlolo.com/install.sh | sudo sh
```

The installer downloads the latest [GitHub Release](https://github.com/sparkyideainc/openlolo/releases),
verifies its checksum, and starts the services. Add `-s -- --no-remote` for a LAN-only box.
The product overview, device details, and operating limits are in the
[device README](apps/device/README.md). Guides are published at [openlolo.com](https://openlolo.com).

## Develop

Python (box service), run from `apps/device`:

```sh
uv sync --frozen --dev --extra mcp
uv run openlolo serve --simulate      # no phone needed
uv run --extra mcp pytest -q
```

TypeScript (box page, shared UI), run from the repository root with [Bun](https://bun.sh):

```sh
bun install
bun run --filter portal build          # box page into apps/device/src/openlolo/web/portal
bun run check-types && bun run check   # tsc and Biome
```

### Shared UI

`packages/ui` holds the shadcn/ui primitives (Base UI, `nova` style) used by the box page.
Design tokens live in `packages/ui/src/styles/globals.css`. Add primitives with:

```sh
npx shadcn@latest add accordion dialog -c packages/ui
```

### Hooks and formatting

- Initialize hooks: `bun run prepare` and `uv run --project apps/device pre-commit install`
- Run checks: `bun run check`

## Releases

Every `v*` tag builds the device tarball in `.github/workflows/release.yml` and publishes
it on the GitHub Release page, which `install.sh` installs from.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Contributors are vouched before their first
pull request.

## License

Apache-2.0 for this repository ([LICENSE](LICENSE)), except `apps/device`, which is
GPL-3.0 ([apps/device/LICENSE](apps/device/LICENSE)) because it links to
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3). See
[THIRD_PARTY_NOTICES.md](apps/device/THIRD_PARTY_NOTICES.md).
