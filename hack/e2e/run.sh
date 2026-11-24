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

# cert-manager, Cluster API, the providers and the ClusterClasses: the same step dciab
# uses on site.
"$ROOT/lib/steps/capi-providers.sh" --with-classes

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

# The classes' CEL rules keep every issuer's users and groups out of system: (system:masters
# bypasses RBAC). CAPI's webhook must refuse a Cluster whose issuer prefix reaches it.
group "An issuer prefix in system: is refused"
oidc="$ROOT/charts/capi-cluster/ci/proxmox-oidc-values.yaml"
bad="$(mktemp -d)"
for prefix in "system:" "sys"; do
  python3 - "$oidc" "$bad/values.yaml" "$prefix" <<'PY'
import sys, yaml
v = yaml.safe_load(open(sys.argv[1]))
v["nodes"]["authentication"]["issuers"][1]["groupsPrefix"] = sys.argv[3]
yaml.safe_dump({"nodes": {"authentication": v["nodes"]["authentication"]}}, open(sys.argv[2], "w"))
PY
  helm template mgmt "$ROOT/charts/capi-cluster" --namespace cluster-mgmt --values "$oidc" --values "$bad/values.yaml" \
    --show-only templates/cluster.yaml > "$bad/cluster.yaml"
  if kubectl apply --dry-run=server -f "$bad/cluster.yaml" > "$bad/out" 2>&1; then
    echo "a Cluster with groupsPrefix '$prefix' was accepted" >&2; rm -rf "$bad"; exit 1
  fi
  grep -q "system:" "$bad/out" || { cat "$bad/out" >&2; rm -rf "$bad"; exit 1; }
  echo "ok: groupsPrefix '$prefix' refused"
done
rm -rf "$bad"
endgroup
