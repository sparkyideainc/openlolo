#!/bin/sh
# Move Armbian's Wi-Fi from netplan/systemd-networkd to plain NetworkManager profiles.
# OpenLolo drives Wi-Fi through nmcli (saved networks, scans, joins, setup mode); while netplan
# renders wlan0 for networkd, NetworkManager reports it "unmanaged" and the box thinks no network
# is saved. Run detached by install.sh (systemd-run) so a dropped SSH session cannot stop it
# halfway. Each netplan access point becomes an NM keyfile; if no default gateway answers within
# 45 s the netplan files are put back and networkd takes wlan0 again. On success networkd and the
# netplan packages are removed. Safe to run again: with no netplan Wi-Fi left it does nothing.
set -u
# install.sh stops openlolo-api first: with polkit in place it would otherwise open the setup
# access point (no saved network yet) and take the radio in the middle of the move.
trap 'systemctl start openlolo-api.service 2>/dev/null' EXIT
log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
netplan_dir=/etc/netplan
backup=/etc/openlolo/netplan-backup
[ -d "$netplan_dir" ] || { log 'no /etc/netplan; nothing to do'; exit 0; }
wifi_files=$(grep -l -E '^[[:space:]]*wifis:' "$netplan_dir"/*.yaml 2>/dev/null || true)
[ -n "$wifi_files" ] || { log 'no netplan Wi-Fi; nothing to do'; exit 0; }

install -d -m 0700 "$backup"
cp -a "$netplan_dir/." "$backup/"
# One keyfile per access point. python3-yaml ships with Armbian (armbian-config needs it).
created=$(python3 - "$netplan_dir" <<'PY'
import glob, os, sys, uuid, yaml
names = []
for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.yaml"))):
    with open(path) as handle:
        doc = yaml.safe_load(handle) or {}
    for ifname, wifi in ((doc.get("network") or {}).get("wifis") or {}).items():
        for ssid, ap in ((wifi or {}).get("access-points") or {}).items():
            ssid, password = str(ssid), str((ap or {}).get("password") or "")
            name = "".join(c if c.isalnum() or c in "-_." else "_" for c in ssid) or "wifi"
            lines = ["[connection]", f"id={ssid}", f"uuid={uuid.uuid4()}", "type=wifi",
                     f"interface-name={ifname}", "autoconnect=true",
                     # 0 = keep retrying; the default gives up after 4 failures.
                     "autoconnect-retries=0", "",
                     "[wifi]", "mode=infrastructure", f"ssid={ssid}", ""]
            if password:
                lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={password}", ""]
            lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
            target = f"/etc/NetworkManager/system-connections/{name}.nmconnection"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write("\n".join(lines))
            names.append(ssid)
print("\n".join(names))
PY
) || { log 'could not read the netplan Wi-Fi settings; left unchanged'; exit 1; }
[ -n "$created" ] || { log 'netplan Wi-Fi has no access points; left unchanged'; exit 0; }
log "NetworkManager profiles written for: $(printf '%s' "$created" | tr '\n' ' ')"

restore() {
  log 'no gateway on NetworkManager within 45 s; restoring netplan'
  printf '%s\n' "$created" | while IFS= read -r ssid; do nmcli connection delete id "$ssid" >/dev/null 2>&1; done
  cp -a "$backup/." "$netplan_dir/"
  netplan generate
  systemctl enable --now systemd-networkd.service systemd-networkd.socket 2>/dev/null
  systemctl restart NetworkManager
  netplan apply
  log 'netplan restored'
  exit 1
}

for file in $wifi_files; do mv "$file" "$file.openlolo-disabled"; done
netplan generate
systemctl stop 'netplan-wpa-*.service' 2>/dev/null
systemctl restart NetworkManager
sleep 2
nmcli device set wlan0 managed yes 2>/dev/null
nmcli connection reload
start=$(date +%s)
while [ $(( $(date +%s) - start )) -lt 45 ]; do
  gateway=$(ip -4 route show default | awk '{print $3; exit}')
  if [ -n "$gateway" ] && nmcli -t -f TYPE,STATE device | grep -q '^wifi:connected$' \
     && ping -c1 -W2 "$gateway" >/dev/null 2>&1; then
    log "connected through NetworkManager (gateway $gateway)"
    rm -f "$netplan_dir"/*.openlolo-disabled
    # NetworkManager now owns every interface (it picks up wired ports by itself), so networkd and
    # the netplan generator have nothing left to do.
    systemctl disable --now systemd-networkd.service systemd-networkd.socket \
      systemd-networkd-wait-online.service 2>/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get purge -y netplan.io netplan-generator python3-netplan libnetplan1 >/dev/null 2>&1 ||
      log 'netplan packages could not be removed (not fatal)'
    rm -rf "$netplan_dir"
    log "done; netplan files kept in $backup"
    exit 0
  fi
  sleep 2
done
for file in $wifi_files; do mv "$file.openlolo-disabled" "$file"; done
restore
