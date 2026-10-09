"""Consented diagnostics export (PRD FR-10).

Included: software versions, configuration without secrets, the same status the owner sees,
network state without credentials, journal counts and the recent operation list, thermal and
load figures, and a bounded, redacted excerpt of the service logs. Excluded by construction:
images, typed text, credentials, pairing records, Wi-Fi passwords, raw USB dumps.
"""

import asyncio
import platform
import sys
from typing import Any

from openlolo import __version__
from openlolo.interfaces.app_api import redact

SECRET_KEYS = {"lan_key", "lan_cert"}
LOG_LINES = 400


async def journal_excerpt(units: tuple[str, ...] = ("openlolo-api", "openlolo-device")) -> list[str]:
    args = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(LOG_LINES), "--since", "-1h"]
    for unit in units:
        args += ["-u", unit]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(process.communicate(), 5)
    except (OSError, asyncio.TimeoutError):
        return ["<journal unavailable>"]
    return [redact(line) for line in out.decode("utf-8", "replace").splitlines()[-LOG_LINES:]]


async def export(runtime) -> dict[str, Any]:
    config = runtime.config
    settings = {
        k: str(v) if k.endswith("_dir") else v for k, v in config.model_dump().items() if k not in SECRET_KEYS
    }
    status = await runtime.phone.status()
    status.get("profile", {}).get("calibration", {}).pop("samples", None)
    counts = {
        row["state"]: row["n"]
        for row in runtime.journal.db.execute(
            "SELECT state, COUNT(*) AS n FROM operations GROUP BY state"
        ).fetchall()
    }
    network = getattr(runtime, "network", None)
    provisioning = getattr(runtime, "provisioning", None)
    result: dict[str, Any] = {
        "versions": {
            "openlolo": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "config": settings,
        "status": status,
        "journal": {"counts": counts, "recent": runtime.journal.list_operations(100)},
        "logs": await journal_excerpt(),
    }
    if network is not None:
        result["network"] = await network.status()
    if provisioning is not None:
        result["provisioning"] = provisioning.health()
    return result
