#!/usr/bin/env bash
# Build the DCiaB lab runner: a Rocky Linux 10 bootc image that runs GitHub Actions jobs
# against real infrastructure (a hypervisor's API, kind as a management cluster, the seed
# registry), then a disk image to boot it as a VM. Test tooling: never part of the archive.
#
#   sudo images/lab-runner/build.sh [--type qcow2] [--type raw] [--print]
#
# 1. Downloads the archive's pinned binaries (kind, kubectl, helm, clusterctl, oras, age,
#    cosign) with build/archive.py, each checked against bundle.lock, and the GitHub
#    Actions runner (labRunner.actionsRunner, checked against its sha256).
# 2. Builds images/lab-runner/Containerfile on nodeImages.bootc's base: the pinned
#    baseImage, or the base images/node-bootc/build.sh built locally.
# 3. bootc-image-builder writes the disks under images/lab-runner/output/.
#
# Output: localhost/dciab-lab-runner:<version> in root's container storage, and disks
# under images/lab-runner/output/ (ignored by Git). See images/lab-runner/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"
BUILD="$HERE/build"

types=()
print_only=false
while (( $# )); do
  case "$1" in
    --type) types+=(--type "${2:?--type needs a value: qcow2, raw, vmdk}"); shift 2 ;;
    --print) print_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
(( ${#types[@]} )) || types=(--type qcow2)

read -r version base_image manifest bib runner_version runner_sha < <(python3 - "$ROOT" <<'PY'
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
r = lock["labRunner"]["actionsRunner"]
if not re.fullmatch(r"\d+\.\d+\.\d+", str(r.get("version") or "")):
    sys.exit("build.sh: labRunner.actionsRunner.version must be X.Y.Z")
if not re.fullmatch(r"[0-9a-f]{64}", r.get("sha256") or ""):
    sys.exit("build.sh: pin labRunner.actionsRunner.sha256 (64 hex)")
print((root / "VERSION").read_text().strip(), b.get("baseImage") or "-", b["manifest"],
      b["bootcImageBuilder"], r["version"], r["sha256"])
PY
)
base="$base_image"
[[ "$base" == - ]] && base="localhost/dciab-rocky-bootc-base:$manifest"
tag="localhost/dciab-lab-runner:$version"
runner_url="https://github.com/actions/runner/releases/download/v$runner_version/actions-runner-linux-x64-$runner_version.tar.gz"

build_cmd=(podman build -f "$HERE/Containerfile" -t "$tag" --build-arg "BASE_IMAGE=$base" "$ROOT")
disk_cmd=(podman run --rm --privileged --security-opt label=type:unconfined_t
  -v "$OUT:/output" -v /var/lib/containers/storage:/var/lib/containers/storage
  "$bib" "${types[@]}" --rootfs xfs "$tag")

if [[ "$print_only" == true ]]; then
  echo "lab runner $version: actions runner $runner_version"
  echo "base: $base"
  echo "runner: $runner_url"
  printf '\nimage: '; printf '%q ' "${build_cmd[@]}"
  printf '\ndisks: '; printf '%q ' "${disk_cmd[@]}"; echo
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
mkdir -p "$BUILD/actions-runner" "$OUT"
# Hand the outputs back to the user who ran sudo, so they can be read and removed without root.
if [[ -n "${SUDO_UID:-}" ]]; then
  # shellcheck disable=SC2153  # SUDO_GID is set by sudo, alongside SUDO_UID
  trap 'chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" "$BUILD" 2>/dev/null || true' EXIT
fi

echo "== tools (build/archive.py binaries)"
python3 "$ROOT/build/archive.py" binaries --out "$BUILD"
chmod 0755 "$BUILD"/bin/*

echo "== actions runner $runner_version"
curl -fsSL --retry 3 -o "$BUILD/actions-runner.tar.gz" "$runner_url"
echo "$runner_sha  $BUILD/actions-runner.tar.gz" | sha256sum -c --quiet -
tar -xzf "$BUILD/actions-runner.tar.gz" -C "$BUILD/actions-runner"
rm -f "$BUILD/actions-runner.tar.gz"

echo "== image: $tag"
"${build_cmd[@]}"
echo "== disks: ${types[*]}"
"${disk_cmd[@]}"
find "$OUT" -type f -printf '%p  %s bytes\n'
