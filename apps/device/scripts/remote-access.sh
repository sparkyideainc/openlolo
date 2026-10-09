#!/bin/sh
# Enable the remote MCP entry (ADR 0003) in front of the loopback gateway. Idempotent; run on
# the Pi as root.
#
#   sudo sh scripts/remote-access.sh [tailscale] [--auth-key tskey-auth-...] [--port 8090]
#   sudo sh scripts/remote-access.sh cloudflare [--hostname box.example.com] [--port 8090]
#   sudo sh scripts/remote-access.sh cloudflare --hostname box.example.com --token eyJ...
#
# tailscale (default): Tailscale Funnel. Without --auth-key (or TS_AUTHKEY in the environment)
# `tailscale up` prints a login URL and QR code and the owner approves the box in their tailnet;
# the box is then also reachable over the tailnet for SSH support. Known issue: the Funnel
# ingress has lost the node's registration after interface churn on the Pi (Wi-Fi to Ethernet,
# hotspot changes) and needed `systemctl restart tailscaled` to come back.
#
# cloudflare (optional): a named Cloudflare Tunnel. Without --token (or TUNNEL_TOKEN in the
# environment) the script logs the box in to your Cloudflare account through a browser URL (you
# pick the domain there), creates a tunnel named after the box, and routes
# openlolo-<board id>.<that domain> to it (or --hostname if given), run from the openlolo-tunnel
# systemd unit. With a token from a tunnel created in the Zero Trust dashboard (public hostname
# -> http://localhost:8090 configured there) the box joins unattended; --hostname is then
# required because the token does not reveal it. Known issue: cloudflared's HTTP/2 edge
# connections stay bound to the address they were opened from; after an interface change they
# go silent until cloudflared's own timeout or a unit restart.
#
# Neither provider's behaviour across network changes is settled; see ADR 0003, amendment
# 2026-09-30.
#
# Secrets (tunnel token, auth key) are passed to the tunnel client only; the Cloudflare token
# is stored in /etc/openlolo/tunnel.env (mode 0600) because the unit needs it on every start.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
usage='Usage: remote-access.sh [tailscale [--auth-key KEY] | cloudflare [--hostname HOST] [--token TOKEN]] [--port PORT]'
provider=tailscale
case "${1:-}" in
  tailscale|cloudflare) provider=$1; shift ;;
  --*|'') ;;
  *) echo "Unknown provider: $1" >&2; echo "$usage" >&2; exit 1 ;;
esac
port=8090
auth_key=${TS_AUTHKEY:-}
token=${TUNNEL_TOKEN:-}
hostname=
while [ $# -gt 0 ]; do
  case "$1" in
    --auth-key) auth_key=${2:?--auth-key needs a value}; shift 2 ;;
    --token) token=${2:?--token needs a value}; shift 2 ;;
    --hostname) hostname=${2:?--hostname needs a value}; shift 2 ;;
    --port) port=${2:?--port needs a value}; shift 2 ;;
    *) echo "Unknown option: $1" >&2; echo "$usage" >&2; exit 1 ;;
  esac
done
case "$port" in ''|*[!0-9]*) echo 'Port must be numeric' >&2; exit 1 ;; esac
config=/etc/openlolo/box.toml
[ -f "$config" ] || { echo "Missing $config; install OpenLolo first" >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Write public_url/mcp_port into box.toml and restart the API so the gateway listener opens.
configure_box() {
  tmp=$(mktemp)
  grep -vE '^[[:space:]]*(public_url|mcp_port)[[:space:]]*=' "$config" > "$tmp"
  printf 'public_url = "https://%s"\nmcp_port = %s\n' "$1" "$port" >> "$tmp"
  install -m 0644 "$tmp" "$config"
  rm -f "$tmp"
  systemctl restart openlolo-api
}

# Same board id the installer uses for the LAN name and the setup network name.
board_id=$(awk '/^Serial/ {print tolower(substr($3, length($3)-3))}' /proc/cpuinfo 2>/dev/null || true)
[ -n "$board_id" ] || board_id=$(sha256sum /etc/machine-id | cut -c1-4)

case "$provider" in
cloudflare)
  normalize_hostname() {
    hostname=$(printf '%s' "$hostname" | tr 'A-Z' 'a-z')
    hostname=${hostname#https://}; hostname=${hostname%%/*}
    case "$hostname" in
      *[!a-z0-9.-]*|*..*|.*|-*|*.|*-|*.-*|*-.*) echo "Invalid hostname: $hostname" >&2; exit 1 ;;
      *.*) ;;
      *) echo "Hostname must be fully qualified: $hostname" >&2; exit 1 ;;
    esac
  }
  # The zone picked during `cloudflared tunnel login` is recorded in cert.pem (zone id plus a
  # scoped API token); its name gives the default hostname openlolo-<board id>.<zone>.
  zone_from_login() {
    python3 - <<'EOF_PY'
import base64, json, re, sys, urllib.request
try:
    pem = open("/root/.cloudflared/cert.pem").read()
    body = re.search(r"-----BEGIN ARGO TUNNEL TOKEN-----\n(.*?)\n-----END", pem, re.S).group(1)
    cert = json.loads(base64.b64decode("".join(body.split())))
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/zones/" + cert["zoneID"],
        headers={"Authorization": "Bearer " + cert["apiToken"]},
    )
    print(json.load(urllib.request.urlopen(req, timeout=15))["result"]["name"])
except Exception as exc:  # any failure: the caller asks for --hostname
    print("zone lookup failed: %s" % exc, file=sys.stderr)
EOF_PY
  }
  if [ -n "$token" ] && [ -z "$hostname" ]; then
    echo '--hostname is required with --token: the token does not reveal the public hostname configured in the dashboard' >&2
    exit 1
  fi
  [ -z "$hostname" ] || normalize_hostname

  if ! command -v cloudflared >/dev/null 2>&1; then
    echo 'Installing cloudflared from pkg.cloudflare.com...'
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      > /etc/apt/sources.list.d/cloudflared.list
    apt-get update
    apt-get install -y cloudflared
  fi

  install -d -m 0700 /etc/openlolo/cloudflared
  if [ -n "$token" ]; then
    echo 'Using the dashboard-managed tunnel token. Make sure its public hostname points at'
    echo "http://localhost:$port in the Zero Trust dashboard."
    umask 077
    printf 'TUNNEL_TOKEN=%s\n' "$token" > /etc/openlolo/tunnel.env
    umask 022
    printf '# Dashboard-managed tunnel: ingress lives in Cloudflare Zero Trust; the token is in tunnel.env.\nno-autoupdate: true\nprotocol: http2\n' \
      > /etc/openlolo/cloudflared.yml
  else
    rm -f /etc/openlolo/tunnel.env
    if [ ! -s /root/.cloudflared/cert.pem ]; then
      echo 'Open the URL below in a browser, log in to Cloudflare and pick the zone that will host the hostname.'
      echo 'Waiting for the login...'
      cloudflared tunnel login
    fi
    name=openlolo-$board_id
    if [ -z "$hostname" ]; then
      zone=$(zone_from_login)
      [ -n "$zone" ] || { echo 'Could not read the zone from the Cloudflare login; rerun with --hostname box.example.com' >&2; exit 1; }
      hostname=$name.$zone
      normalize_hostname
      echo "Public hostname: $hostname (pass --hostname to choose another name in $zone)"
    fi
    creds=/etc/openlolo/cloudflared/$name.json
    tunnel_id() {
      cloudflared tunnel list --name "$name" --output json 2>/dev/null \
        | python3 -c 'import json,sys
try:
    tunnels = json.load(sys.stdin)
except Exception:
    tunnels = []
print(tunnels[0]["id"] if tunnels else "")'
    }
    id=$(tunnel_id)
    if [ -n "$id" ] && [ ! -s "$creds" ] && [ -s "/root/.cloudflared/$id.json" ]; then
      install -m 0600 "/root/.cloudflared/$id.json" "$creds"
    fi
    if [ -n "$id" ] && [ ! -s "$creds" ]; then
      echo "Tunnel $name exists but its credentials are gone; recreating it."
      cloudflared tunnel delete -f "$name"
      id=
    fi
    if [ -z "$id" ]; then
      cloudflared tunnel create --credentials-file "$creds" "$name"
      id=$(tunnel_id)
      [ -n "$id" ] || { echo 'Tunnel creation did not register; check `cloudflared tunnel list`' >&2; exit 1; }
      [ -s "$creds" ] || { [ -s "/root/.cloudflared/$id.json" ] && install -m 0600 "/root/.cloudflared/$id.json" "$creds"; }
      [ -s "$creds" ] || { echo "Credentials file $creds was not written" >&2; exit 1; }
    fi
    chmod 0600 "$creds"
    echo "Routing $hostname to tunnel $name ($id)..."
    cloudflared tunnel route dns --overwrite-dns "$name" "$hostname"
    cat > /etc/openlolo/cloudflared.yml <<EOF
# Written by scripts/remote-access.sh. The public hostname must equal public_url in box.toml;
# the gateway rejects every other Host (HOST_REJECTED). cloudflared forwards the original Host.
tunnel: $id
credentials-file: $creds
no-autoupdate: true
# HTTP/2 over TCP: QUIC (UDP 7844) times out behind some home NATs and shared connections
# (\"no recent network activity\"), which drops the door for ~15 s every few minutes.
protocol: http2
ingress:
  - hostname: $hostname
    service: http://127.0.0.1:$port
  - service: http_status:404
EOF
  fi
  chmod 0644 /etc/openlolo/cloudflared.yml

  # A box previously on Funnel would otherwise keep a second public door to the same port.
  if command -v tailscale >/dev/null 2>&1 && tailscale funnel status 2>/dev/null | grep -q "127.0.0.1:$port"; then
    echo 'Turning the old Tailscale Funnel off (Cloudflare Tunnel replaces it).'
    tailscale funnel --https=443 off || true
  fi
  unit=$source_dir/deploy/systemd/openlolo-tunnel.service
  [ -f /etc/systemd/system/openlolo-tunnel.service ] || [ ! -f "$unit" ] || install -m 0644 "$unit" /etc/systemd/system/
  [ -f /etc/systemd/system/openlolo-tunnel.service ] || { echo 'openlolo-tunnel.service missing; install the release first' >&2; exit 1; }
  systemctl daemon-reload
  systemctl enable openlolo-tunnel >/dev/null
  systemctl restart openlolo-tunnel

  configure_box "$hostname"
  # Verify through Cloudflare's own resolver (DNS over HTTPS) and connect to the returned edge
  # address: the LAN resolver may still hold a negative answer for the new name, and asking it
  # now would plant one for up to 30 minutes.
  echo 'Waiting for the tunnel to answer from the public side...'
  ok=
  for _ in $(seq 1 12); do
    sleep 5
    edge=$(curl -fsS --max-time 10 -H 'accept: application/dns-json' \
      "https://cloudflare-dns.com/dns-query?name=$hostname&type=A" 2>/dev/null \
      | python3 -c 'import json,sys
try:
    answers = json.load(sys.stdin).get("Answer") or []
    print(next(a["data"] for a in answers if a.get("type") == 1))
except Exception:
    pass')
    [ -n "$edge" ] || continue
    if curl -fsS --max-time 10 --resolve "$hostname:443:$edge" "https://$hostname/.well-known/oauth-authorization-server" 2>/dev/null \
      | grep -q "\"issuer\": *\"https://$hostname\""; then ok=1; break; fi
  done
  if [ -z "$ok" ]; then
    echo 'The public URL did not answer through Cloudflare yet. Tunnel side: journalctl -u openlolo-tunnel -n 30' >&2
  else
    echo 'Public URL verified through Cloudflare. Your own network may still answer NXDOMAIN for a few'
    echo 'minutes if the name was looked up before it existed; a phone on mobile data sees it now.'
  fi
  echo "Remote MCP entry: https://$hostname/mcp"
  ;;

tailscale)
  command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
  if ! tailscale status >/dev/null 2>&1; then
    if [ -n "$auth_key" ]; then
      echo 'Joining the tailnet with the provided auth key...'
      tailscale up --auth-key "$auth_key"
    else
      echo 'Open the URL below (or scan the QR code) and approve this Pi. Waiting for login...'
      tailscale up --qr
    fi
  fi
  # Funnel needs HTTPS certificates and the funnel node attribute on the tailnet. On first use
  # Tailscale prints a one-click enable link and waits for it to be applied; older releases exit
  # instead, so retry after the owner confirms.
  echo 'Enabling Funnel. If Tailscale shows an enable link, open it and approve; this waits.'
  until tailscale funnel --bg --https=443 "http://127.0.0.1:$port"; do
    echo 'Funnel is not enabled for this tailnet yet. Enable it via the link above, then press Enter.' >&2
    read -r _ </dev/tty || { echo 'No terminal to wait on; rerun after enabling Funnel' >&2; exit 1; }
  done
  hostname=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
  [ -n "$hostname" ] || { echo 'Could not determine the tailnet hostname' >&2; exit 1; }
  if systemctl is-enabled openlolo-tunnel >/dev/null 2>&1; then
    echo 'Stopping the Cloudflare Tunnel unit (Funnel replaces it).'
    systemctl disable --now openlolo-tunnel || true
  fi
  configure_box "$hostname"
  echo "Remote MCP entry: https://$hostname/mcp"
  echo 'Note: after a network change on the Pi, Funnel may need `sudo systemctl restart tailscaled` (see the runbook).'
  ;;
esac

echo "Approve clients from the OpenLolo app (AI clients tab, QR on the consent page, or a PIN)."
echo "Fallback without the app: sudo -u openlolo /opt/openlolo/current/.venv/bin/openlolo credential"
