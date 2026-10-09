#!/bin/sh
# Versioned Debian/Pi installation. Existing config, trust records and NM profiles are preserved.
set -eu
umask 022
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
machine_arch=$(dpkg --print-architecture 2>/dev/null || true)
[ "$machine_arch" = arm64 ] || {
  echo "OpenLolo requires Debian ARM64 (aarch64); found ${machine_arch:-unknown}." >&2
  exit 1
}
. /etc/os-release
case "${ID:-}:${VERSION_CODENAME:-}" in
  debian:trixie|raspios:trixie|raspbian:trixie) ;;
  *) echo "Supported systems are Debian 13 ARM64 and Raspberry Pi OS 64-bit (trixie); found ${PRETTY_NAME:-unknown}." >&2; exit 1 ;;
esac
usage='Usage: install.sh [source-directory] [--remote tailscale|cloudflare | --no-remote] [--auth-key KEY] [--hostname HOST] [--tunnel-token TOKEN] [--wifi-country CC]'
source_dir=
remote=tailscale   # Remote MCP entry (ADR 0003) is part of the standard install; --no-remote for LAN-only.
auth_key=${TS_AUTHKEY:-}           # Tailscale auth key (unattended tailnet join).
hostname=${OPENLOLO_HOSTNAME:-}   # Cloudflare only (--remote cloudflare): public DNS name; default openlolo-<board id>.<zone>.
tunnel_token=${TUNNEL_TOKEN:-}     # Cloudflare only: dashboard-managed tunnel token (unattended).
wifi_country=${WIFI_COUNTRY:-US}
while [ $# -gt 0 ]; do
  case "$1" in
    --wifi-country) wifi_country=${2:?--wifi-country needs a two-letter code}; shift 2 ;;
    --remote) remote=${2:?--remote needs a provider (tailscale or cloudflare)}; shift 2 ;;
    --no-remote) remote=; shift ;;
    --hostname) hostname=${2:?--hostname needs a value}; shift 2 ;;
    --tunnel-token) tunnel_token=${2:?--tunnel-token needs a value}; shift 2 ;;
    --auth-key) auth_key=${2:?--auth-key needs a value}; shift 2 ;;
    --*) echo "Unknown option: $1" >&2; echo "$usage" >&2; exit 1 ;;
    *) [ -z "$source_dir" ] || { echo "$usage" >&2; exit 1; }; source_dir=$1; shift ;;
  esac
done
[ -n "$source_dir" ] || source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$remote" in ''|tailscale|cloudflare) ;; *) echo "Unsupported --remote provider: $remote" >&2; exit 1 ;; esac
# The version is the GitHub release tag stamped by scripts/package.sh; a plain checkout is a dev build.
release=$(sed -n 's/^release=//p' "$source_dir/RELEASE" 2>/dev/null | head -1)
[ -n "$release" ] || release=dev-$(date -u +%Y%m%d%H%M)
root=/opt/openlolo
# Build in a staging directory and swap it into place only after the import checks pass.
# Older versions are not kept on the box: GitHub Releases hold every tagged build.
release_dir=$root/.staging
rm -rf "$release_dir"
apt-get update
awk '!/^#/ && NF' "$source_dir/deploy/os-packages.txt" | xargs apt-get install -y
# GPIO button support is a Raspberry Pi hardware extension; generic ARM64 boxes do not need it.
if grep -qi 'Raspberry Pi' /proc/device-tree/model 2>/dev/null; then
  apt-get install -y python3-gpiozero python3-lgpio
fi
getent group openlolo >/dev/null || groupadd --system openlolo
for account in openlolo openlolo-device; do
  id "$account" >/dev/null 2>&1 || useradd --system --gid openlolo --home-dir /var/lib/openlolo --shell /usr/sbin/nologin "$account"
done
install -d -m 0755 "$root" "$release_dir" /etc/openlolo
install -d -o openlolo -g openlolo -m 0750 /var/lib/openlolo
# Pairing records, DDI cache and lockdown trust cache for the worker user only.
install -d -o openlolo-device -g openlolo -m 0700 /var/lib/openlolo/device
install -d -o openlolo -g openlolo -m 0770 /run/openlolo
printf 'd /run/openlolo 0770 openlolo openlolo -\n' > /etc/tmpfiles.d/openlolo.conf
cp -R "$source_dir/src" "$source_dir/pyproject.toml" "$source_dir/uv.lock" "$source_dir/deploy" "$release_dir/"
[ ! -f "$source_dir/RELEASE" ] || cp "$source_dir/RELEASE" "$release_dir/"   # commit stamp written by scripts/package.sh
python3 -m venv "$release_dir/.tools"
"$release_dir/.tools/bin/pip" install 'uv==0.12.19'
uv_bin=$release_dir/.tools/bin/uv
cd "$release_dir"
# API venv: the mcp extra serves the optional remote MCP entry (public_url); it is small and always installed.
# --relocatable: the tree is renamed from .staging to current after the checks, so entry-point
# shebangs must not embed the build path.
"$uv_bin" venv --relocatable --python /usr/bin/python3 .venv
# --no-editable: uv would otherwise point site-packages at the build path through a .pth file.
"$uv_bin" sync --frozen --no-dev --no-editable --extra mcp --python /usr/bin/python3
# Worker venv: pymobiledevice3 (GPL-3.0) and its dependencies are isolated here, resolved from the same uv.lock.
"$uv_bin" venv --relocatable --python /usr/bin/python3 .device-venv
UV_PROJECT_ENVIRONMENT="$release_dir/.device-venv" "$uv_bin" sync --frozen --no-dev --no-editable --extra device --python /usr/bin/python3
# Validate imports before touching the active release.
.venv/bin/python -c 'import openlolo.interfaces.cli'
.device-venv/bin/python -c 'import openlolo.coredevice.worker; import pymobiledevice3.remote.core_device.hid_service, pymobiledevice3.remote.userspace_tunnel'
[ -e /etc/openlolo/box.toml ] || install -m 0644 "$source_dir/config/box.example.toml" /etc/openlolo/box.toml
install -m 0644 deploy/systemd/openlolo-*.service /etc/systemd/system/

# Wi-Fi access-point setup and station mode need a configured regulatory country.
case "$wifi_country" in [A-Z][A-Z]) ;; *) echo 'Wi-Fi country must be a two-letter code, e.g. US' >&2; exit 1 ;; esac
if command -v raspi-config >/dev/null 2>&1; then raspi-config nonint do_wifi_country "$wifi_country" || true; fi
command -v iw >/dev/null 2>&1 && iw reg set "$wifi_country" 2>/dev/null || true
command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on || true
# The setup access point is hostapd under openlolo-setup-ap.service (ADR 0011 amendment);
# NetworkManager keeps station mode only. Drop the SoftAP profile and captive DNS rule earlier
# releases created, and keep Debian's stock hostapd unit from grabbing the radio at boot.
systemctl stop openlolo-setup-ap.service 2>/dev/null || true
nmcli connection delete openlolo-setup >/dev/null 2>&1 || true
rm -f /etc/NetworkManager/dnsmasq-shared.d/openlolo-captive.conf
systemctl disable --now hostapd.service 2>/dev/null || true
systemctl mask hostapd.service 2>/dev/null || true

# Optional LAN MCP entry: box id, self-signed certificate, lan_url, mDNS and polkit.
# The certificate names only the .local host; add IPs via lan_extra_hosts when needed.
board_id=$(awk '/^Serial/ {print tolower(substr($3, length($3)-7))}' /proc/cpuinfo 2>/dev/null || true)
[ -n "$board_id" ] || board_id=$(sha256sum /etc/machine-id | cut -c1-8)
lan_name=openlolo-$board_id
lan_port=8443
if ! grep -qE '^[[:space:]]*lan_url[[:space:]]*=' /etc/openlolo/box.toml; then
  printf '\n# Optional LAN MCP entry.\nlan_url = "https://%s.local:%s"\n' "$lan_name" "$lan_port" >> /etc/openlolo/box.toml
fi
# The setup AP name and local hostname share this per-box suffix.
if ! grep -qE '^[[:space:]]*setup_name[[:space:]]*=' /etc/openlolo/box.toml; then
  printf 'setup_name = "OpenLolo-%s"\n' "$(printf '%s' "$board_id" | tr 'a-z' 'A-Z')" >> /etc/openlolo/box.toml
fi
lan_url=$(sed -nE 's/^[[:space:]]*lan_url[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' /etc/openlolo/box.toml | tail -1)
lan_host=${lan_url#https://}; lan_host=${lan_host%%/*}; lan_port=${lan_host##*:}; lan_host=${lan_host%%:*}
[ "$lan_port" != "$lan_host" ] || lan_port=443
install -d -o openlolo -g openlolo -m 0700 /var/lib/openlolo/tls
if [ ! -s /var/lib/openlolo/tls/lan.key ] || [ ! -s /var/lib/openlolo/tls/lan.crt ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=$lan_host" -addext "subjectAltName=DNS:$lan_host" \
    -keyout /var/lib/openlolo/tls/lan.key -out /var/lib/openlolo/tls/lan.crt 2>/dev/null
  chown openlolo:openlolo /var/lib/openlolo/tls/lan.key /var/lib/openlolo/tls/lan.crt
  chmod 0600 /var/lib/openlolo/tls/lan.key; chmod 0644 /var/lib/openlolo/tls/lan.crt
fi
fingerprint=$(openssl x509 -in /var/lib/openlolo/tls/lan.crt -noout -fingerprint -sha256 | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f')
sed -e "s/@PORT@/$lan_port/" -e "s/@ID@/$board_id/" -e "s/@VER@/$release/" -e "s/@FP@/$fingerprint/" \
  deploy/avahi/openlolo.service > /etc/avahi/services/openlolo.service
if grep -qE '^#?host-name=' /etc/avahi/avahi-daemon.conf; then
  sed -i -E "s/^#?host-name=.*/host-name=${lan_host%.local}/" /etc/avahi/avahi-daemon.conf
else
  sed -i -E "s/^\[server\]/[server]\nhost-name=${lan_host%.local}/" /etc/avahi/avahi-daemon.conf
fi
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 deploy/polkit/50-openlolo.rules /etc/polkit-1/rules.d/50-openlolo.rules
systemctl enable --now avahi-daemon
systemctl restart avahi-daemon
# Remove the pre-CoreDevice units and the patched-usbmuxd drop-in if a previous release installed them.
for old in openlolo-hid openlolo-media; do
  systemctl disable --now "$old.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/$old.service"
done
if [ -e /etc/systemd/system/usbmuxd.service.d/openlolo.conf ]; then
  mv /etc/systemd/system/usbmuxd.service.d/openlolo.conf /etc/openlolo/usbmuxd-legacy-dropin.conf.disabled
fi
rm -f /etc/udev/rules.d/70-openlolo.rules /etc/dbus-1/system.d/openlolo.conf
systemctl stop openlolo-api.service openlolo-device.service 2>/dev/null || true
rm -rf "$root/current.old"
[ ! -e "$root/current" ] && [ ! -L "$root/current" ] || mv "$root/current" "$root/current.old"
mv "$release_dir" "$root/current"
# The entry points must run from their final path; a venv that still references .staging fails
# here, and the previous build is put back.
if ! "$root/current/.venv/bin/openlolo" --help >/dev/null 2>&1 \
   || ! "$root/current/.device-venv/bin/openlolo-device" --help >/dev/null 2>&1; then
  rm -rf "$root/current.failed"; mv "$root/current" "$root/current.failed"
  [ ! -e "$root/current.old" ] && [ ! -L "$root/current.old" ] || mv "$root/current.old" "$root/current"
  echo "New build does not run from $root/current (kept as current.failed); previous build restored." >&2
  exit 1
fi
# The pre-0.6 layout kept every release under releases/ behind a current symlink; drop it.
rm -rf "$root/current.old" "$root/releases" "$root/previous-release"
systemctl daemon-reload
systemctl restart usbmuxd
systemctl enable --now openlolo-device openlolo-api
if grep -qi 'Raspberry Pi' /proc/device-tree/model 2>/dev/null && [ -e /dev/gpiochip0 ]; then
  systemctl enable --now openlolo-button || true
fi
if ! grep -qw memory /sys/fs/cgroup/cgroup.controllers; then
  printf 'Memory cgroup controller absent: MemoryMax is configured but not enforced on this boot. See installation runbook.\n' >&2
fi
printf 'Installed %s. LAN entry %s (sha256:%s)\n' "$release" "$lan_url" "$fingerprint"
# Remote MCP entry (ADR 0003): Tailscale Funnel (default) or Cloudflare Tunnel in front of the
# loopback gateway. Tailscale: a login URL and QR code, the owner approves the box in their
# tailnet. Cloudflare: a browser login where the owner picks their domain; the box then takes
# openlolo-<board id>.<domain> unless --hostname names another. A failure here leaves a working
# LAN-only box. Neither provider is known to survive an interface change on the box without a
# restart of its daemon; that behaviour is under evaluation (ADR 0003, amendment 2026-09-30).
if [ "$remote" = cloudflare ] && [ -n "$tunnel_token" ] && [ -z "$hostname" ]; then
  printf -- '--tunnel-token needs --hostname (the dashboard tunnel does not reveal its hostname); remote access skipped.\n' >&2
  remote=
fi
remote_failed=
case "$remote" in
  tailscale)
    printf '\nStep 1 of 2: remote access through Tailscale Funnel. Approve this box in your Tailscale account when asked.\n'
    if [ -n "$auth_key" ]; then
      sh "$source_dir/scripts/remote-access.sh" tailscale --auth-key "$auth_key" || remote_failed=1
    else
      sh "$source_dir/scripts/remote-access.sh" tailscale || remote_failed=1
    fi
    [ -z "$remote_failed" ] || printf 'Remote access was not enabled; the box works on the LAN. Retry later with: sudo sh %s/scripts/remote-access.sh\n' "$source_dir" >&2
    ;;
  cloudflare)
    printf '\nStep 1 of 2: remote access through Cloudflare Tunnel. Log in and pick your domain when asked.\n'
    set -- cloudflare
    [ -z "$hostname" ] || set -- "$@" --hostname "$hostname"
    [ -z "$tunnel_token" ] || set -- "$@" --token "$tunnel_token"
    sh "$source_dir/scripts/remote-access.sh" "$@" || remote_failed=1
    [ -z "$remote_failed" ] || printf 'Remote access was not enabled; the box works on the LAN. Retry later with: sudo sh %s/scripts/remote-access.sh cloudflare\n' "$source_dir" >&2
    ;;
esac
# The per-box Wi-Fi password is created on first start; print the setup card once ready.
for _ in $(seq 1 30); do
  [ -s /var/lib/openlolo/openlolo.sqlite3 ] && break
  sleep 1
done
printf '\nStep 2 of 2: setup Wi-Fi. Join the OpenLolo network from iPhone Wi-Fi Settings, then follow the local setup page.\n'
printf 'Connect the iPhone to the box over USB to pair it automatically; unlock the phone and tap Trust if prompted.\n'
printf '\nWi-Fi name, password, and iPhone join QR:\n'
sudo -u openlolo /opt/openlolo/current/.venv/bin/openlolo setup-card || \
  printf 'Label not ready yet; run: sudo -u openlolo /opt/openlolo/current/.venv/bin/openlolo setup-card\n'
printf 'Browser login with: sudo -u openlolo /opt/openlolo/current/.venv/bin/openlolo credential\n'
