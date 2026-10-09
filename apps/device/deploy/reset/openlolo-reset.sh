#!/bin/sh
# Root helper behind openlolo-reset.service: put the box back to first-time setup.
# Started by the API through polkit after the owner typed the box's name. Keeps the board id,
# the setup card's Wi-Fi password and the LAN certificate; everything the owner can redo goes.
set -u
state=/var/lib/openlolo

systemctl stop openlolo-api.service openlolo-device.service openlolo-setup-ap.service 2>/dev/null || true
systemctl stop usbmuxd.service 2>/dev/null || true

# Wi-Fi profiles the box created (openlolo-<network>); nothing an installer added by hand.
nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2 == "802-11-wireless" && $1 ~ /^openlolo-/ { print $1 }' |
  while IFS= read -r name; do nmcli connection delete "$name" >/dev/null 2>&1 || true; done

# The bound phone, its pairing and trust records (ours and usbmuxd's), AI clients and the journal.
rm -f "$state/binding.json" "$state/openlolo.sqlite3" "$state/openlolo.sqlite3-shm" "$state/openlolo.sqlite3-wal"
rm -f "$state"/device/lockdown/*.plist "$state"/device/pymobiledevice3/*.plist
rm -f /var/lib/lockdown/*.plist

systemctl start usbmuxd.service
systemctl start openlolo-device.service openlolo-api.service
