#!/bin/sh
# Install the latest OpenLolo device release from GitHub on a Debian 13 / Raspberry Pi OS 64-bit box.
#
#   curl -fsSL https://raw.githubusercontent.com/sparkyideainc/openlolo/main/apps/device/install.sh | sudo sh
#   curl -fsSL .../install.sh | sudo sh -s -- --no-remote      # any apps/device/scripts/install.sh option after --
#   OPENLOLO_RELEASE=0.0.1 ... | sudo sh                        # pin a release instead of the latest
#   OPENLOLO_REPO=you/OpenLolo ... | sudo sh                    # install from a fork
#
# Downloads the release tarball built by .github/workflows/release.yml, verifies its sha256,
# unpacks it under /var/tmp/openlolo-install and runs the versioned installer
# (scripts/install.sh) from it.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root: curl ... | sudo sh' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo 'curl is required: apt-get install -y curl' >&2; exit 1; }
repo=${OPENLOLO_REPO:-sparkyideainc/openlolo}
release=${OPENLOLO_RELEASE:-}
if [ -n "$release" ]; then
  api=https://api.github.com/repos/$repo/releases/tags/v${release#v}
else
  api=https://api.github.com/repos/$repo/releases/latest
fi
json=$(curl -fsSL -H 'Accept: application/vnd.github+json' "$api") || { echo "No release found at $api" >&2; exit 1; }
url=$(printf '%s' "$json" | grep -o '"browser_download_url": *"[^"]*/openlolo-device-[^"/]*\.tar\.gz"' | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')
[ -n "$url" ] || { echo "The release at $api has no openlolo-device-*.tar.gz asset" >&2; exit 1; }
file=${url##*/}
name=${file%.tar.gz}
release=${name#openlolo-device-}
work=/var/tmp/openlolo-install
mkdir -p "$work"
cd "$work"
echo "Downloading $file"
curl -fsSL -o "$file" "$url"
curl -fsSL -o "$file.sha256" "$url.sha256"
sha256sum -c "$file.sha256"
rm -rf "$name"
tar -xzf "$file"
echo "Installing release $release from $work/$name"
exec sh "$name/scripts/install.sh" "$work/$name" "$@"
