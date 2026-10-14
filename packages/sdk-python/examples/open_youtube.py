#!/usr/bin/env python3
"""Open YouTube on the iPhone behind an OpenLolo box and save what the screen shows.

    python examples/open_youtube.py openlolo-d8a4a226          # LAN, box name from its card
    python examples/open_youtube.py https://<box>.ts.net       # remote entry

The first run prints an approval request: open the box page on the iPhone, go to Access and
approve it. Later runs reuse the cached grant.
"""

import sys

from openlolo_sdk import OpenLoloError, Phone

YOUTUBE = "com.google.ios.youtube"


def main(box: str) -> int:
    with Phone(box, name="open_youtube.py") as phone:
        # Find the app on the box first: launching takes the exact bundle id.
        apps = phone.apps("youtube")
        if not any(app["bundle_id"] == YOUTUBE for app in apps):
            found = ", ".join(f"{app['name']} ({app['bundle_id']})" for app in apps) or "nothing"
            print(f"YouTube is not installed; a search for 'youtube' found {found}.", file=sys.stderr)
            return 1

        # Foreground YouTube; the box waits for it to draw and returns the new screen.
        phone.open_app(YOUTUBE)
        frame = phone.frame or phone.screenshot()
        path = frame.save("youtube.jpg")
        print(f"YouTube is open ({frame.width}x{frame.height}); screen saved to {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        sys.exit(main(sys.argv[1]))
    except OpenLoloError as exc:
        sys.exit(f"OpenLolo: {exc}")
