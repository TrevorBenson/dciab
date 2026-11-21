#!/usr/bin/env bash
# dciab mgmt: build the MGMT cluster from the bootstrap (kind) cluster, then make MGMT
# manage itself.
#
#   dciab mgmt <config> --bootstrap-kubeconfig FILE [--state DIR] [--trust-bundle FILE] [--until STEP]
#
# Steps, in order. Each is safe to re-run, and a finished run changes nothing:
#   preflight   tools present; the bootstrap cluster has Cluster API, the providers and the
#               ClusterClasses (dciab seed installs them)
#   values      dciab cluster-values for mgmt -> <state>/mgmt-values.yaml
#   cluster     the platform credentials Secret and the capi-cluster release "mgmt" in
#               namespace cluster-mgmt, on the bootstrap cluster
#   control-plane
#               wait for the first control plane node; save <state>/mgmt.kubeconfig
#   cilium      addons/cilium/install.sh; waits until every node is Ready
#   providers   cert-manager, Cluster API and the providers on MGMT
#   move        clusterctl move: every CAPI object, MGMT's own Cluster and the
#               ClusterClasses, from the bootstrap cluster to MGMT
#   adopt       the classes and mgmt Helm releases on MGMT take over the moved objects
#
# --until STEP stops after STEP. <state> defaults to .dciab/state next to the config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STEPS=(preflight values cluster control-plane cilium providers move adopt)
NS=cluster-mgmt
CLASS_NS=dciab-clusterclasses

die() { echo "dciab mgmt: $*" >&2; exit 1; }
usage() { sed -n '5p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2; exit 2; }

config="" bootstrap="" state="" trust="" until=""
while (( $# )); do
  case "$1" in
    --bootstrap-kubeconfig) bootstrap="${2:?}"; shift 2 ;;
    --state) state="${2:?}"; shift 2 ;;
    --trust-bundle) trust="${2:?}"; shift 2 ;;
    --until) until="${2:?}"; shift 2 ;;
    -*) echo "dciab mgmt: unknown option '$1'" >&2; usage ;;
    *) [[ -z "$config" ]] || usage; config="$1"; shift ;;
  esac
done
[[ -n "$config" && -n "$bootstrap" ]] || usage
[[ -f "$config" ]] || die "no such config: $config"
[[ -f "$bootstrap" ]] || die "no such kubeconfig: $bootstrap"
if [[ -n "$until" ]] && ! printf '%s\n' "${STEPS[@]}" | grep -qx "$until"; then
  die "--until must be one of: ${STEPS[*]}"
fi
config="$(cd "$(dirname "$config")" && pwd)/$(basename "$config")"
state="${state:-$(dirname "$config")/.dciab/state}"
mkdir -p "$state" && chmod 700 "$state"
values="$state/mgmt-values.yaml"
mgmt_kubeconfig="$state/mgmt.kubeconfig"

export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
on_bootstrap() { KUBECONFIG="$bootstrap" "$@"; }
on_mgmt() { KUBECONFIG="$mgmt_kubeconfig" "$@"; }
step() { echo; echo "== mgmt: $1"; }
finish_if() { if [[ "$until" == "$1" ]]; then echo "dciab mgmt: stopped after '$1' (--until)"; exit 0; fi; }

platform="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["spec"]["platform"]["type"])' "$config")"

# One run at a time per state folder: two runs could move objects concurrently.
exec 9>"$state/.lock"
flock -n 9 || die "another dciab mgmt is running with state $state"

# Where MGMT's Cluster object lives: before the move on the bootstrap cluster, after it on
# MGMT. A failed lookup (API down, access denied) stops the run instead of reading as absent.
cluster_on() {   # cluster_on <kubeconfig>: prints the Cluster's name if it exists there
  KUBECONFIG="$1" kubectl -n "$NS" get cluster mgmt --ignore-not-found -o name
}
moved() {
  local on_m on_b
  [[ -f "$mgmt_kubeconfig" ]] || return 1
  on_m="$(cluster_on "$mgmt_kubeconfig")" || return 1   # MGMT not reachable yet: not moved
  on_b="$(cluster_on "$bootstrap")" || die "can't read Clusters on the bootstrap cluster"
  [[ -n "$on_m" && -z "$on_b" ]]
}

step preflight
for tool in kubectl helm clusterctl python3 flock; do
  command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
want="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["clusterAPI"]["core"]["version"])' "$ROOT/bundle.lock")"
have="$(clusterctl version -o short 2>/dev/null || true)"
[[ "$have" == "$want" ]] || die "clusterctl is ${have:-unknown}; bundle.lock pins Cluster API $want and clusterctl must match"
if ! moved; then
  # dciab seed will set the bootstrap cluster up; until it exists, lib/steps/capi-providers.sh
  # --with-classes does that part.
  seed_hint="install them with: KUBECONFIG=$bootstrap $ROOT/lib/steps/capi-providers.sh --with-classes"
  on_bootstrap kubectl -n capi-system get deployment capi-controller-manager >/dev/null 2>&1 \
    || die "the bootstrap cluster has no Cluster API; $seed_hint"
  on_bootstrap kubectl -n "$CLASS_NS" get clusterclasses -o name | grep -q "dciab-$platform-" \
    || die "the bootstrap cluster has no dciab-$platform ClusterClass; $seed_hint"
fi
finish_if preflight

step values
args=("$config" mgmt -o "$values")
[[ -n "$trust" ]] && args+=(--trust-bundle "$trust")
python3 -m dciab_cluster --root "$ROOT" "${args[@]}"
echo "wrote $values"
finish_if values

apply_credentials() {   # apply_credentials <kubeconfig>
  KUBECONFIG="$1" kubectl create namespace "$NS" --dry-run=client -o yaml | KUBECONFIG="$1" kubectl apply -f -
  python3 -m dciab_cluster.credentials "$config" mgmt "$NS" | KUBECONFIG="$1" kubectl apply -f -
}

if moved; then
  echo "MGMT already manages itself; checking the remaining steps"
  finish_if cluster
else
  step cluster
  apply_credentials "$bootstrap"
  on_bootstrap helm upgrade --install mgmt "$ROOT/charts/capi-cluster" --namespace "$NS" --values "$values"
  finish_if cluster

  step control-plane
  on_bootstrap kubectl -n "$NS" wait cluster/mgmt --for=condition=ControlPlaneInitialized --timeout=45m
  tmp="$(mktemp "$state/.mgmt.kubeconfig.XXXXXX")"
  on_bootstrap kubectl -n "$NS" get secret mgmt-kubeconfig -o jsonpath='{.data.value}' | base64 -d > "$tmp"
  chmod 600 "$tmp" && mv "$tmp" "$mgmt_kubeconfig"
  on_mgmt kubectl get --raw /readyz >/dev/null || die "MGMT's API at its VIP doesn't answer"
  echo "saved $mgmt_kubeconfig"
fi
finish_if control-plane

step cilium
"$ROOT/addons/cilium/install.sh" "$values" "$mgmt_kubeconfig"
finish_if cilium

step providers
on_mgmt "$ROOT/lib/steps/capi-providers.sh"
finish_if providers

step move
if moved; then
  echo "already moved"
else
  # clusterctl move takes every CAPI object on the bootstrap cluster: it must hold MGMT only.
  others="$(on_bootstrap kubectl get clusters -A --no-headers -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name \
    | awk -v ns="$NS" '!($1 == ns && $2 == "mgmt")')"
  [[ -z "$others" ]] || die "the bootstrap cluster holds other Clusters, which the move would take to MGMT: $others"
  on_bootstrap kubectl -n "$NS" wait cluster/mgmt --for=condition=Available --timeout=45m
  clusterctl move --kubeconfig "$bootstrap" --to-kubeconfig "$mgmt_kubeconfig"
fi
finish_if move

step adopt
# The credentials Secret moves with the Cluster (clusterctl's move label); applying it here
# as well keeps it current when the config's secret changes after the move.
apply_credentials "$mgmt_kubeconfig"
# clusterctl move keeps Helm's ownership annotations, so these releases take the moved
# objects over instead of creating them again.
on_mgmt helm upgrade --install classes "$ROOT/charts/capi-clusterclasses" --namespace "$CLASS_NS" --create-namespace
on_mgmt helm upgrade --install mgmt "$ROOT/charts/capi-cluster" --namespace "$NS" --values "$values"
on_mgmt kubectl -n "$NS" get cluster mgmt
echo
echo "dciab mgmt: done. MGMT manages itself; kubeconfig: $mgmt_kubeconfig"
