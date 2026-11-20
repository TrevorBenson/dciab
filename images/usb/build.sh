#!/usr/bin/env bash
# Build the DCiaB USB image: Ventoy (pinned in bundle.lock) with the bootstrap appliance
# as a Linux vDisk, on an exFAT data partition labelled DCIAB. Any OS writes the image to
# a stick with an ordinary imaging tool. See docs/adr/0002-bootstrap-appliance-on-ventoy.md.
#
#   sudo images/usb/build.sh [--size GiB] [--with-isos] [--print]
#
# Input: images/bootstrap-bootc/output/image/disk.raw (images/bootstrap-bootc/build.sh).
# Output: images/usb/output/dciab-usb-<version>.img, plus its sha256.
#
# --size     the whole stick image; defaults to the appliance disk plus 16 GiB
# --with-isos  also copy the hypervisor installers DCiaB ships (bundle.lock hypervisors,
#              shipped: true) into isos/, checked against their sha256
#
# The data partition starts with dciab.yaml absent: the operator adds it (and secrets/,
# and a site-supplied ESXi ISO) from any laptop after writing the stick.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"
BUILD="$HERE/build"
APPLIANCE="$ROOT/images/bootstrap-bootc/output/image/disk.raw"

size=""
with_isos=false
print_only=false
while (( $# )); do
  case "$1" in
    --size) size="${2:?--size needs GiB}"; shift 2 ;;
    --with-isos) with_isos=true; shift ;;
    --print) print_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

read -r version vt_ver vt_url vt_sha < <(python3 - "$ROOT" <<'PY'
import re, sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
v = yaml.safe_load((root / "bundle.lock").read_text())["bootstrapAppliance"]["ventoy"]
if not re.fullmatch(r"[0-9a-f]{64}", v.get("sha256") or ""):
    sys.exit("build.sh: pin bootstrapAppliance.ventoy.sha256 (64 hex)")
print((root / "VERSION").read_text().strip(), v["version"], v["url"], v["sha256"])
PY
)
vdisk="dciab-bootstrap-$version.raw.vtoy"
image="$OUT/dciab-usb-$version.img"

if [[ -z "$size" ]]; then
  if [[ -f "$APPLIANCE" ]]; then
    size=$(( ($(stat -c %s "$APPLIANCE") + (1 << 30) - 1) / (1 << 30) + 16 ))
  else
    size="?"
  fi
fi

if [[ "$print_only" == true ]]; then
  echo "USB $version: Ventoy $vt_ver, ${size} GiB image, appliance vDisk /$vdisk, isos: $with_isos"
  echo "appliance: $APPLIANCE$([[ -f "$APPLIANCE" ]] || echo ' (not built yet)')"
  echo "output: $image"
  exit 0
fi
if (( EUID != 0 )); then
  echo "build.sh: run as root (it uses a loop device)" >&2
  exit 2
fi
[[ -f "$APPLIANCE" ]] || { echo "build.sh: $APPLIANCE not found; run images/bootstrap-bootc/build.sh first" >&2; exit 1; }
[[ "$size" =~ ^[0-9]+$ ]] || { echo "build.sh: --size must be a whole number of GiB" >&2; exit 2; }

rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD" "$OUT"
loop=""
mnt="$BUILD/mnt"
cleanup() {
  mountpoint -q "$mnt" && umount "$mnt"
  [[ -n "$loop" ]] && losetup -d "$loop"
  return 0
}
trap cleanup EXIT

echo "== Ventoy $vt_ver"
curl -fsSL --retry 3 -o "$BUILD/ventoy.tar.gz" "$vt_url"
echo "$vt_sha  $BUILD/ventoy.tar.gz" | sha256sum -c --quiet -
tar -xzf "$BUILD/ventoy.tar.gz" -C "$BUILD"
ventoy_dir="$BUILD/ventoy-$vt_ver"

echo "== image: ${size} GiB"
truncate -s "${size}G" "$image"
loop=$(losetup --find --show --partscan "$image")
# -I: install unconditionally; -g: GPT; -L: data partition label. Secure Boot support is
# Ventoy's default. Ventoy2Disk.sh asks for confirmation twice.
(cd "$ventoy_dir" && printf 'y\ny\n' | ./Ventoy2Disk.sh -I -g -L DCIAB "$loop")
partprobe "$loop" || true
udevadm settle
data="${loop}p1"
[[ -b "$data" ]] || { echo "build.sh: $data not found after Ventoy install" >&2; exit 1; }

mkdir -p "$mnt"
mount -t exfat "$data" "$mnt"

echo "== appliance vDisk: $vdisk"
cp --sparse=never "$APPLIANCE" "$mnt/$vdisk"

mkdir -p "$mnt/ventoy" "$mnt/isos" "$mnt/secrets" "$mnt/examples"
cat > "$mnt/ventoy/ventoy.json" <<JSON
{
  "control": [
    { "VTOY_DEFAULT_IMAGE": "/$vdisk" },
    { "VTOY_MENU_TIMEOUT": "10" }
  ],
  "menu_alias": [
    { "image": "/$vdisk", "alias": "DCiaB $version: bootstrap appliance (server zero)" }
  ]
}
JSON
cp "$ROOT"/examples/*.yaml "$mnt/examples/"
cat > "$mnt/README.txt" <<TXT
DCiaB $version

1. Put your site config here as dciab.yaml (start from examples/), and the files it
   references under secrets/. For ESXi, copy the installer ISO it names here too.
2. Boot one x86_64 server on the node network from this stick: "DCiaB $version:
   bootstrap appliance" starts after 10 seconds.
3. SSH to spec.bootstrap.address as one of identity.initialAdmins, and run dciab.

isos/ holds installers you can also boot from this menu by hand.
TXT

if [[ "$with_isos" == true ]]; then
  echo "== installers"
  python3 - "$ROOT" <<'PY' | while read -r url sha; do
import sys
from pathlib import Path
import yaml
for name, h in yaml.safe_load((Path(sys.argv[1]) / "bundle.lock").read_text())["hypervisors"].items():
    if h.get("shipped") and h.get("isoURL") and h.get("isoSHA256"):
        print(h["isoURL"], h["isoSHA256"])
PY
    dest="$mnt/isos/$(basename "$url")"
    curl -fsSL --retry 3 -o "$dest" "$url"
    echo "$sha  $dest" | sha256sum -c --quiet -
    echo "   $(basename "$url")"
  done
fi

sync
df -h "$mnt" | tail -1
umount "$mnt"
losetup -d "$loop"; loop=""
(cd "$OUT" && sha256sum "$(basename "$image")" > "$(basename "$image").sha256")
echo "== done: $image"
