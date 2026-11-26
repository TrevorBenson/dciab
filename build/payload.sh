#!/usr/bin/env bash
# Build the full archive payload into OUT: every image on every platform, charts, providers,
# binaries, node disks, SBOMs and SHA256SUMS. Used by the archive payload and release workflows.
#
#   build/payload.sh OUT [INVENTORY]
#
# INVENTORY, if given, receives a copy of the image inventory (images.yaml) the build used.
#
# Needs python3 with PyYAML, helm (bundle.lock tools.helm) and network access; downloads
# kubeadm (checked against dl.k8s.io's sha256) only to list the control plane images. Refuses
# while an image DCiaB builds itself or the Proxmox VE node disk isn't published and pinned.
set -euo pipefail
out="${1:?usage: build/payload.sh OUT [INVENTORY]}"
inventory_copy="${2:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

k8s="$(python3 -c 'import yaml; print(yaml.safe_load(open("bundle.lock"))["kubernetes"]["version"])')"
[[ "$k8s" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "payload.sh: bad kubernetes.version: $k8s" >&2; exit 1; }
curl -fsSL --connect-timeout 20 --max-time 300 -o "$work/kubeadm" "https://dl.k8s.io/release/$k8s/bin/linux/amd64/kubeadm"
echo "$(curl -fsSL --connect-timeout 20 --max-time 60 "https://dl.k8s.io/release/$k8s/bin/linux/amd64/kubeadm.sha256")  $work/kubeadm" | sha256sum -c -
chmod +x "$work/kubeadm"

crds="$(python3 hack/fetch_crds.py)"
addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > "$work/cilium.yaml"
addons/longhorn/install.sh examples/metal3-cluster.yaml --template --cluster metal01 > "$work/longhorn.yaml"
addons/longhorn/install.sh examples/metal3-cluster.yaml --images --cluster metal01 > "$work/longhorn-images.txt"
addons/vsphere-cpi/install.sh examples/vsphere.yaml --template > "$work/vsphere-cpi.yaml"
mapfile -t longhorn < "$work/longhorn-images.txt"
(( ${#longhorn[@]} > 0 )) || { echo "payload.sh: no Longhorn images listed" >&2; exit 1; }
"$work/kubeadm" config images list --kubernetes-version "$k8s" > "$work/kubeadm.txt"
args=(--manifests "$crds" --extra "$work/cilium.yaml" --extra "$work/longhorn.yaml"
      --extra "$work/vsphere-cpi.yaml" --kubeadm-images "$work/kubeadm.txt")
for ref in "${longhorn[@]}"; do args+=(--image "$ref=longhorn chart values"); done
# --require-built: the images DCiaB builds itself (the chrony image, ...) must be published and pinned.
python3 build/inventory.py "${args[@]}" --require-built --format yaml > "$work/images.yaml"
[[ -z "$inventory_copy" ]] || cp "$work/images.yaml" "$inventory_copy"

python3 build/archive.py binaries --out "$out"
python3 build/archive.py images --inventory "$work/images.yaml" --out "$out" --oras "$out/bin/oras"
python3 build/archive.py charts --out "$out"
python3 build/archive.py providers --out "$out" --manifests "$crds"
# --require-built: the Proxmox VE node disk must be published and pinned (lab-node-image.yml).
python3 build/archive.py nodes --out "$out" --oras "$out/bin/oras" --require-built
python3 build/package.py tools --out "$work/build"
python3 build/package.py sboms --payload "$out" --syft "$work/build/tools/syft"
python3 build/archive.py sums --out "$out"
du -sh "$out"/* "$out"
