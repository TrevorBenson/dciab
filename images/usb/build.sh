#!/usr/bin/env bash
# Build the DCiaB USB image: Ventoy (pinned in bundle.lock) with the bootstrap appliance's
# installer ISO, on an exFAT data partition labelled DCIAB. Any OS writes the image to a
# stick with an ordinary imaging tool. See docs/adr/0002-bootstrap-appliance-on-ventoy.md.
#
#   sudo images/usb/build.sh [--size GiB] [--with-isos] [--print]
#
# Input: images/bootstrap-bootc/output/bootiso/install.iso (images/bootstrap-bootc/build.sh).
# Output: images/usb/output/dciab-usb-<version>.img, plus its sha256.
#
# --size     the whole stick image; defaults to the appliance ISO plus 16 GiB
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
APPLIANCE="$ROOT/images/bootstrap-bootc/output/bootiso/install.iso"

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

read -r version vt_ver vt_url vt_sha py_ver py_url py_sha < <(python3 - "$ROOT" <<'PY'
import re, sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
a = yaml.safe_load((root / "bundle.lock").read_text())["bootstrapAppliance"]
v, p = a["ventoy"], a["pyyaml"]
for name, sha in (("ventoy", v.get("sha256")), ("pyyaml", p.get("sha256"))):
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        sys.exit(f"build.sh: pin bootstrapAppliance.{name}.sha256 (64 hex)")
print((root / "VERSION").read_text().strip(), v["version"], v["url"], v["sha256"],
      p["version"], p["url"], p["sha256"])
PY
)
iso="dciab-bootstrap-$version.iso"
image="$OUT/dciab-usb-$version.img"

if [[ -z "$size" ]]; then
  if [[ -f "$APPLIANCE" ]]; then
    size=$(( ($(stat -c %s "$APPLIANCE") + (1 << 30) - 1) / (1 << 30) + 16 ))
  else
    size="?"
  fi
fi

if [[ "$print_only" == true ]]; then
  echo "USB $version: Ventoy $vt_ver, ${size} GiB image, appliance installer /$iso, PyYAML $py_ver, isos: $with_isos"
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
  if [[ -n "${SUDO_UID:-}" ]]; then
    # shellcheck disable=SC2153  # SUDO_GID is set by sudo, alongside SUDO_UID
    chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" "$BUILD" 2>/dev/null || true
  fi
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

echo "== appliance installer: $iso"
cp "$APPLIANCE" "$mnt/$iso"

# The installer's %pre script runs dciab_bootstrap_host.install from here, with a
# pure-Python PyYAML (the installer environment has python3 but not PyYAML).
echo "== installer helper (PyYAML $py_ver)"
curl -fsSL --retry 3 -o "$BUILD/pyyaml.tar.gz" "$py_url"
echo "$py_sha  $BUILD/pyyaml.tar.gz" | sha256sum -c --quiet -
tar -xzf "$BUILD/pyyaml.tar.gz" -C "$BUILD"
mkdir -p "$mnt/.dciab/installer"
cp -r "$BUILD/pyyaml-$py_ver/lib/yaml" "$mnt/.dciab/installer/yaml"
cp -r "$ROOT/lib/dciab_bootstrap_host" "$mnt/.dciab/installer/dciab_bootstrap_host"
find "$mnt/.dciab" -name '__pycache__' -prune -exec rm -rf {} +

mkdir -p "$mnt/ventoy" "$mnt/isos" "$mnt/secrets" "$mnt/examples"
# No default entry and no timeout: the installer wipes a disk, so it must be chosen by hand.
cat > "$mnt/ventoy/ventoy.json" <<JSON
{
  "menu_alias": [
    { "image": "/$iso", "alias": "DCiaB $version: install the bootstrap appliance (server zero)" }
  ]
}
JSON
cp "$ROOT"/examples/*.yaml "$mnt/examples/"
cat > "$mnt/README.txt" <<TXT
DCiaB $version

1. Put your site config here as dciab.yaml (start from examples/), and the files it
   references under secrets/. Set spec.bootstrap.workDisk.disk to the server-zero disk
   the appliance installs onto: that disk is wiped. For ESXi, copy the installer ISO the
   config names here too.
2. Boot one x86_64 server on the node network from this stick and choose "DCiaB $version:
   install the bootstrap appliance". It installs unattended onto that one disk and
   reboots into the appliance (boot from the disk, not the stick, from then on).
3. SSH to spec.bootstrap.address as one of identity.initialAdmins, and run dciab.
   The stick can then be removed: its config is copied to the appliance's disk.

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
