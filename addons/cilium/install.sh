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
read -r version repo < <(python3 - "$ROOT/bundle.lock" "$cluster_values" "$overrides" <<'PY'
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
print(lock["charts"]["cilium"], lock["chartRepositories"]["cilium"])
PY
)

chart=(cilium --repo "$repo" --version "$version" --namespace kube-system
       --values "$HERE/values.yaml" --values "$overrides")

case "$mode" in
  --print-values)
    echo "# cilium $version from $repo"
    cat "$overrides"
    ;;
  --template)
    helm template "${chart[@]}"
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    echo "== cilium $version on $(KUBECONFIG="$kubeconfig" kubectl config current-context)"
    helm upgrade --install "${chart[@]}" --kubeconfig "$kubeconfig" --wait --timeout 10m
    echo "== waiting for every node to be Ready"
    kubectl --kubeconfig "$kubeconfig" wait node --all --for=condition=Ready --timeout=10m
    kubectl --kubeconfig "$kubeconfig" get nodes -o wide
    ;;
esac
