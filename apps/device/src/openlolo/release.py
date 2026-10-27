"""The installed release: what scripts/package.sh stamped into RELEASE, else the package version."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from openlolo import __version__

RELEASE_FILE = Path("/opt/openlolo/current/RELEASE")


@cache
def installed_release(path: Path = RELEASE_FILE) -> str:
    """``release=`` from RELEASE (the GitHub tag without ``v``, or a dev build name).

    Read once per process: an update replaces the file and restarts the API.
    """
    try:
        for line in path.read_text().splitlines():
            if line.startswith("release="):
                release = line.removeprefix("release=").strip()
                if release:
                    return release
    except OSError:
        pass
    return __version__
