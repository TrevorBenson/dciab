#!/usr/bin/env bash
# Install or upgrade the site's array CSI driver on one DCiaB cluster, then its StorageClasses.
#
#   addons/csi/install.sh <config> <cluster> <kubeconfig>    install or upgrade; wait; apply the four StorageClasses
#   addons/csi/install.sh <config> <cluster> --template      render the manifests; no cluster or network needed
#
# The driver is the cluster's block provider (spec.storage.block): proxmox-csi (the Proxmox
# CSI plugin) or vsphere-csi (the vSphere CSI driver). Its manifest is the upstream release
# manifest pinned in bundle.lock csiDrivers, from $DCIAB_PAYLOAD/providers when DCIAB_PAYLOAD
# is set, else from hack/fetch_crds.py; its sha256 is checked against bundle.lock checksums.
# DCiaB's changes to it: addons/csi/csi.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

usage() {
  sed -n '4,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
[[ $# -eq 3 ]] || usage
config="$1" cluster="$2" mode="$3"
[[ -f "$config" ]] || { echo "install.sh: no such file: $config" >&2; exit 2; }

read -r driver ns < <(python3 "$HERE/csi.py" driver "$config" "$cluster")
if [[ -n "${DCIAB_PAYLOAD:-}" ]]; then
  manifests="$DCIAB_PAYLOAD/providers"
else
  manifests="$(python3 "$ROOT/hack/fetch_crds.py")"
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
python3 "$HERE/csi.py" render "$config" "$cluster" "$manifests" > "$workdir/driver.yaml"

case "$mode" in
  --template)
    cat "$workdir/driver.yaml"
    echo "---"
    python3 "$HERE/csi.py" secret "$config" "$cluster" --template
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    export KUBECONFIG="$kubeconfig"
    echo "== $driver on $(kubectl config current-context), namespace $ns"
    # The namespace first (privileged: the node plugin mounts host paths), then the
    # credentials straight from the config's reference through a pipe. Server-side apply: a
    # client-side apply would keep a plain copy in the last-applied-configuration annotation.
    python3 - "$workdir/driver.yaml" > "$workdir/namespace.yaml" <<'PY'
import sys

import yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
yaml.safe_dump(next(d for d in docs if d["kind"] == "Namespace"), sys.stdout, sort_keys=False)
PY
    kubectl apply --server-side --field-manager=dciab --force-conflicts -f "$workdir/namespace.yaml"
    python3 "$HERE/csi.py" secret "$config" "$cluster" \
      | kubectl apply --server-side --field-manager=dciab --force-conflicts -f -
    kubectl apply --server-side --field-manager=dciab --force-conflicts -f "$workdir/driver.yaml"
    echo "== waiting for the controller and the node plugin"
    python3 - "$workdir/driver.yaml" > "$workdir/workloads.txt" <<'PY'
import sys

import yaml

for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d["kind"] in ("Deployment", "DaemonSet"):
        print(f"{d['kind'].lower()}/{d['metadata']['name']}")
PY
    while read -r workload; do
      kubectl -n "$ns" rollout status "$workload" --timeout=10m
    done < "$workdir/workloads.txt"
    echo "== StorageClasses"
    "$ROOT/lib/steps/storage-classes.sh" "$config" "$cluster" "$kubeconfig"
    ;;
esac
