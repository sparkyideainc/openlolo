#!/bin/sh
# Root helper behind openlolo-setup-ap.service and openlolo-wifi-scan.service.
# The API (user openlolo) writes hostapd.conf and setup-dnsmasq.conf under /run/openlolo and
# starts the units through polkit; this script owns the interface handoff with NetworkManager.
set -eu
run=/run/openlolo
conf=$run/hostapd.conf
interface=$(sed -n 's/^interface=//p' "$conf" | head -1)
[ -n "$interface" ] || { echo "no interface in $conf" >&2; exit 1; }
address=192.168.4.1/24

case "${1:-}" in
  start)
    # NetworkManager must let go of the radio (and take wpa_supplicant off it) before hostapd
    # can own it. The device shows up as unmanaged once that is done.
    nmcli device set "$interface" managed no
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ "$(nmcli -g GENERAL.STATE device show "$interface" 2>/dev/null | cut -d' ' -f1)" = 10 ] && break
      sleep 0.5
    done
    ip link set "$interface" up
    ip addr flush dev "$interface"
    ip addr add "$address" dev "$interface"
    dnsmasq --conf-file="$run/setup-dnsmasq.conf" --pid-file="$run/setup-dnsmasq.pid"
    exec hostapd "$conf"
    ;;
  stop)
    [ ! -f "$run/setup-dnsmasq.pid" ] || { kill "$(cat "$run/setup-dnsmasq.pid")" 2>/dev/null || true; rm -f "$run/setup-dnsmasq.pid"; }
    ip addr flush dev "$interface" 2>/dev/null || true
    nmcli device set "$interface" managed yes
    ;;
  scan)
    # Station-mode scans while hostapd holds the radio; brcmfmac scans from an AP interface.
    iw dev "$interface" scan > "$run/wifi-scan.txt.tmp"
    mv "$run/wifi-scan.txt.tmp" "$run/wifi-scan.txt"
    ;;
  *)
    echo "usage: $0 start|stop|scan" >&2
    exit 2
    ;;
esac
