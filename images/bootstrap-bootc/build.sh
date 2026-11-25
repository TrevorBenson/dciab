#!/usr/bin/env bash
# Build the DCiaB bootstrap appliance: an OCI image, then an unattended installer ISO for
# the DCiaB USB (default) and, optionally, qcow2/vmdk/vhd for running it as a VM.
#
#   sudo images/bootstrap-bootc/build.sh [--type anaconda-iso] [--type qcow2] [--image-only] [--print]
#
# 1. Downloads kind, helm and kubectl, each checked against its sha256 in bundle.lock
#    (bootstrapAppliance.toolChecksums), and copies the Day 0 images (CoreDNS, Caddy, chrony,
#    Zot, the pve-assistant; lib/dciab_media/preload.py day0_refs) at their bundle.lock digests
#    into an OCI layout with oras (tools.oras, checked against binarySHA256), the same way
#    build/archive.py fills the archive's payload. The appliance loads them at boot.
# 2. Builds images/bootstrap-bootc/Containerfile on nodeImages.bootc's base: the pinned
#    baseImage, or the base images/node-bootc/build.sh built locally.
# 3. bootc-image-builder writes the outputs under images/bootstrap-bootc/output/. The ISO
#    embeds kickstart.ks: it installs onto spec.bootstrap.workDisk.disk, chosen from
#    dciab.yaml on the stick when the installer starts.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"
BUILD="$HERE/build"

types=()
print_only=false
image_only=false
while (( $# )); do
  case "$1" in
    --type) types+=(--type "${2:?--type needs a value: anaconda-iso, qcow2, vmdk, vhd, raw}"); shift 2 ;;
    --print) print_only=true; shift ;;
    --image-only) image_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
(( ${#types[@]} )) || types=(--type anaconda-iso)

read -r version k8s kind helm base_image manifest bib sha_kind sha_kubectl sha_helm oras sha_oras < <(python3 - "$ROOT" <<'PY'
import re, sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
lock = yaml.safe_load((root / "bundle.lock").read_text())
b = lock["nodeImages"]["bootc"]
def pinned(ref):
    return bool(re.search(r"@sha256:[0-9a-f]{64}$", ref or ""))
if not pinned(b["bootcImageBuilder"]):
    sys.exit("build.sh: pin nodeImages.bootc.bootcImageBuilder as image@sha256:<digest>")
if b.get("baseImage") and not pinned(b["baseImage"]):
    sys.exit("build.sh: nodeImages.bootc.baseImage must be image@sha256:<digest>")
t = lock["bootstrapAppliance"]["toolChecksums"]
for name in ("kind", "kubectl", "helm"):
    if not re.fullmatch(r"[0-9a-f]{64}", t.get(name) or ""):
        sys.exit(f"build.sh: pin bootstrapAppliance.toolChecksums.{name} (64 hex)")
sha_oras = (lock.get("binarySHA256") or {}).get("oras-linux-amd64.tar.gz") or ""
if not re.fullmatch(r"[0-9a-f]{64}", sha_oras):
    sys.exit("build.sh: pin binarySHA256.oras-linux-amd64.tar.gz (64 hex)")
print((root / "VERSION").read_text().strip(), lock["kubernetes"]["version"], lock["tools"]["kind"],
      lock["tools"]["helm"], b.get("baseImage") or "-", b["manifest"], b["bootcImageBuilder"],
      t["kind"], t["kubectl"], t["helm"], lock["tools"]["oras"], sha_oras)
PY
)
base="$base_image"
[[ "$base" == - ]] && base="localhost/dciab-rocky-bootc-base:$manifest"
tag="localhost/dciab-bootstrap:$version"

build_cmd=(podman build -f "$HERE/Containerfile" -t "$tag" --build-arg "BASE_IMAGE=$base" "$ROOT")
disk_cmd=(podman run --rm --privileged --security-opt label=type:unconfined_t
  -v "$OUT:/output" -v "$BUILD/config.toml:/config.toml:ro"
  -v /var/lib/containers/storage:/var/lib/containers/storage
  "$bib" --config /config.toml "${types[@]}" --rootfs xfs "$tag")

if [[ "$print_only" == true ]]; then
  echo "appliance $version: kubernetes $k8s, kind $kind, helm $helm"
  echo "base: $base"
  echo "day0 images:"
  PYTHONPATH="$ROOT/lib" python3 -c 'import sys, yaml; from dciab_media.preload import day0_refs
for k, v in day0_refs(yaml.safe_load(open(sys.argv[1]))).items(): print(f"  {k}: {v}")' "$ROOT/bundle.lock"
  printf '\nimage:   '; printf '%q ' "${build_cmd[@]}"
  printf '\noutputs: '; printf '%q ' "${disk_cmd[@]}"; echo
  exit 0
fi
if (( EUID != 0 )); then
  echo "build.sh: run as root (bootc-image-builder reads root's container storage)" >&2
  exit 2
fi
if ! podman image exists "$base"; then
  if [[ "$base_image" != - ]]; then
    podman pull "$base"
  else
    echo "build.sh: base image $base not found; run images/node-bootc/build.sh first, or pin nodeImages.bootc.baseImage" >&2
    exit 1
  fi
fi

rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD/bin" "$OUT"
# Hand the outputs back to the user who ran sudo, so they can be read and removed without root.
if [[ -n "${SUDO_UID:-}" ]]; then
  # shellcheck disable=SC2153  # SUDO_GID is set by sudo, alongside SUDO_UID
  trap 'chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" "$BUILD" 2>/dev/null || true' EXIT
fi

# fetch URL DEST EXPECTED_SHA256
fetch() {
  curl -fsSL --retry 3 -o "$2" "$1"
  echo "$3  $2" | sha256sum -c --quiet -
}

echo "== tools"
fetch "https://github.com/kubernetes-sigs/kind/releases/download/$kind/kind-linux-amd64" "$BUILD/bin/kind" "$sha_kind"
fetch "https://dl.k8s.io/release/$k8s/bin/linux/amd64/kubectl" "$BUILD/bin/kubectl" "$sha_kubectl"
fetch "https://get.helm.sh/helm-$helm-linux-amd64.tar.gz" "$BUILD/helm.tar.gz" "$sha_helm"
tar -xzf "$BUILD/helm.tar.gz" -C "$BUILD" linux-amd64/helm
mv "$BUILD/linux-amd64/helm" "$BUILD/bin/helm"
rm -rf "$BUILD/helm.tar.gz" "$BUILD/linux-amd64"
chmod 0755 "$BUILD"/bin/*

echo "== day0 images"
fetch "https://github.com/oras-project/oras/releases/download/$oras/oras_${oras#v}_linux_amd64.tar.gz" "$BUILD/oras.tar.gz" "$sha_oras"
mkdir -p "$BUILD/tools"
tar -xzf "$BUILD/oras.tar.gz" -C "$BUILD/tools" oras
rm -f "$BUILD/oras.tar.gz"
PYTHONPATH="$ROOT:$ROOT/lib" python3 - "$ROOT" "$BUILD/day0-images" "$BUILD/tools/oras" <<'PY'
import sys
from pathlib import Path
import yaml
from build.archive import copy_images
from dciab_media.preload import check, day0_refs

root, out, oras = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
refs = day0_refs(yaml.safe_load((root / "bundle.lock").read_text()))
copy_images([{"image": r, "digest": "pinned"} for r in refs.values()], out, oras=oras)
problems = check(out)
if problems:
    sys.exit("build.sh: " + "; ".join(problems))
print(f"   {len(refs)} images at their pinned digests")
PY

# The kickstart goes into config.toml as a TOML multi-line literal string.
if grep -q "'''" "$HERE/kickstart.ks"; then
  echo "build.sh: kickstart.ks must not contain three single quotes in a row" >&2
  exit 1
fi
{
  echo "[customizations.installer.kickstart]"
  echo "contents = '''"
  cat "$HERE/kickstart.ks"
  echo "'''"
} > "$BUILD/config.toml"

echo "== image: $tag"
"${build_cmd[@]}"
if [[ "$image_only" == true ]]; then
  echo "== $tag built (--image-only: no installer ISO or disks)"
  exit 0
fi
echo "== outputs: ${types[*]}"
"${disk_cmd[@]}"
find "$OUT" -type f -printf '%p  %s bytes\n'
