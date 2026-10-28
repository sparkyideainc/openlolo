# Third-party dependencies and references

This repository implements the local application around Apple's CoreDevice services as
exposed by pymobiledevice3. iMouse is an action-design and calibration reference only; no
dongle package, vendor report extension, or proprietary transport code is included.

OpenLolo's own code, including the CoreDevice worker in `src/openlolo/coredevice/`, is MIT
(see `LICENSE`). Only the worker imports pymobiledevice3, and it runs as its own process in
its own virtual environment; the API, MCP entries and box page reach it over a Unix socket
and never import it. pymobiledevice3 is GPL-3.0, so a worker distributed together with it
(for example, preinstalled on a LoloBox) is distributed under GPL-3.0 as a whole: its source
is this repository at the matching release tag, and pymobiledevice3's source is upstream.
MIT is compatible with GPL-3.0, so this does not change the license of OpenLolo's files.

Hardware dependency, installed only in the worker's separate virtual environment:

| Component | Source / pin | Upstream license |
|---|---|---|
| pymobiledevice3 | PyPI `pymobiledevice3==11.34.0`, resolved in `uv.lock` under the `device` extra | GPL-3.0 |
| pmd-pytcp, qh3, pytun-pmd3, PyAV and other pymobiledevice3 dependencies | as resolved in `uv.lock` | see each distribution |
| usbmuxd | Debian `usbmuxd` (stock build) | GPL-2.0 |

Application dependencies include Pydantic (MIT), Starlette (BSD-3-Clause), Uvicorn
(BSD-3-Clause), HTTPX (BSD-3-Clause), and Pillow (MIT-CMU), plus the optional official
MCP Python SDK and mcp-types (MIT) for the MCP entries. Exact Python resolutions
are in `uv.lock`; `deploy/versions.lock.json` records the pins in prose. Installed
distributions retain their license files. Preserve upstream notices and source availability
when redistributing binary releases; this file is an inventory, not a relicensing of those
projects. The worker's touch, keyboard and button wire formats are documented reverse
engineering in pymobiledevice3; keep the exact pin until a new release is validated on hardware.
