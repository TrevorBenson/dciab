#!/usr/bin/env bash
# dciab mgmt: build the MGMT cluster from the bootstrap (kind) cluster, then make MGMT
# manage itself.
#
#   dciab mgmt <config> [--bootstrap-kubeconfig FILE] [--state DIR] [--trust-bundle FILE]
#                       [--payload DIR] [--until STEP]
#
# After dciab seed with the same --state, --bootstrap-kubeconfig and --trust-bundle default
# to the files seed wrote (<state>/bootstrap.kubeconfig, <state>/pki/trust-bundle.pem).
# --payload DIR (the archive's payload; default <archive>/payload when it exists) makes
# every chart and manifest come from it instead of the internet, and puts its bin/ first in
# PATH (clusterctl, kubectl, helm), as dciab seed does.
#
# Steps, in order. Each is safe to re-run, and a finished run changes nothing:
#   preflight   tools present; the bootstrap cluster has Cluster API, the providers and the
#               ClusterClasses (dciab seed installs them)
#   values      dciab cluster-values for mgmt -> <state>/mgmt-values.yaml
#   cluster     the platform credentials Secret and the capi-cluster release "mgmt" in
#               namespace cluster-mgmt, on the bootstrap cluster. The Secret is applied
#               server-side; a last-applied annotation an older dciab left on it is removed
#               (the adopt step does the same on MGMT)
#   control-plane
#               wait for the first control plane node; save <state>/mgmt.kubeconfig
#   cpi         vSphere only: addons/vsphere-cpi/install.sh, the cloud provider the vSphere
#               class's kubelets wait for (cloud-provider: external). Until it initializes a
#               node, the node keeps the uninitialized taint, nothing else schedules there, and
#               Cluster API can't match it to its Machine. It needs the network, so it starts
#               working during the cilium step. Other platforms skip it
#   cilium      addons/cilium/install.sh; waits until every node is Ready (on vSphere, also
#               until the CPI has initialized every node). Nodes get DCIAB_NODE_WAIT_MINUTES
#               to register: default 20, 45 on vSphere, where each control plane node joins
#               only after the CPI has initialized the one before
#               Then addons/kubelet-csr-approver/install.sh (kubelet serving certificates),
#               once every node is Ready and, on vSphere, has its CPI addresses
#   providers   cert-manager, Cluster API and the providers on MGMT
#   move        clusterctl move of namespace cluster-mgmt (MGMT's own Cluster, its machines
#               and their Secrets, and copies of the ClusterClasses it uses) from the
#               bootstrap cluster to MGMT; then checks that the Cluster and its Machines are
#               on MGMT and none are left on the bootstrap cluster. First, an encrypted CAPI
#               export of the bootstrap cluster to the site target (docs/BACKUP.md); the
#               move doesn't start without it. The backup key is generated here if the
#               config gives none, and its sealed private half must reach the target first
#   adopt       the classes and mgmt Helm releases on MGMT take over the moved objects; it
#               refuses to run unless the moved Cluster is on MGMT. Then it exports MGMT's
#               CAPI state from MGMT once; a later run retries a failed export
#   storage     nothing, unless spec.storage.block.overrides.mgmt is set: MGMT keeps no
#               volumes (ADR 0008, decision 4a), so by default it gets no Longhorn, no CSI
#               driver and no StorageClass, and the step says so. With overrides.mgmt:
#               that provider and the four StorageClasses, replicated the default
#               (lib/steps/block-storage.sh). longhorn: addons/longhorn/install.sh, with
#               recurring snapshots and backups to the site target (spec.backup,
#               docs/BACKUP.md); vsphere-csi and proxmox-csi: addons/csi/install.sh, the
#               site's array; existing: the classes copy the StorageClass the config names.
#               After the move, so a storage problem never holds up the pivot
#   backups     MGMT's backup jobs (spec.backup, docs/BACKUP.md): the site target's
#               credentials Secret (server-side) and CronJobs for etcd snapshots and CAPI
#               exports, encrypted to the recipients and the backup key. Needs bundle.lock
#               images.backup pinned. The jobs stage on the node's disk (emptyDir) and write
#               to the target, so they need no StorageClass
#
# A Cluster left on both clusters (a move that stopped part-way, or a second Cluster on MGMT
# from an earlier run whose move moved nothing), or paused on the bootstrap cluster, stops
# the run with what to do about it (docs/MGMT.md).
# Each step prints how long it took.
#
# --until STEP stops after STEP (longhorn, the storage step's old name, still works for one
# minor release). <state> defaults to .dciab/state next to the config
# (/var/lib/dciab/work/state on the bootstrap appliance; lib/dciab_state).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STEPS=(preflight values cluster control-plane cpi cilium providers move adopt storage backups)
NS=cluster-mgmt
CLASS_NS=dciab-clusterclasses

die() { echo "dciab mgmt: $*" >&2; exit 1; }
usage() { sed -n '5,6p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2; exit 2; }

config="" bootstrap="" state="" trust="" until="" payload=""
while (( $# )); do
  case "$1" in
    --bootstrap-kubeconfig) bootstrap="${2:?}"; shift 2 ;;
    --state) state="${2:?}"; shift 2 ;;
    --trust-bundle) trust="${2:?}"; shift 2 ;;
    --payload) payload="${2:?}"; shift 2 ;;
    --until) until="${2:?}"; shift 2 ;;
    -*) echo "dciab mgmt: unknown option '$1'" >&2; usage ;;
    *) [[ -z "$config" ]] || usage; config="$1"; shift ;;
  esac
done
[[ -n "$config" ]] || usage
[[ -f "$config" ]] || die "no such config: $config"
# --until longhorn: the storage step's old name, accepted for one minor release.
if [[ "$until" == longhorn ]]; then
  echo "dciab mgmt: --until longhorn is now --until storage; the old name goes in the next minor release" >&2
  until=storage
fi
if [[ -n "$until" ]] && ! printf '%s\n' "${STEPS[@]}" | grep -qx "$until"; then
  die "--until must be one of: ${STEPS[*]}"
fi
config="$(cd "$(dirname "$config")" && pwd)/$(basename "$config")"
state="${state:-$(PYTHONPATH="$ROOT/lib" python3 -m dciab_state "$config")}"
mkdir -p "$state" && chmod 700 "$state"
# The state holds kubeconfigs, whose exec plugins kubectl runs as this user: only this user
# may own it.
[[ "$(stat -c %u "$state")" == "$(id -u)" ]] || die "$state is not owned by $(id -un); refusing to use its kubeconfigs"
# dciab seed's outputs, when this run doesn't name others.
bootstrap="${bootstrap:-$state/bootstrap.kubeconfig}"
[[ -f "$bootstrap" ]] || die "no such kubeconfig: $bootstrap (run dciab seed, or pass --bootstrap-kubeconfig)"
if [[ -z "$trust" && -f "$state/pki/trust-bundle.pem" ]]; then
  trust="$state/pki/trust-bundle.pem"
fi
if [[ -z "$payload" && -d "$ROOT/payload/providers" ]]; then
  payload="$ROOT/payload"
fi
if [[ -n "$payload" ]]; then
  [[ -d "$payload/providers" && -d "$payload/charts" ]] || die "$payload: not an archive payload (no providers/ or charts/)"
  payload="$(cd "$payload" && pwd)"
  # Its binaries and manifests must match its SHA256SUMS before anything is applied from it.
  PYTHONPATH="$ROOT/lib" python3 -c 'import sys; from pathlib import Path; from dciab_seed.cli import verify_payload_tools; from dciab_seed.plan import SeedError
try:
    verify_payload_tools(Path(sys.argv[1]))
except SeedError as exc:
    sys.exit(f"dciab mgmt: {exc}")' "$payload"
  export DCIAB_PAYLOAD="$payload"
  # Its tools come first, as in dciab seed: sudo resets PATH, and the appliance has no
  # clusterctl of its own.
  export PATH="$payload/bin:$PATH"
fi
values="$state/mgmt-values.yaml"
mgmt_kubeconfig="$state/mgmt.kubeconfig"

export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
on_bootstrap() { KUBECONFIG="$bootstrap" "$@"; }
on_mgmt() { KUBECONFIG="$mgmt_kubeconfig" "$@"; }
# "== mgmt: <step> done in Ns" (or "failed after Ns") when the next step starts or the run
# ends, as dciab seed and services print.
step_name="" step_start=0
step_done() {   # step_done <exit status>
  [[ -n "$step_name" ]] || return 0
  if (( $1 )); then
    echo "== mgmt: $step_name failed after $((SECONDS - step_start))s"
  else
    echo "== mgmt: $step_name done in $((SECONDS - step_start))s"
  fi
  step_name=""
}
step() { step_done 0; step_name="$1" step_start=$SECONDS; echo; echo "== mgmt: $1"; }
trap 'step_done $?' EXIT
finish_if() { if [[ "$until" == "$1" ]]; then step_done 0; echo "dciab mgmt: stopped after '$1' (--until)"; exit 0; fi; }

platform="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["spec"]["platform"]["type"])' "$config")"

# One run at a time per state folder: two runs could move objects concurrently.
exec 9>"$state/.lock"
flock -n 9 || die "another dciab mgmt is running with state $state"

# capi_backup <kubeconfig> <label>: an encrypted CAPI export of that cluster to the site
# target, kept in <state>/capi-backup too (lib/dciab_backup). Generates the backup key first
# if there is none. The move step calls it before the move, the adopt step after.
capi_backup() {
  # Called as capi_backup ... || die, where set -e doesn't apply: each command returns on failure.
  python3 -m dciab_backup key "$config" --state "$state" >/dev/null || return
  python3 -m dciab_backup export "$config" --state "$state" --kubeconfig "$1" --label "$2" || return
}

# Where MGMT's Cluster object lives (pivot_state), the move and its checks.
# shellcheck source=lib/steps/mgmt-pivot.sh
source "$ROOT/lib/steps/mgmt-pivot.sh"

step preflight
for tool in kubectl helm clusterctl python3 flock; do
  command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
want="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["clusterAPI"]["core"]["version"])' "$ROOT/bundle.lock")"
have="$(clusterctl version -o short 2>/dev/null || true)"
[[ "$have" == "$want" ]] || die "clusterctl is ${have:-unknown}; bundle.lock pins Cluster API $want and clusterctl must match"
# Read once: values and the cluster steps go by it.
pivot="$(pivot_state)"
# Paused, or on both clusters: stop before anything changes, with what to do.
# rerun: a move stopped part-way, and the bootstrap cluster still has everything and is in
# charge again; the move step moves again.
[[ "$pivot" == bootstrap || "$pivot" == rerun || "$pivot" == mgmt ]] || die "$(pivot_help "$pivot")"
if [[ "$pivot" != mgmt ]]; then
  seed_hint="run dciab seed (or: KUBECONFIG=$bootstrap $ROOT/lib/steps/capi-providers.sh --with-classes)"
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
  # Server-side apply: a client-side apply would keep a copy of the Secret, token or password
  # included, in the last-applied-configuration annotation.
  python3 -m dciab_cluster.credentials "$config" mgmt "$NS" \
    | KUBECONFIG="$1" kubectl apply --server-side --force-conflicts --field-manager=dciab -f -
  # A Secret an older dciab applied client-side still has that annotation, and server-side
  # apply keeps it up to date where it exists: remove it, after the apply. No-op once gone.
  KUBECONFIG="$1" kubectl -n "$NS" annotate secret "mgmt-$platform-credentials" \
    kubectl.kubernetes.io/last-applied-configuration- >/dev/null
}

if [[ "$pivot" == mgmt ]]; then
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

step cpi
if [[ "$platform" == vsphere ]]; then
  # The chart comes from $DCIAB_PAYLOAD/charts when DCIAB_PAYLOAD is set.
  "$ROOT/addons/vsphere-cpi/install.sh" "$config" "$mgmt_kubeconfig"
else
  echo "not needed on $platform"
fi
finish_if cpi

step cilium
if [[ "$platform" == vsphere ]]; then
  export DCIAB_NODE_WAIT_MINUTES="${DCIAB_NODE_WAIT_MINUTES:-45}"
fi
"$ROOT/addons/cilium/install.sh" "$values" "$mgmt_kubeconfig"
if [[ "$platform" == vsphere ]]; then
  "$ROOT/addons/vsphere-cpi/install.sh" "$config" --wait "$mgmt_kubeconfig"
fi
"$ROOT/addons/kubelet-csr-approver/install.sh" "$values" "$mgmt_kubeconfig"
finish_if cilium

step providers
providers_args=()
[[ -n "$payload" ]] && providers_args=(--manifests "$payload/providers")
on_mgmt "$ROOT/lib/steps/capi-providers.sh" "${providers_args[@]}"
finish_if providers

step move
move_mgmt
finish_if move

step adopt
# Only a moved Cluster is adopted: on an empty MGMT, the mgmt release would create a second
# Cluster with the same VIP and node range.
check_moved || die "refusing to adopt: the moved Cluster mgmt is not on MGMT (above)"
# The credentials Secret moves with the Cluster (clusterctl's move label); applying it here
# as well keeps it current when the config's secret changes after the move.
apply_credentials "$mgmt_kubeconfig"
# clusterctl move keeps Helm's ownership annotations, so these releases take the moved
# objects over instead of creating them again.
on_mgmt helm upgrade --install classes "$ROOT/charts/capi-clusterclasses" --namespace "$CLASS_NS" --create-namespace
on_mgmt helm upgrade --install mgmt "$ROOT/charts/capi-cluster" --namespace "$NS" --values "$values"
on_mgmt kubectl -n "$NS" get cluster mgmt
# Once MGMT is moved, export its CAPI state from MGMT itself (ADR 0009, decision 3). Done
# when <state>/capi-backup/LAST records it, so a run after a failed export retries it.
if [[ "$pivot" != mgmt ]] || ! grep -qs ' after-move$' "$state/capi-backup/LAST"; then
  capi_backup "$mgmt_kubeconfig" after-move || die "the CAPI export after the move failed (above); MGMT is moved and adopted. Re-run dciab mgmt: it retries the export"
fi
finish_if adopt

step storage
# Nothing on MGMT by default: it keeps no volumes (ADR 0008, decision 4a). Only
# spec.storage.block.overrides.mgmt gives it block storage and the four StorageClasses.
"$ROOT/lib/steps/block-storage.sh" "$config" mgmt "$mgmt_kubeconfig"
finish_if storage

step backups
python3 -m dciab_backup key "$config" --state "$state" >/dev/null
# Rendered first: an unpinned images.backup stops the step before anything is applied.
backup_jobs="$(python3 -m dciab_backup manifests "$config" mgmt --state "$state")" \
  || die "MGMT's backup jobs can't be rendered (above); nothing was applied"
# The S3 credentials go from the config's reference into the cluster through a pipe,
# server-side, as Longhorn's do. NFS targets take none.
on_mgmt kubectl create namespace dciab-backup --dry-run=client -o yaml | on_mgmt kubectl apply --server-side --field-manager=dciab -f -
if [[ "$(python3 -m dciab_backup site-kind "$config")" == s3 ]]; then
  python3 -m dciab_backup secret "$config" | on_mgmt kubectl apply --server-side --field-manager=dciab --force-conflicts -f -
else
  on_mgmt kubectl -n dciab-backup delete secret dciab-backup-target --ignore-not-found
fi
on_mgmt kubectl apply --server-side --field-manager=dciab --force-conflicts -f - <<<"$backup_jobs"
on_mgmt kubectl -n dciab-backup get cronjobs
step_done 0
echo
echo "dciab mgmt: done. MGMT manages itself; kubeconfig: $mgmt_kubeconfig"
