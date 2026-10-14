"""``openlolo serve``: no Phone API listener; the runtime lives as long as the configured entries."""

import asyncio
import os
import signal
import socket

import httpx
import pytest

from openlolo.config import Config
from openlolo.interfaces import cli


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_nothing_to_serve_is_an_error(tmp_path):
    config = Config(backend="simulated", state_dir=tmp_path / "s", runtime_dir=tmp_path / "r")
    with pytest.raises(SystemExit, match="Nothing to serve"):
        await cli.serve(config)


async def test_box_page_only_and_a_signal_stops_everything(tmp_path):
    port, api_port = free_port(), free_port()
    config = Config(
        backend="simulated",
        state_dir=tmp_path / "s",
        runtime_dir=tmp_path / "r",
        portal_port=port,
        port=api_port,
        allowed_hosts=[f"127.0.0.1:{api_port}"],
    )
    task = asyncio.create_task(cli.serve(config))
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                r = await c.get(f"http://127.0.0.1:{port}/setup/status")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
        assert r.status_code == 200 and r.json()["access"]["allowed"] is True
        # The Phone API's old control port answers nothing.
        with pytest.raises(httpx.ConnectError):
            await c.get(f"http://127.0.0.1:{config.port}/api/status")
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)
