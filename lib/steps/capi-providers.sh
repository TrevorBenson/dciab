#!/usr/bin/env bash
# Install cert-manager, Cluster API and every provider pinned in bundle.lock into the
# cluster KUBECONFIG points at. Re-running changes nothing.
#
#   lib/steps/capi-providers.sh [--with-classes] [--manifests DIR]
#
# --with-classes also installs the capi-clusterclasses chart. --manifests DIR uses those
# manifests (the archive payload's providers/ folder) instead of downloading them. Provider ${VARIABLES} come
# from bundle.lock (clusterAPI.variables); the environment overrides declared ones.
# Without --manifests, they come from hack/fetch_crds.py, checked against bundle.lock's
# checksums.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLASS_NS=dciab-clusterclasses

with_classes=false
manifests=""
while (( $# )); do
  case "$1" in
    --with-classes) with_classes=true; shift ;;
    --manifests) manifests="${2:?--manifests needs a folder}"; shift 2 ;;
    *) echo "capi-providers.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

# retry <attempts> <sleep seconds> <command...>: webhooks need a moment after their
# Deployments report Available, so the first applies may be refused.
retry() {
  local attempts="$1" pause="$2" n=1
  shift 2
  until "$@"; do
    if (( n >= attempts )); then
      echo "giving up after $attempts attempts: $*" >&2
      return 1
    fi
    echo "attempt $n failed; retrying in ${pause}s" >&2
    sleep "$pause"
    n=$((n + 1))
  done
}

# Rendered components may hold credentials passed through the environment: keep them in
# a private file and remove it on exit.
components="$(mktemp)"
chmod 600 "$components"
trap 'rm -f "$components"' EXIT

if [[ -n "$manifests" ]]; then
  [[ -d "$manifests" ]] || { echo "capi-providers.sh: no such folder: $manifests" >&2; exit 2; }
  crds="$manifests"
else
  crds="$(python3 "$ROOT/hack/fetch_crds.py")"
fi

group "cert-manager"
kubectl apply --server-side -f "$crds"/cert-manager-*/cert-manager.yaml
kubectl -n cert-manager wait --for=condition=Available deployment --all --timeout=5m
endgroup

group "Bare Metal Operator CRDs (CAPM3 watches BareMetalHosts)"
kubectl apply --server-side -f "$crds"/baremetal-operator-*/
endgroup

group "Cluster API and providers"
python3 "$ROOT/hack/render_components.py" \
  "$crds"/cluster-api-*/*.yaml "$crds"/infrastructure-*/*.yaml "$crds"/ipam-*/*.yaml > "$components"
retry 5 15 kubectl apply --server-side --force-conflicts -f "$components"
kubectl wait --for=condition=Available deployment --all -A --timeout=10m
kubectl get deployments -A
endgroup

if [[ "$with_classes" == true ]]; then
  group "ClusterClasses"
  retry 10 15 helm upgrade --install classes "$ROOT/charts/capi-clusterclasses" \
    --namespace "$CLASS_NS" --create-namespace --wait
  kubectl -n "$CLASS_NS" get clusterclasses
  endgroup
fi
