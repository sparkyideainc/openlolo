#!/usr/bin/env python3
"""Run the bounded Safari grid/endurance acceptance through the public Phone API.

Requires an authenticated CLI session and an enabled calibration listener. The
owner opens the printed URL on the bound phone. No page navigation or unrelated
phone input is performed. Evidence is metadata only; images are discarded.
"""

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--viewport-top", type=float, required=True)
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument(
        "--capture-every",
        type=int,
        default=1,
        help="Capture after every N grid taps (1–25); training/held-out taps always capture",
    )
    parser.add_argument("--out", type=Path, default=Path(".tmp/validation/milestone/acceptance"))
    args = parser.parse_args()
    if not 0 <= args.trials <= 1000 or not 0 <= args.minutes <= 90:
        parser.error("Bound trials to 1000 and duration to 90 minutes")
    if not 1 <= args.capture_every <= 25:
        parser.error("Bound capture cadence to 1–25 taps")
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.out, 0o700)
    session = json.loads(args.session.read_text())
    with httpx.Client(base_url=session["url"], headers={"X-OpenLolo-Client": "cli"}, timeout=30) as c:
        c.cookies.set("openlolo_session", session["session"])
        lease = None

        def post(path, payload):
            r = c.post("/api/" + path, json=payload)
            r.raise_for_status()
            return r.json()

        def control(name):
            return post("control/" + name, {"operation_id": str(uuid4()), "lease_token": lease})

        def wait(operation):
            while operation["state"] in ("QUEUED", "DISPATCHED"):
                time.sleep(0.1)
                r = c.get("/api/operations/" + operation["id"])
                r.raise_for_status()
                operation = r.json()
            if operation["state"] != "SUCCEEDED":
                raise RuntimeError(json.dumps(operation))
            return operation["result"]

        def setup(kind, payload=None):
            return wait(
                post(
                    "setup/" + kind,
                    {"operation_id": str(uuid4()), "lease_token": lease, "payload": payload or {}},
                )
            )

        timings = []
        last_capture = 0

        def capture():
            nonlocal last_capture
            start = time.monotonic()
            result = post("screenshot", {})
            result.pop("image")
            timings.append(time.monotonic() - start)
            last_capture = time.monotonic()
            return result["metadata"]

        lease = control("acquire")["control"]["lease_token"]
        # Acquiring an already-owned lease does not extend its remaining lifetime.
        control("renew")
        control("resume")
        started = time.monotonic()
        last_renew = started

        def renew():
            nonlocal last_renew
            if time.monotonic() - last_renew > 15:
                control("renew")
                last_renew = time.monotonic()

        report = {
            "started_epoch": time.time(),
            "completed": False,
            "requested_grid_trials": args.trials,
            "requested_minutes": args.minutes,
            "capture_every_grid_taps": args.capture_every,
            "source_age_seconds": None,
            "freshness_gate": "not measured",
        }
        log = (args.out / "observations.jsonl").open("x")
        try:
            frame = capture()
            calibration = setup(
                "calibration_start",
                {
                    "native_width": frame["native_width"],
                    "native_height": frame["native_height"],
                    "viewport_top": args.viewport_top,
                    "duration": 7200,
                    "grid_trials": args.trials,
                },
            )
            print("Open on the bound phone: " + calibration["url"], flush=True)
            print("Waiting up to 120 seconds for page geometry.", flush=True)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                renew()
                state = c.get("/api/calibration").json()
                if state.get("geometry"):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Calibration page not opened")
            for i in range(14 + args.trials):
                renew()
                # Keep the frame reference fresh even when feedback/network is slow.
                if time.monotonic() - last_capture > 30:
                    frame = capture()
                setup("calibration_next")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    state = c.get("/api/calibration").json()
                    if state.get("samples", 0) > i:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Missing phone feedback; trial not scored")
                captured = i < 14 or (i - 13) % args.capture_every == 0 or i == 13 + args.trials
                if captured:
                    frame = capture()
                else:
                    # Let the phone render the next target between successive taps.
                    time.sleep(0.35)
                log.write(
                    json.dumps(
                        {"trial": i, "frame": frame, "captured_after_trial": captured, "control": state}
                    )
                    + "\n"
                )
                log.flush()
                if i % 25 == 0:
                    print(f"{i + 1}/{14 + args.trials} trials", flush=True)
            report["calibration"] = setup("calibration_finish", {"session": calibration["session"]})
            while time.monotonic() - started < args.minutes * 60:
                renew()
                frame = capture()
                health = c.get("/api/status").json()
                log.write(
                    json.dumps({"elapsed": time.monotonic() - started, "frame": frame, "health": health})
                    + "\n"
                )
                log.flush()
                time.sleep(3)
            report["completed"] = True
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            report["elapsed_seconds"] = time.monotonic() - started
            report["captures"] = len(timings)
            if timings:
                report["screenshot_p95_seconds"] = sorted(timings)[
                    max(0, int(len(timings) * 0.95 + 0.999) - 1)
                ]
                report["screenshot_mean_seconds"] = statistics.mean(timings)
                report["latency_target_met"] = report["screenshot_p95_seconds"] <= 2.5
            report["owner_no_trust_prompt_confirmation"] = "pending"
            (args.out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
            log.close()
            try:
                control("pause")
                control("release")
            except Exception:
                pass


if __name__ == "__main__":
    main()
