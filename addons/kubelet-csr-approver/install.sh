#!/usr/bin/env bash
# Install or upgrade kubelet-csr-approver on one DCiaB cluster.
#
#   addons/kubelet-csr-approver/install.sh <capi-cluster values> <kubeconfig>    install, then wait for it
#   addons/kubelet-csr-approver/install.sh <capi-cluster values> --template      render the manifests; no cluster needed
#   addons/kubelet-csr-approver/install.sh <capi-cluster values> --print-values  print the per-cluster values
#
# <capi-cluster values> is the values file the cluster was created with (charts/capi-cluster).
# The chart version and source come from bundle.lock; DCiaB's defaults from values.yaml here.
# With DCIAB_PAYLOAD set (an archive payload folder), the chart comes from its charts/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
NAME=kubelet-csr-approver

usage() {
  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
[[ $# -eq 2 ]] || usage
cluster_values="$1"
mode="$2"
[[ -f "$cluster_values" ]] || { echo "install.sh: no such file: $cluster_values" >&2; exit 2; }

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
overrides="$workdir/overrides.yaml"

# The networks a serving certificate may name addresses in: every node network of the
# cluster, from the cluster values. The chart pin from bundle.lock.
read -r version repo digest < <(python3 - "$ROOT/bundle.lock" "$cluster_values" "$overrides" <<'PY'
import ipaddress
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
cluster = yaml.safe_load(open(sys.argv[2]))
if not cluster.get("name"):
    sys.exit(f"install.sh: {sys.argv[2]} has no name")
networks = [cluster["network"]] if "network" in cluster else (
    [cluster["controlPlane"]["network"]] + [p["network"] for p in cluster.get("workerPools", [])])
prefixes = []
for n in networks:
    start = n["nodeIPRange"].split("-")[0]
    net = str(ipaddress.ip_network(f"{start}/{n['prefix']}", strict=False))
    if net not in prefixes:
        prefixes.append(net)
yaml.safe_dump({"providerIpPrefixes": prefixes}, open(sys.argv[3], "w"), sort_keys=False)
print(lock["charts"]["kubelet-csr-approver"], lock["chartRepositories"]["kubelet-csr-approver"],
      lock.get("chartDigests", {}).get("kubelet-csr-approver") or "-")
PY
)

# Fetch the pinned chart once and check its sha256 against bundle.lock (chartDigests).
fetch_chart() {
  local tgz sum
  if [[ -n "${DCIAB_PAYLOAD:-}" ]]; then
    # Offline: the chart the archive vendored (build/archive.py charts).
    tgz="$DCIAB_PAYLOAD/charts/$NAME-$version.tgz"
    [[ -f "$tgz" ]] || { echo "install.sh: $tgz: no such file in the payload" >&2; exit 1; }
  else
    helm pull "$NAME" --repo "$repo" --version "$version" --destination "$workdir" >/dev/null
    tgz="$(ls "$workdir/$NAME"-*.tgz)"
  fi
  sum="$(sha256sum "$tgz" | cut -d' ' -f1)"
  if [[ "$digest" == - ]]; then
    echo "install.sh: warning: bundle.lock has no chartDigests.$NAME; this chart's sha256 is $sum" >&2
  elif [[ "$sum" != "$digest" ]]; then
    echo "install.sh: $NAME $version chart sha256 is $sum, bundle.lock pins $digest; refusing" >&2
    exit 1
  fi
  echo "$tgz"
}
values=(--namespace kube-system --values "$HERE/values.yaml" --values "$overrides")

case "$mode" in
  --print-values)
    echo "# $NAME $version from $repo"
    cat "$overrides"
    ;;
  --template)
    tgz="$(fetch_chart)"
    helm template "$NAME" "$tgz" "${values[@]}"
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    echo "== $NAME $version on $(KUBECONFIG="$kubeconfig" kubectl config current-context)"
    tgz="$(fetch_chart)"
    helm upgrade --install "$NAME" "$tgz" "${values[@]}" --kubeconfig "$kubeconfig" --wait --timeout 5m
    ;;
esac
