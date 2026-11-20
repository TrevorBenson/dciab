#!/usr/bin/env bash
# Build the DCiaB node template on Proxmox VE with Kubernetes image-builder.
#
#   images/node/build.sh <proxmox.env> [--print-vars]
#
# --print-vars writes and prints the Packer variables, then stops: nothing is built.
# Reads every version from bundle.lock, builds a local image-builder container with the
# Rocky Linux 10 target (images/node/Containerfile), and runs the Proxmox build. The
# result is a VM template on Proxmox named rocky10-kube-<kubernetes version>. Nothing
# is written to the repo except images/node/.build/ (ignored).
#
# <proxmox.env> holds the Proxmox connection and placement; copy proxmox.env.example,
# keep it outside the repo, mode 600. See images/node/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

env_file="${1:-}"
if [[ -z "$env_file" || ! -f "$env_file" ]]; then
  echo "usage: images/node/build.sh <proxmox.env> [--print-vars]   (see images/node/proxmox.env.example)" >&2
  exit 2
fi
if [[ "$(stat -c %a "$env_file")" != 600 ]]; then
  echo "build.sh: $env_file holds a Proxmox token; chmod 600 it first" >&2
  exit 2
fi

print_only=false
case "${2:-}" in
  "") ;;
  --print-vars) print_only=true ;;
  *) echo "build.sh: unknown argument '$2' (only --print-vars)" >&2; exit 2 ;;
esac
if (( $# > 2 )); then
  echo "build.sh: too many arguments" >&2
  exit 2
fi

runtime="${CONTAINER_RUNTIME:-}"
if [[ -z "$runtime" ]]; then
  runtime="$(command -v podman || command -v docker || true)"
fi
if [[ -z "$runtime" && "$print_only" == false ]]; then
  echo "build.sh: needs podman or docker (or set CONTAINER_RUNTIME)" >&2
  exit 2
fi

# One build at a time per checkout: .build/ and the image tag are shared.
mkdir -p "$HERE/.build"
python3 - "$ROOT/bundle.lock" "$HERE/.build/dciab-node.json" "$env_file" > "$HERE/.build/base-image" <<'PY'
import json, re, sys
import yaml

# The env file is parsed, never sourced: KEY=value lines only. The PROXMOX_* values go
# to the container through --env-file; only the optional DCIAB_* values are read here.
env = {}
for n, line in enumerate(open(sys.argv[3]), 1):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, sep, value = line.partition("=")
    if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        sys.exit(f"build.sh: {sys.argv[3]} line {n}: expected KEY=value")
    env[key] = value

lock = yaml.safe_load(open(sys.argv[1]))
nodes = lock["nodeImages"]
ib, rocky = nodes["imageBuilder"], nodes["rocky10"]
missing = [name for name, value in [
    ("nodeImages.imageBuilder.digest", ib.get("digest")),
    ("nodeImages.rocky10.isoURL", rocky.get("isoURL")),
    ("nodeImages.rocky10.isoSHA256", rocky.get("isoSHA256")),
] if not value]
if missing:
    sys.exit("build.sh: bundle.lock has no value for " + ", ".join(missing)
             + " (handoff task 0002, step 1 resolves them)")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", ib["digest"]):
    sys.exit("build.sh: nodeImages.imageBuilder.digest must be sha256:<64 hex>")
if not re.fullmatch(r"[0-9a-f]{64}", rocky["isoSHA256"]):
    sys.exit("build.sh: nodeImages.rocky10.isoSHA256 must be 64 hex characters")

k8s = lock["kubernetes"]["version"]          # v1.36.6
major_minor = ".".join(k8s.split(".")[:2])   # v1.36
variables = {
    "kubernetes_semver": k8s,
    "kubernetes_series": major_minor,
    "kubernetes_rpm_version": k8s.lstrip("v"),
    "iso_url": rocky["isoURL"],
    "iso_checksum": rocky["isoSHA256"],
    "iso_checksum_type": "sha256",
    # Matches the template name the site config and examples use.
    "artifact_name": f"rocky10-kube-{k8s}",
    # raw works on every Proxmox storage. Upstream's default, qcow2, fails on block
    # storage (ZFS, LVM, LVM-thin, Ceph RBD) with "unsupported format 'qcow2'".
    "disk_format": env.get("DCIAB_DISK_FORMAT") or "raw",
}
optional = {
    "DCIAB_TEMPLATE_VMID": "vmid",
    "DCIAB_STORAGE_POOL_TYPE": "storage_pool_type",
    "DCIAB_DISK_SIZE": "disk_size",
    "DCIAB_CPU_TYPE": "cpu_type",
}
for name, var in optional.items():
    if env.get(name):
        variables[var] = env[name]
json.dump(variables, open(sys.argv[2], "w"), indent=2, sort_keys=True)
print(f'{ib["image"]}@{ib["digest"]}')
PY

if [[ "$print_only" == true ]]; then
  echo "base image: $(cat "$HERE/.build/base-image")"
  cat "$HERE/.build/dciab-node.json"; echo
  exit 0
fi

base_image="$(cat "$HERE/.build/base-image")"
version="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["kubernetes"]["version"])' "$ROOT/bundle.lock")"
tag="localhost/dciab-node-image-builder:$version"

echo "== building $tag from $base_image"
"$runtime" build -f "$HERE/Containerfile" --build-arg "BASE_IMAGE=$base_image" -t "$tag" "$HERE"

echo "== packer variables"
cat "$HERE/.build/dciab-node.json"; echo

echo "== building template rocky10-kube-$version on Proxmox"
# Host networking: the build VM fetches its kickstart from Packer's HTTP server on this
# machine, and Packer reaches the VM over SSH.
"$runtime" run --rm --network host --env-file "$env_file" "$tag" build-proxmox-rockylinux-10
