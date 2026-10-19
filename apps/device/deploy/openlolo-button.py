#!/usr/bin/python3
"""Physical buttons of the box, in their own systemd unit so the API unit keeps PrivateDevices=yes.

OPENLOLO_BUTTON_BACKEND picks the hardware:
- ``gpio`` (default, Raspberry Pi): hold the button on BCM pin 17 for five seconds. The only
  side effect is touching the setup flag the API's provisioning supervisor watches.
- ``pisugar3`` (Lolo Pocket): PiSugar3 top button over I2C, status on the board LED; see
  ``openlolo_pisugar3.py`` next to this file.

The API decides what a hold means: setup mode, then a reset armed, then the reset confirmed.
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
    backend = os.environ.get("OPENLOLO_BUTTON_BACKEND", "gpio")
    if backend == "pisugar3":
        return pisugar3()
    if backend != "gpio":
        # Exit 0: Restart=on-failure must not loop on a configuration error.
        print(f"unknown OPENLOLO_BUTTON_BACKEND {backend!r}; buttons disabled", file=sys.stderr)
        return 0
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


def pisugar3() -> int:
    import openlolo_pisugar3

    stop = False

    def quit(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, quit)
    signal.signal(signal.SIGINT, quit)
    return openlolo_pisugar3.main(lambda: stop)


if __name__ == "__main__":
    raise SystemExit(main())
