# OpenLolo project context

OpenLolo is an open-source AI assistant for iOS devices, previously named PhoneBox.
Use `OpenLolo` in prose, `openlolo` for packages/commands/paths, and `OPENLOLO`
for environment-variable prefixes.

The current implementation is a Python local-control prototype in `apps/device`: an owner-bound iPhone
observed and driven through Apple CoreDevice services (screenshots, touch, keyboard,
hardware buttons, app and device information) over either a USB or a Wi-Fi transport,
a browser UI over an SSH tunnel, and stdio/remote MCP adapters. The device layer lives
in `src/openlolo/coredevice` and runs as a separate worker process. The broader AI
assistant vision does not mean cloud orchestration is implemented. The owner surface is the
box page (`apps/portal`), served by the device over the setup access point and the LAN; it
drives the `/app/*` API behind a session that the cabled phone unlocks (ADR 0012). The LAN
TLS gateway is optional and only for LAN MCP clients.

Read `README.md` for setup. The Python package is `src/openlolo`; preserve owner binding, authentication,
control leases, explicit pairing requirements, and the transport-agnostic `DeviceBackend`
port when making changes. pymobiledevice3 is pinned exactly; do not bump it without hardware validation.

The device backend is Python in `apps/device/src/openlolo`. The monorepo also has the box
page (`apps/portal`) and the public website (`apps/web`). The `/app/*` API is the contract
between the two; change both sides together.

Development checks (run from `apps/device`):

```sh
uv sync --frozen --dev --extra mcp
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen ty check src tests
uv run --frozen --extra mcp pytest -q
uv build
```


For continuity across the local folder rename, read `.context/session-handoff.md`
if it exists. That local, ignored file records the session ID and the last verified
state. Use the actual checkout directory rather than assuming its old folder name.
