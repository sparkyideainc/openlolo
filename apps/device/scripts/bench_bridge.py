#!/usr/bin/env python3
"""Overhead of the in-process bridge on the simulated backend: no phone, no network.

Times a screenshot, a tap and a tap with its observation through LocalPhone, and one tap through
the MCP tool layer, with the simulator's fixed capture sleep removed so only the box's own work
is measured. Frames are phone-sized (709x1536 JPEG, about 670 KiB) unless --small is given.

    uv run --extra mcp python scripts/bench_bridge.py [--samples 50] [--small]

Runs on a developer machine or on the box itself (stop the openlolo service first, or point
--state at a scratch directory, since the runtime takes a lock on its runtime directory).
"""

import argparse
import asyncio
import io
import statistics
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from mcp import Client
from PIL import Image

from openlolo.bootstrap import Runtime
from openlolo.config import Config
from openlolo.domain.models import Frame
from openlolo.interfaces.local_phone import LocalPhone
from openlolo.interfaces.mcp_server import create_server


def report(name: str, samples: list[float]) -> None:
    ms = sorted(s * 1000 for s in samples)
    p95 = ms[max(0, int(len(ms) * 0.95) - 1)]
    print(f"{name:44s} p50={statistics.median(ms):8.2f} ms  p95={p95:8.2f} ms  n={len(ms)}")


async def timed(step, samples: int, warmup: int = 3) -> list[float]:
    for _ in range(warmup):
        await step()
    out = []
    for _ in range(samples):
        started = time.perf_counter()
        await step()
        out.append(time.perf_counter() - started)
    return out


def phone_sized(device) -> None:
    """Replace the simulator's small frame with a noisy 709x1536 JPEG, the observation size."""
    buffer = io.BytesIO()
    Image.effect_noise((709, 1536), 40).convert("RGB").save(buffer, format="JPEG", quality=90)
    image = buffer.getvalue()
    capture = device.capture

    async def large(binding, options):
        frame = await capture(binding, options)
        return Frame(frame.metadata, image)

    device.capture = large


async def main(samples: int, small: bool, state: Path) -> None:
    runtime = Runtime(Config(backend="simulated", state_dir=state / "state", runtime_dir=state / "run"))
    await runtime.start()
    try:
        device = runtime.phone.device
        device.delay = 0
        if not small:
            phone_sized(device)
        bridge = LocalPhone(runtime.phone, owner="client:bench")
        await bridge.ensure_control()
        size = len((await runtime.phone.screenshot()).image)
        print(f"frame {size / 1024:.0f} KiB JPEG, {samples} samples after 3 warm-ups")

        report("screenshot", await timed(lambda: bridge.screenshot({"max_size": 1536}), samples))

        async def tap(observe: bool):
            frame = (await bridge.screenshot({"max_size": 1536}))["metadata"]
            action = {
                "kind": "tap",
                "frame_id": frame["frame_id"],
                "geometry_epoch": frame["geometry_epoch"],
                "x": 0.5,
                "y": 0.5,
            }
            started = time.perf_counter()
            result = await bridge.action(str(uuid4()), action, observe)
            assert result["operation"]["state"] == "SUCCEEDED", result
            return time.perf_counter() - started

        for name, observe in (("tap, observe=false (130 ms HID holds)", False), ("tap + observation", True)):
            for _ in range(3):
                await tap(observe)
            report(name, [await tap(observe) for _ in range(samples)])

        async with Client(create_server(bridge)) as client:
            frame = (await client.call_tool("openlolo_screenshot", {})).structured_content["metadata"]
            out = []
            for i in range(samples + 3):
                started = time.perf_counter()
                result = await client.call_tool(
                    "openlolo_tap",
                    {
                        "frame_id": frame["frame_id"],
                        "geometry_epoch": frame["geometry_epoch"],
                        "x": 0.5,
                        "y": 0.5,
                        "settle": 0,
                    },
                )
                if i >= 3:
                    out.append(time.perf_counter() - started)
                frame = result.structured_content["observation"]["metadata"]
            report("MCP openlolo_tap (settle 0, observation)", out)
    finally:
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--small", action="store_true", help="keep the simulator's 390x844 frame")
    parser.add_argument("--state", type=Path, help="scratch directory (default: a new temporary one)")
    args = parser.parse_args()
    asyncio.run(
        main(args.samples, args.small, args.state or Path(tempfile.mkdtemp(prefix="openlolo-bench-")))
    )
