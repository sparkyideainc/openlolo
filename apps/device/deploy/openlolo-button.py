#!/usr/bin/python3
"""Physical setup button: hold for five seconds to open local Wi-Fi setup mode.

Runs as its own systemd unit with GPIO access so the API unit keeps PrivateDevices=yes. The
only side effect is touching a flag file the API's provisioning supervisor watches.
"""

import os
import signal
import sys
import time
from pathlib import Path

FLAG = Path(os.environ.get("OPENLOLO_BUTTON_FLAG", "/run/openlolo/setup-button"))
PIN = int(os.environ.get("OPENLOLO_BUTTON_PIN", "17"))
HOLD = float(os.environ.get("OPENLOLO_BUTTON_HOLD", "5"))


def held() -> None:
    FLAG.touch()
    os.utime(FLAG, None)
    print(f"setup button held {HOLD:.0f}s; flag {FLAG} touched", flush=True)


def main() -> int:
    try:
        from gpiozero import Button
    except ImportError:
        print("gpiozero is not installed; setup button disabled", file=sys.stderr)
        return 0
    button = Button(PIN, pull_up=True, hold_time=HOLD)
    button.when_held = held
    stop = False

    def quit(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, quit)
    signal.signal(signal.SIGINT, quit)
    while not stop:
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
