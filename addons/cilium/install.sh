#!/usr/bin/env bash
# Install or upgrade Cilium on one DCiaB cluster.
#
#   addons/cilium/install.sh <capi-cluster values> <kubeconfig>    install, then wait for nodes Ready
#   addons/cilium/install.sh <capi-cluster values> --template      render the manifests; no cluster needed
#   addons/cilium/install.sh <capi-cluster values> --print-values  print the per-cluster values
#
# <capi-cluster values> is the values file the cluster was created with (charts/capi-cluster).
# The chart version and source come from bundle.lock; DCiaB's defaults from values.yaml here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

usage() {
  sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
[[ $# -eq 2 ]] || usage
cluster_values="$1"
mode="$2"
[[ -f "$cluster_values" ]] || { echo "install.sh: no such file: $cluster_values" >&2; exit 2; }

overrides="$(mktemp)"
trap 'rm -f "$overrides"' EXIT

# Per-cluster values, and the chart pin, from the cluster values and bundle.lock.
read -r version repo digest nodes < <(python3 - "$ROOT/bundle.lock" "$cluster_values" "$overrides" <<'PY'
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
cluster = yaml.safe_load(open(sys.argv[2]))
for key in ("name", "apiVIP"):
    if not cluster.get(key):
        sys.exit(f"install.sh: {sys.argv[2]} has no {key}")
values = {
    "k8sServiceHost": cluster["apiVIP"],
    "cluster": {"name": cluster["name"]},
}
yaml.safe_dump(values, open(sys.argv[3], "w"), sort_keys=False)
# Nodes the cluster should end up with: control plane plus every worker pool.
nodes = cluster.get("controlPlane", {}).get("replicas", 0) + sum(
    p.get("replicas", 0) for p in cluster.get("workerPools", []))
digest = lock.get("chartDigests", {}).get("cilium") or "-"
print(lock["charts"]["cilium"], lock["chartRepositories"]["cilium"], digest, nodes)
PY
)

# Fetch the pinned chart once and check its sha256 against bundle.lock (chartDigests).
workdir="$(mktemp -d)"
trap 'rm -f "$overrides"; rm -rf "$workdir"' EXIT
fetch_chart() {
  helm pull cilium --repo "$repo" --version "$version" --destination "$workdir" >/dev/null
  local tgz sum
  tgz="$(ls "$workdir"/cilium-*.tgz)"
  sum="$(sha256sum "$tgz" | cut -d' ' -f1)"
  if [[ "$digest" == - ]]; then
    echo "install.sh: warning: bundle.lock has no chartDigests.cilium; this chart's sha256 is $sum" >&2
  elif [[ "$sum" != "$digest" ]]; then
    echo "install.sh: cilium $version chart sha256 is $sum, bundle.lock pins $digest; refusing" >&2
    exit 1
  fi
  echo "$tgz"
}
values=(--namespace kube-system --values "$HERE/values.yaml" --values "$overrides")

case "$mode" in
  --print-values)
    echo "# cilium $version from $repo"
    cat "$overrides"
    ;;
  --template)
    tgz="$(fetch_chart)"
    helm template cilium "$tgz" "${values[@]}"
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    echo "== cilium $version on $(KUBECONFIG="$kubeconfig" kubectl config current-context)"
    tgz="$(fetch_chart)"
    helm upgrade --install cilium "$tgz" "${values[@]}" --kubeconfig "$kubeconfig" --wait --timeout 10m
    # Nodes still joining aren't listed yet, so wait for the expected count, then Ready.
    echo "== waiting for $nodes nodes to register and be Ready"
    registered=0
    for _ in $(seq 1 120); do
      registered="$(kubectl --kubeconfig "$kubeconfig" get nodes --no-headers 2>/dev/null | wc -l)"
      (( registered >= nodes )) && break
      sleep 10
    done
    if (( registered < nodes )); then
      echo "install.sh: only $registered of $nodes nodes registered after 20 minutes" >&2
      exit 1
    fi
    kubectl --kubeconfig "$kubeconfig" wait node --all --for=condition=Ready --timeout=10m
    kubectl --kubeconfig "$kubeconfig" get nodes -o wide
    ;;
esac
