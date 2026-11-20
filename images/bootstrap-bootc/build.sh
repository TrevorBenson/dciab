#!/usr/bin/env bash
# Build the DCiaB bootstrap appliance: an OCI image, then a fixed-size raw disk for the
# USB (Ventoy Linux vDisk) and, optionally, qcow2/vmdk/vhd for running it as a VM.
#
#   sudo images/bootstrap-bootc/build.sh [--size GiB] [--type raw] [--type qcow2] [--print]
#
# 1. Downloads kind, helm and kubectl (versions from bundle.lock, checked against the
#    projects' published sha256 files) and vtoyboot (sha256 pinned in bundle.lock).
# 2. Builds images/bootstrap-bootc/Containerfile on nodeImages.bootc's base: the pinned
#    baseImage, or the base images/node-bootc/build.sh built locally.
# 3. bootc-image-builder writes the disks under images/bootstrap-bootc/output/.
#
# --size is the root filesystem's minimum size. The vDisk can't grow on the stick, and it
# holds container storage, kind and the seed registry for the whole bootstrap.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"
BUILD="$HERE/build"

size=64
types=()
print_only=false
while (( $# )); do
  case "$1" in
    --size) size="${2:?--size needs GiB}"; shift 2 ;;
    --type) types+=(--type "${2:?--type needs a value: raw, qcow2, vmdk, vhd}"); shift 2 ;;
    --print) print_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
(( ${#types[@]} )) || types=(--type raw)
[[ "$size" =~ ^[0-9]+$ ]] || { echo "build.sh: --size must be a whole number of GiB" >&2; exit 2; }

read -r version k8s kind helm base_image manifest bib vtb_ver vtb_url vtb_sha < <(python3 - "$ROOT" <<'PY'
import re, sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
lock = yaml.safe_load((root / "bundle.lock").read_text())
b = lock["nodeImages"]["bootc"]
v = lock["bootstrapAppliance"]["vtoyboot"]
def pinned(ref):
    return bool(re.search(r"@sha256:[0-9a-f]{64}$", ref or ""))
if not pinned(b["bootcImageBuilder"]):
    sys.exit("build.sh: pin nodeImages.bootc.bootcImageBuilder as image@sha256:<digest>")
if b.get("baseImage") and not pinned(b["baseImage"]):
    sys.exit("build.sh: nodeImages.bootc.baseImage must be image@sha256:<digest>")
if not re.fullmatch(r"[0-9a-f]{64}", v.get("sha256") or ""):
    sys.exit("build.sh: pin bootstrapAppliance.vtoyboot.sha256 (64 hex)")
print((root / "VERSION").read_text().strip(), lock["kubernetes"]["version"], lock["tools"]["kind"],
      lock["tools"]["helm"], b.get("baseImage") or "-", b["manifest"], b["bootcImageBuilder"],
      v["version"], v["url"], v["sha256"])
PY
)
base="$base_image"
[[ "$base" == - ]] && base="localhost/dciab-rocky-bootc-base:$manifest"
tag="localhost/dciab-bootstrap:$version"

build_cmd=(podman build -f "$HERE/Containerfile" -t "$tag" --build-arg "BASE_IMAGE=$base" "$ROOT")
disk_cmd=(podman run --rm --privileged --security-opt label=type:unconfined_t
  -v "$OUT:/output" -v "$BUILD/config.toml:/config.toml:ro"
  -v /var/lib/containers/storage:/var/lib/containers/storage
  "$bib" "${types[@]}" --rootfs xfs "$tag")

if [[ "$print_only" == true ]]; then
  echo "appliance $version: kubernetes $k8s, kind $kind, helm $helm, vtoyboot $vtb_ver; root >= ${size} GiB"
  echo "base: $base"
  printf '\nimage: '; printf '%q ' "${build_cmd[@]}"
  printf '\ndisks: '; printf '%q ' "${disk_cmd[@]}"; echo
  exit 0
fi
if (( EUID != 0 )); then
  echo "build.sh: run as root (bootc-image-builder reads root's container storage)" >&2
  exit 2
fi
if ! podman image exists "$base"; then
  echo "build.sh: base image $base not found; run images/node-bootc/build.sh first, or pin nodeImages.bootc.baseImage" >&2
  exit 1
fi

rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD/bin" "$BUILD/vtoyboot" "$OUT"

# fetch URL DEST EXPECTED_SHA256
fetch() {
  curl -fsSL --retry 3 -o "$2" "$1"
  echo "$3  $2" | sha256sum -c --quiet -
}
upstream_sha() { curl -fsSL --retry 3 "$1" | awk '{print $1; exit}'; }

echo "== tools"
fetch "https://github.com/kubernetes-sigs/kind/releases/download/$kind/kind-linux-amd64" "$BUILD/bin/kind" \
  "$(upstream_sha "https://github.com/kubernetes-sigs/kind/releases/download/$kind/kind-linux-amd64.sha256sum")"
fetch "https://dl.k8s.io/release/$k8s/bin/linux/amd64/kubectl" "$BUILD/bin/kubectl" \
  "$(upstream_sha "https://dl.k8s.io/release/$k8s/bin/linux/amd64/kubectl.sha256")"
fetch "https://get.helm.sh/helm-$helm-linux-amd64.tar.gz" "$BUILD/helm.tar.gz" \
  "$(upstream_sha "https://get.helm.sh/helm-$helm-linux-amd64.tar.gz.sha256sum")"
tar -xzf "$BUILD/helm.tar.gz" -C "$BUILD" linux-amd64/helm
mv "$BUILD/linux-amd64/helm" "$BUILD/bin/helm"
rm -rf "$BUILD/helm.tar.gz" "$BUILD/linux-amd64"
chmod 0755 "$BUILD"/bin/*

echo "== vtoyboot $vtb_ver"
fetch "$vtb_url" "$BUILD/vtoyboot.iso" "$vtb_sha"
xorriso -osirrox on -indev "$BUILD/vtoyboot.iso" -extract / "$BUILD/vtoyboot" >/dev/null 2>&1
chmod -R u+w "$BUILD/vtoyboot"
rm -f "$BUILD/vtoyboot.iso"
ls "$BUILD"/vtoyboot/vtoyboot-*.tar.gz >/dev/null

cat > "$BUILD/config.toml" <<TOML
[[customizations.filesystem]]
mountpoint = "/"
minsize = "${size} GiB"
TOML

echo "== image: $tag"
"${build_cmd[@]}"
echo "== disks: ${types[*]}"
"${disk_cmd[@]}"
find "$OUT" -type f -printf '%p  %s bytes\n'
