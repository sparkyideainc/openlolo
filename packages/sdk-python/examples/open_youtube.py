#!/usr/bin/env python3
"""Open YouTube on the iPhone behind an OpenLolo box and save what the screen shows.

    python examples/open_youtube.py openlolo-d8a4a226          # LAN: the box name from its card
    python examples/open_youtube.py https://<box>.ts.net       # remote entry

Connecting a script to a box for the first time (one time per machine and box):

    $ python examples/open_youtube.py openlolo-d8a4a226
    Connecting to https://openlolo-d8a4a226.local:8443 ...
    OpenLolo: approve 'open_youtube.py' on the box page (Access). Code GZMG-QDXT.
      http://openlolo-d8a4a226.local/access?r=...
      For unattended scripts choose 'Until I remove it'.
      Box certificate SHA-256 3ea0c204ec2c175b... is pinned once you approve.

    Plug the iPhone into the box, open the box page on it, find 'open_youtube.py' under
    Access, check that the code matches, pick how long it may stay connected, and tap Approve.
    The script continues by itself:

    OpenLolo: approved.
    Connected; box certificate 3ea0c204ec2c175b...

The grant is saved in ~/.config/openlolo/sdk/ and refreshed by itself, so later runs connect
without asking. Remove the script under Access on the box page to cut it off; delete its file
in ~/.config/openlolo/sdk/ to connect it again from scratch.
"""

import sys

from openlolo_sdk import ApprovalRequired, OpenLoloError, Phone

YOUTUBE = "com.google.ios.youtube"


def main(box: str) -> int:
    with Phone(box, name="open_youtube.py") as phone:
        # 1. Connect. With no saved grant this asks the owner to approve on the box page and
        #    waits (up to 10 minutes); with one, it only refreshes the token. Without connect()
        #    the first phone call below would do the same.
        print(f"{'Reconnecting' if phone.approved else 'Connecting'} to {phone.url} ...")
        try:
            phone.connect()
        except ApprovalRequired:
            # No terminal (cron, CI): the SDK never waits for approval there. Run once by hand.
            print("Not approved yet: run this once in a terminal and approve it on the box page.")
            return 2
        pinned = f"; box certificate {phone.fingerprint[:16]}..." if phone.fingerprint else ""
        print(f"Connected{pinned}")

        # 2. Find the app on the box: launching takes the exact bundle id.
        apps = phone.apps("youtube")
        if not any(app["bundle_id"] == YOUTUBE for app in apps):
            found = ", ".join(f"{app['name']} ({app['bundle_id']})" for app in apps) or "nothing"
            print(f"YouTube is not installed; a search for 'youtube' found {found}.", file=sys.stderr)
            return 1

        # 3. Open it. The box waits for it to draw and returns the new screen.
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
