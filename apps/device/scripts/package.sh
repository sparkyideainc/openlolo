#!/bin/sh
# Build a versioned OpenLolo device release tarball from a committed git ref.
# The tarball is what you copy to the box and hand to scripts/install.sh.
set -eu
usage='Usage: package.sh [RELEASE] [--ref GIT_REF] [--out DIR]
  RELEASE   version stamped into the tarball (default: YYYYMMDD-<short commit>; the workflow passes the tag)
  --ref     git ref to package (default: HEAD); uncommitted changes are never included
  --out     output directory (default: apps/device/dist)'
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
release=
ref=HEAD
out=$here/dist
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) ref=${2:?--ref needs a git ref}; shift 2 ;;
    --out) out=${2:?--out needs a directory}; shift 2 ;;
    -h|--help) echo "$usage"; exit 0 ;;
    --*) echo "Unknown option: $1" >&2; echo "$usage" >&2; exit 1 ;;
    *) [ -z "$release" ] || { echo "$usage" >&2; exit 1; }; release=$1; shift ;;
  esac
done
commit=$(git -C "$repo" rev-parse --short "$ref")
[ -n "$release" ] || release=$(date -u +%Y%m%d)-$commit
case "$release" in ''|*[!A-Za-z0-9._-]*|.|..) echo 'Invalid release name: letters, digits, . _ - only' >&2; exit 1 ;; esac
if [ "$ref" = HEAD ] && [ -n "$(git -C "$repo" status --porcelain -- apps/device)" ]; then
  echo 'Warning: apps/device has uncommitted changes; they are not included (commit them or pass --ref).' >&2
fi
name=openlolo-device-$release
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# A subdirectory tree-ish puts the device app at the archive root, without the apps/device/ prefix.
git -C "$repo" archive --format=tar --prefix="$name/" "$ref:apps/device" | tar -x -C "$tmp"
# The box page (apps/portal, ADR 0012) is built here and shipped inside the package; its output
# directory is git-ignored, so it is never in the archive above.
command -v bun >/dev/null 2>&1 || { echo 'bun is required to build the box page (apps/portal)' >&2; exit 1; }
(cd "$repo" && bun run --filter portal build >/dev/null) || { echo 'Box page build failed' >&2; exit 1; }
rm -rf "$tmp/$name/src/openlolo/web/portal"
cp -R "$repo/apps/device/src/openlolo/web/portal" "$tmp/$name/src/openlolo/web/portal"
printf 'release=%s\ncommit=%s\nref=%s\nbuilt=%s\n' "$release" "$(git -C "$repo" rev-parse "$ref")" "$ref" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp/$name/RELEASE"
mkdir -p "$out"
# COPYFILE_DISABLE keeps macOS from adding ._* AppleDouble files, which break the app-profile loader.
COPYFILE_DISABLE=1 tar -C "$tmp" -czf "$out/$name.tar.gz" "$name"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$out" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
else
  (cd "$out" && shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256")
fi
cat "$out/$name.tar.gz.sha256"
cat <<MSG

Install on the box (Debian 13 / Raspberry Pi OS 64-bit, arm64):

  scp "$out/$name.tar.gz" USER@BOX:
  ssh -t USER@BOX 'tar -xzf $name.tar.gz && sudo sh $name/scripts/install.sh "\$HOME/$name"'

Add --no-remote for a LAN-only box, or --auth-key tskey-auth-... for an unattended tailnet join.
MSG
