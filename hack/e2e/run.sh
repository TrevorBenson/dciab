#!/usr/bin/env bash
# ClusterClass end-to-end test.
#
# Installs cert-manager, Cluster API and every provider pinned in bundle.lock into the
# current kubeconfig context (a throwaway kind cluster), with the same ${VARIABLES}
# DCiaB uses on site. Then it installs the ClusterClasses and one Cluster per class
# from charts/capi-cluster/ci, and lets CAPI's topology controller apply the patches.
# hack/e2e/verify.py checks the objects CAPI generates.
#
# No VM or server is built: the example vCenter, Proxmox and BMCs don't exist, so the
# providers stop after the topology step. That step is what this test covers.
#
# DCIAB_E2E_MANAGEMENT_ONLY=1 stops after the ClusterClasses: a management cluster for
# building real clusters, with no example Clusters in it.
#
# Needs: kubectl, helm, python3 with PyYAML, and a disposable kind cluster as the current
# context. It refuses any context not named kind-*; set DCIAB_E2E_CONTEXT to the exact
# context name to use another disposable cluster.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLASS_NS=dciab-clusterclasses

context="$(kubectl config current-context)"
if [[ "$context" != kind-* && "$context" != "${DCIAB_E2E_CONTEXT:-}" ]]; then
  echo "run.sh: refusing to install into context '$context'. This test installs Cluster API" >&2
  echo "cluster-wide; point kubectl at a disposable kind cluster, or set DCIAB_E2E_CONTEXT=$context." >&2
  exit 2
fi

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

diagnostics() {
  echo "::group::diagnostics"
  kubectl get clusters,kubeadmcontrolplanes,machinedeployments -A -o wide || true
  kubectl get clusters -A -o yaml | sed -n '/^  status:/,/^- apiVersion/p' | head -200 || true
  kubectl get events -A --sort-by=.lastTimestamp | tail -60 || true
  kubectl -n capi-system logs deploy/capi-controller-manager --tail=200 || true
  echo "::endgroup::"
}
trap 'diagnostics' ERR

# Rendered components may hold credentials passed through the environment: keep them in
# a private file and remove it on exit.
components="$(mktemp)"
chmod 600 "$components"
trap 'rm -f "$components"' EXIT

crds="$(python3 "$ROOT/hack/fetch_crds.py")"

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

group "ClusterClasses"
retry 10 15 helm upgrade --install classes "$ROOT/charts/capi-clusterclasses" \
  --namespace "$CLASS_NS" --create-namespace --wait
kubectl -n "$CLASS_NS" get clusterclasses
endgroup

if [[ "${DCIAB_E2E_MANAGEMENT_ONLY:-}" == 1 ]]; then
  echo "Management cluster ready: CAPI, providers and ClusterClasses installed; no Clusters created."
  exit 0
fi

for values in "$ROOT"/charts/capi-cluster/ci/*-values.yaml; do
  read -r cluster class < <(python3 -c 'import sys, yaml; v = yaml.safe_load(open(sys.argv[1])); print(v["name"], v["class"])' "$values")
  ns="cluster-$cluster"
  group "Cluster $cluster ($class)"
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  # Credentials Secrets the classes reference. Placeholder values: nothing is reached.
  case "$class" in
    vsphere)
      kubectl -n "$ns" create secret generic "$cluster-vsphere-credentials" \
        --from-literal=username=e2e --from-literal=password=e2e --dry-run=client -o yaml | kubectl apply -f - ;;
    proxmox)
      kubectl -n "$ns" create secret generic "$cluster-proxmox-credentials" \
        --from-literal=url=https://pve.invalid:8006 "--from-literal=token=e2e@pve!e2e" --from-literal=secret=e2e \
        --dry-run=client -o yaml | kubectl apply -f - ;;
  esac
  retry 10 15 helm upgrade --install "$cluster" "$ROOT/charts/capi-cluster" --namespace "$ns" --values "$values"
  endgroup
done

group "Verify what the topology controller generated"
python3 "$ROOT/hack/e2e/verify.py" "$ROOT"/charts/capi-cluster/ci/*-values.yaml
endgroup
