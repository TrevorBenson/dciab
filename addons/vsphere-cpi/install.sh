#!/usr/bin/env bash
# Install or upgrade the vSphere cloud provider (CPI) on one DCiaB cluster on vSphere.
#
#   addons/vsphere-cpi/install.sh <config> <kubeconfig> [--chart TGZ]   install or upgrade; doesn't wait
#   addons/vsphere-cpi/install.sh <config> --wait <kubeconfig>          wait until the CPI has initialized every node
#   addons/vsphere-cpi/install.sh <config> --template [--chart TGZ]     render the manifests; no cluster needed
#   addons/vsphere-cpi/install.sh <config> --print-values               print the per-site values
#
# The CPI reaches the API server at its Service address, which works only once the CNI
# (Cilium, replacing kube-proxy) runs. Install it before Cilium, so it is there when the
# network comes up, and --wait after Cilium.
# <config> is the site config (spec.platform.vsphere: server, thumbprint, datacenter and the
# credentials reference). The chart comes from --chart, else from $DCIAB_PAYLOAD/charts when
# DCIAB_PAYLOAD is set, else from the pinned repository; its sha256 is checked against
# bundle.lock chartDigests.vsphere-cpi. DCiaB's defaults: values.yaml here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
NS=kube-system
NAME=vsphere-cpi
TAINT=node.cloudprovider.kubernetes.io/uninitialized

usage() {
  sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
[[ $# -eq 2 || $# -eq 3 || $# -eq 4 ]] || usage
config="$1"
mode="$2"
chart="" kubeconfig=""
if [[ "$mode" == --wait ]]; then
  [[ $# -eq 3 ]] || usage
  kubeconfig="$3"
elif [[ $# -eq 4 && "$3" == --chart ]]; then
  chart="$4"
elif [[ $# -ne 2 ]]; then
  usage
fi
[[ -f "$config" ]] || { echo "install.sh: no such file: $config" >&2; exit 2; }
if [[ -n "$chart" ]]; then
  [[ -f "$chart" ]] || { echo "install.sh: no such chart: $chart" >&2; exit 2; }
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
overrides="$workdir/site-values.yaml"

python3 "$HERE/cpi_values.py" values "$config" > "$overrides"
read -r version repo digest < <(python3 - "$ROOT/bundle.lock" "$NAME" <<'PY'
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
name = sys.argv[2]
digest = (lock.get("chartDigests") or {}).get(name) or "-"
print(lock["charts"][name], lock["chartRepositories"][name], digest)
PY
)

# The chart: --chart, the payload's vendored copy, or the pinned repository. Checked against
# bundle.lock (chartDigests) whichever it is.
get_chart() {
  local tgz sum
  if [[ -n "$chart" ]]; then
    tgz="$chart"
  elif [[ -n "${DCIAB_PAYLOAD:-}" ]]; then
    tgz="$DCIAB_PAYLOAD/charts/$NAME-$version.tgz"
    [[ -f "$tgz" ]] || { echo "install.sh: the payload has no $tgz" >&2; exit 1; }
  else
    helm pull "$NAME" --repo "$repo" --version "$version" --destination "$workdir" >/dev/null
    tgz="$workdir/$NAME-$version.tgz"
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
values=(--namespace "$NS" --values "$HERE/values.yaml" --values "$overrides")

case "$mode" in
  --print-values)
    echo "# $NAME $version from $repo"
    cat "$overrides"
    ;;
  --template)
    tgz="$(get_chart)"
    helm template "$NAME" "$tgz" "${values[@]}"
    ;;
  --wait)
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    export KUBECONFIG="$kubeconfig"
    kubectl -n "$NS" rollout status "daemonset/$NAME" --timeout=10m
    # The CPI clears the taint and sets each node's provider ID, which Cluster API matches
    # Machines by.
    echo "== waiting until no node carries $TAINT"
    left=""
    for _ in $(seq 1 120); do
      left="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.taints[*].key}{"\n"}{end}' \
        | awk -v t="$TAINT" '{for (i = 2; i <= NF; i++) if ($i == t) print $1}')"
      [[ -z "$left" ]] && break
      sleep 5
    done
    [[ -z "$left" ]] || { echo "install.sh: still uninitialized after 10 minutes: $left" >&2; exit 1; }
    kubectl get nodes -o custom-columns=NAME:.metadata.name,PROVIDER-ID:.spec.providerID
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    export KUBECONFIG="$kubeconfig"
    echo "== $NAME $version on $(kubectl config current-context)"
    tgz="$(get_chart)"
    # vCenter credentials go from the config's reference straight into the cluster through a
    # pipe (pipefail stops the install on a bad reference). Server-side apply: a client-side
    # apply would keep a copy of the Secret, password included, in the
    # last-applied-configuration annotation.
    python3 "$HERE/cpi_values.py" secret "$config" "$NS" \
      | kubectl apply --server-side --force-conflicts --field-manager=dciab -f -
    # No --wait: without the CNI the CPI can't reach the API yet (see the header).
    helm upgrade --install "$NAME" "$tgz" "${values[@]}"
    ;;
esac
