"""Passive RemotePairing announcements from the box's avahi-daemon.

avahi already listens to every mDNS packet on the LAN, so following its view of
``_remotepairing._tcp`` costs nothing while nothing changes. An unlocked iPhone advertises at
once; a locked one only in short bursts and refuses new sessions anyway. Each resolved
announcement is a hint that the phone may be reachable now: the caller checks it against the
bound phone's pairing record before acting.
"""

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass

log = logging.getLogger("openlolo.coredevice.announce")

SERVICE = "_remotepairing._tcp"


@dataclass(frozen=True)
class Announcement:
    instance: str
    address: str
    port: int
    properties: dict[str, str]


def parse(line: str) -> Announcement | None:
    """One ``avahi-browse -p -r`` line; only resolved IPv4 entries (``=``) carry an address the
    tunnel can dial without an interface scope."""
    fields = line.rstrip("\n").split(";")
    if len(fields) < 10 or fields[0] != "=" or fields[2] != "IPv4":
        return None
    try:
        port = int(fields[8])
    except ValueError:
        return None
    properties = dict(item.split("=", 1) for item in re.findall(r'"([^"]*)"', fields[9]) if "=" in item)
    return Announcement(fields[3], fields[7], port, properties)


async def announcements(service: str = SERVICE) -> AsyncGenerator[Announcement]:
    """Resolved announcements as avahi reports them, until cancelled. Ends at once when
    avahi-browse is not installed; the caller then relies on its own searches."""
    browse = shutil.which("avahi-browse")
    if browse is None:
        log.warning("avahi-browse not found; the phone coming back is noticed only by searches")
        return
    process = await asyncio.create_subprocess_exec(
        browse,
        "--parsable",
        "--resolve",
        "--no-db-lookup",
        service,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            announcement = parse(line.decode(errors="replace"))
            if announcement is not None:
                yield announcement
    finally:
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 2)
