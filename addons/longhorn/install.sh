#!/usr/bin/env bash
# Install or upgrade Longhorn on one DCiaB cluster.
#
#   addons/longhorn/install.sh <config> <kubeconfig> [--chart TGZ]    install, then wait for the manager and StorageClass
#   addons/longhorn/install.sh <config> --template [--chart TGZ]      render the manifests; no cluster needed
#   addons/longhorn/install.sh <config> --images [--chart TGZ]        every image the release runs, one per line
#   addons/longhorn/install.sh <config> --print-values                print the per-site values
#
# <config> is the site config (spec.storage.longhorn). The chart comes from --chart, else from
# $DCIAB_PAYLOAD/charts when DCIAB_PAYLOAD is set, else from the pinned repository; its sha256
# is checked against bundle.lock chartDigests.longhorn. DCiaB's defaults: values.yaml here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
NS=longhorn-system

usage() {
  sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
[[ $# -eq 2 || $# -eq 4 ]] || usage
config="$1"
mode="$2"
chart=""
if [[ $# -eq 4 ]]; then
  [[ "$3" == --chart ]] || usage
  chart="$4"
fi
[[ -f "$config" ]] || { echo "install.sh: no such file: $config" >&2; exit 2; }
if [[ -n "$chart" ]]; then
  [[ -f "$chart" ]] || { echo "install.sh: no such chart: $chart" >&2; exit 2; }
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
overrides="$workdir/site-values.yaml"

python3 "$HERE/longhorn_values.py" values "$config" > "$overrides"
read -r version repo digest < <(python3 - "$ROOT/bundle.lock" <<'PY'
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
digest = (lock.get("chartDigests") or {}).get("longhorn") or "-"
print(lock["charts"]["longhorn"], lock["chartRepositories"]["longhorn"], digest)
PY
)

# The chart: --chart, the payload's vendored copy, or the pinned repository. Checked against
# bundle.lock (chartDigests) whichever it is.
get_chart() {
  local tgz sum
  if [[ -n "$chart" ]]; then
    tgz="$chart"
  elif [[ -n "${DCIAB_PAYLOAD:-}" ]]; then
    tgz="$DCIAB_PAYLOAD/charts/longhorn-$version.tgz"
    [[ -f "$tgz" ]] || { echo "install.sh: the payload has no $tgz" >&2; exit 1; }
  else
    helm pull longhorn --repo "$repo" --version "$version" --destination "$workdir" >/dev/null
    tgz="$workdir/longhorn-$version.tgz"
  fi
  sum="$(sha256sum "$tgz" | cut -d' ' -f1)"
  if [[ "$digest" == - ]]; then
    echo "install.sh: warning: bundle.lock has no chartDigests.longhorn; this chart's sha256 is $sum" >&2
  elif [[ "$sum" != "$digest" ]]; then
    echo "install.sh: longhorn $version chart sha256 is $sum, bundle.lock pins $digest; refusing" >&2
    exit 1
  fi
  echo "$tgz"
}
values=(--namespace "$NS" --values "$HERE/values.yaml" --values "$overrides")

case "$mode" in
  --print-values)
    echo "# longhorn $version from $repo"
    cat "$overrides"
    ;;
  --template)
    tgz="$(get_chart)"
    helm template longhorn "$tgz" "${values[@]}"
    ;;
  --images)
    tgz="$(get_chart)"
    helm show values "$tgz" > "$workdir/chart-values.yaml"
    python3 "$HERE/longhorn_values.py" images "$workdir/chart-values.yaml" "$HERE/values.yaml" "$overrides"
    ;;
  -*)
    usage
    ;;
  *)
    kubeconfig="$mode"
    [[ -f "$kubeconfig" ]] || { echo "install.sh: no such kubeconfig: $kubeconfig" >&2; exit 2; }
    export KUBECONFIG="$kubeconfig"
    echo "== longhorn $version on $(kubectl config current-context)"
    tgz="$(get_chart)"
    kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
    # S3 credentials go from the config's reference straight into the cluster through a pipe
    # (pipefail stops the install on a bad credentials file). NFS targets take none.
    if grep -q backupTargetCredentialSecret "$overrides"; then
      python3 "$HERE/longhorn_values.py" secret "$config" "$NS" | kubectl apply -f -
    fi
    helm upgrade --install longhorn "$tgz" "${values[@]}" --wait --timeout 15m
    echo "== waiting for the Longhorn manager on every node and the default StorageClass"
    kubectl -n "$NS" rollout status daemonset/longhorn-manager --timeout=10m
    for _ in $(seq 1 60); do
      default="$(kubectl get storageclass longhorn --ignore-not-found \
        -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}')"
      [[ "$default" == true ]] && break
      sleep 5
    done
    [[ "${default:-}" == true ]] || { echo "install.sh: StorageClass longhorn isn't the default after 5 minutes" >&2; exit 1; }
    kubectl get storageclass
    ;;
esac
