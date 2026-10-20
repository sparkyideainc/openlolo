#!/usr/bin/env python3
"""Foreground observer bench (docs/plans/app-permissions.md, PR 1). Runs on the box against the
device worker socket; needs ``foreground_observer`` set to "demand" or "poll" in box.toml.

    sudo -u openlolo-device <venv>/bin/python scripts/foreground_bench.py watch --seconds 60
    sudo -u openlolo-device <venv>/bin/python scripts/foreground_bench.py steps --count 20 --think 3,10
    sudo -u openlolo-device <venv>/bin/python scripts/foreground_bench.py idle --seconds 120

watch  asks for the foreground app every --interval seconds and prints each change, so a person
       (or an MCP client) can drive the phone through the overlay matrix meanwhile.
steps  alternates launches of --apps with the Home button; after each action it models the
       post-action screenshot (query starts at --floor, the image is due at --settle) and, after
       a think gap, the check before the next action. Prints p50/p95 of both waits.
idle   samples the worker's CPU time with no requests, e.g. across the poll-mode tail.
Each mode ends with the worker's CPU seconds and the observer counters.
"""

import argparse
import asyncio
import json
import random
import statistics
import subprocess
import time
from pathlib import Path

from openlolo.adapters.storage.binding import BindingStore
from openlolo.config import load_config
from openlolo.workers.protocol import rpc

HOME = (0x0C, 0x40)


class Bench:
    def __init__(self, config):
        self.socket = config.runtime_dir / "device.sock"
        binding = BindingStore(config.state_dir).read()
        if binding is None:
            raise SystemExit("No bound phone")
        self.binding = binding.model_dump()

    async def call(self, op, timeout=30, **fields):
        return await rpc(self.socket, {"op": op, "binding": self.binding, **fields}, timeout)

    async def foreground(self, fresh=False):
        started = time.monotonic()
        result = await self.call("foreground", fresh=fresh)
        return time.monotonic() - started, result["foreground"], result["observer"]

    async def launch(self, bundle_id):
        await self.call("launch_app", bundle_id=bundle_id, url=None, kill_existing=False)

    async def home(self):
        generation = (await self.call("begin", kinds=["button"]))["generation"]
        for state in ("down", "up"):
            await rpc(
                self.socket,
                {"op": "report", "generation": generation, "kind": "button", "value": [*HOME, state]},
            )
            await asyncio.sleep(0.05)


def worker_cpu() -> float:
    """utime + stime of the device worker, in seconds."""
    pid = subprocess.run(
        ["pgrep", "-f", "bin/openlolo-device"], capture_output=True, text=True
    ).stdout.split()
    if not pid:
        return float("nan")
    fields = Path(f"/proc/{pid[0]}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / 100


def summary(name, values):
    if not values:
        return f"{name}: none"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return (
        f"{name}: n={len(values)} p50={statistics.median(values) * 1000:.0f} ms "
        f"p95={p95 * 1000:.0f} ms max={ordered[-1] * 1000:.0f} ms"
    )


async def watch(bench, args):
    start, last = time.monotonic(), None
    observer = {}
    while time.monotonic() - start < args.seconds:
        try:
            took, fg, observer = await bench.foreground(fresh=args.fresh)
        except Exception as exc:  # Keep watching through a transient refusal.
            print(f"+{time.monotonic() - start:6.2f}s error {exc}", flush=True)
            await asyncio.sleep(args.interval)
            continue
        key = (fg["context"], tuple(fg["apps"]), fg["unidentified"])
        if key != last:
            print(
                f"{time.strftime('%H:%M:%S')} +{time.monotonic() - start:6.2f}s "
                f"{fg['context']:9} apps={fg['apps']} "
                f"unidentified={fg['unidentified']} wait={took * 1000:.0f} ms",
                flush=True,
            )
            last = key
        await asyncio.sleep(args.interval)
    return observer


async def steps(bench, args):
    low, _, high = args.think.partition(",")
    post, pre, contexts = [], [], []
    observer = {}
    actions = [("launch", app) for app in args.apps.split(",")] + [("home", None)]
    for index in range(args.count):
        kind, app = actions[index % len(actions)]
        await (bench.launch(app) if kind == "launch" else bench.home())
        acted = time.monotonic()  # Settle starts when the action has been sent.
        await asyncio.sleep(max(0.0, args.floor - (time.monotonic() - acted)))
        took, fg, observer = await bench.foreground()
        # The image is due at --settle anyway; only the part of the query past it is extra.
        post.append(max(0.0, (time.monotonic() - acted) - args.settle))
        contexts.append(fg["context"])
        print(
            f"{index:3d} {kind:6} {app or '':24} -> {fg['context']:9} {fg['apps']} "
            f"query={took * 1000:.0f} ms",
            flush=True,
        )
        await asyncio.sleep(random.uniform(float(low), float(high or low)))
        took, fg, observer = await bench.foreground()
        pre.append(took)
    print(summary("post-action extra", post))
    print(summary("pre-action wait", pre))
    print("contexts:", {c: contexts.count(c) for c in set(contexts)})
    return observer


async def lag(bench, args):
    """After each action, query back to back for --window seconds: when does the answer change?"""
    observer = {}
    actions = [("launch", app) for app in args.apps.split(",")] + [("home", None)]
    for index in range(args.count):
        kind, app = actions[index % len(actions)]
        await (bench.launch(app) if kind == "launch" else bench.home())
        acted, line = time.monotonic(), []
        while time.monotonic() - acted < args.window:
            sent = time.monotonic() - acted
            took, fg, observer = await bench.foreground(fresh=True)
            label = ",".join(a.rsplit(".", 1)[-1] for a in fg["apps"]) or "-"
            line.append(f"{sent:.2f}s:{label}")
        print(f"{index:3d} {kind:6} {(app or '').rsplit('.', 1)[-1]:12} {' '.join(line)}", flush=True)
        await asyncio.sleep(args.gap)
    return observer


async def cost(bench, args):
    """Back-to-back fresh queries with no actions: worker CPU per query."""
    took = []
    observer = {}
    for _ in range(args.count):
        seconds, _fg, observer = await bench.foreground(fresh=True)
        took.append(seconds)
    print(summary("query", took))
    return observer


async def idle(bench, args):
    await asyncio.sleep(args.seconds)
    return (await bench.call("foreground", timeout=30))["observer"] if args.query_after else {}


async def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="/etc/openlolo/box.toml")
    sub = parser.add_subparsers(dest="mode", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--seconds", type=float, default=60)
    w.add_argument("--interval", type=float, default=0.3)
    w.add_argument("--fresh", action="store_true", help="query every time instead of reusing")
    s = sub.add_parser("steps")
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--think", default="3,10", help="seconds, or low,high for a uniform gap")
    s.add_argument("--settle", type=float, default=0.5)
    s.add_argument("--floor", type=float, default=0.3)
    s.add_argument("--apps", default="com.apple.mobilenotes,com.apple.Preferences")
    g = sub.add_parser("lag")
    g.add_argument("--count", type=int, default=9)
    g.add_argument("--window", type=float, default=3)
    g.add_argument("--gap", type=float, default=2)
    g.add_argument("--apps", default="com.apple.mobilenotes,com.apple.Preferences")
    c = sub.add_parser("cost")
    c.add_argument("--count", type=int, default=20)
    i = sub.add_parser("idle")
    i.add_argument("--seconds", type=float, default=120)
    i.add_argument("--query-after", action="store_true")
    args = parser.parse_args()
    bench = Bench(load_config(args.config))
    cpu, wall = worker_cpu(), time.monotonic()
    observer = await {"watch": watch, "steps": steps, "lag": lag, "cost": cost, "idle": idle}[args.mode](
        bench, args
    )
    elapsed = time.monotonic() - wall
    used = worker_cpu() - cpu
    print(f"worker cpu: {used:.2f} s over {elapsed:.0f} s = {100 * used / elapsed:.1f}% of one core")
    if observer:
        print("observer:", json.dumps({k: v for k, v in observer.items() if k != "events"}))
        print("events:", len(observer.get("events", [])))


if __name__ == "__main__":
    asyncio.run(main())
