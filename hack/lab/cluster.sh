#!/usr/bin/env bash
# A real cluster on Proxmox from the dciab-proxmox class (task 0003's steps), one subcommand
# per step so a workflow shows each result and time:
#
#   hack/lab/cluster.sh kind                     a throwaway kind management cluster
#   hack/lab/cluster.sh values TEMPLATE_NAME     the cluster's values, secrets and CA
#   hack/lab/cluster.sh build                    helm install; wait for the milestones
#   hack/lab/cluster.sh cilium                   addons/cilium/install.sh
#   hack/lab/cluster.sh checks                   kube-proxy replacement, CIDRs, services, VIP, nodes
#   hack/lab/cluster.sh replace                  replace a worker, then a control plane node
#   hack/lab/cluster.sh down                     uninstall, wait for the VMs to go, delete kind
#
# Environment: PVE_* (hack/lab/pve.sh: only to look at VMs), CAPMOX_TOKEN_ID and
# CAPMOX_TOKEN_SECRET (the credentials Secret CAPMOX uses), handoff/lab/network.yaml.
# State lives in $LAB_DIR (default ~/.dciab-ci); nothing is written to the repository.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/hack/lab/pve.sh"
LAB_DIR="${LAB_DIR:-$HOME/.dciab-ci}"; mkdir -p "$LAB_DIR"; chmod 700 "$LAB_DIR"
export KUBECONFIG="$LAB_DIR/kind.kubeconfig"
KIND=dciab-ci NS=cluster-lab01 NAME=lab01
WK="$LAB_DIR/$NAME.kubeconfig"
MGMT_LIMIT="${MILESTONE_LIMIT:-1800}"   # seconds any one wait may take
now() { date +%s; }
note() { echo "[$(( $(now) - ${T0:-$(now)} ))s] $*"; }
wk() { kubectl --kubeconfig "$WK" "$@"; }
vip() { lab_get clusters.lab01.apiVIP; }

kind_up() {
  sudo env KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name "$KIND" --kubeconfig "$KUBECONFIG"
  sudo chown "$(id -u):$(id -g)" "$KUBECONFIG"
  DCIAB_E2E_MANAGEMENT_ONLY=1 "$ROOT/hack/e2e/run.sh"
}

values() {
  local tpl_name="${1:?template name}" tpl pub
  export PVE_NODE; PVE_NODE="$(lab_get proxmox.node)"; pve_init
  tpl="$(pve_vmid_by_name "$tpl_name")"; [[ -n "$tpl" ]] || lab_die "no template called $tpl_name"
  ssh-keygen -q -t ed25519 -N '' -f "$LAB_DIR/node-key" <<<y >/dev/null 2>&1 || true
  pub="$(cat "$LAB_DIR/node-key.pub")"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 -subj "/CN=DCiaB lab CA" \
    -keyout "$LAB_DIR/ca.key" -out "$LAB_DIR/ca.crt" 2>/dev/null
  TPL="$tpl" PUB="$pub" CA="$LAB_DIR/ca.crt" python3 - <<'PY' > "$LAB_DIR/$NAME-values.yaml"
import os, yaml
n = yaml.safe_load(open(os.environ.get("LAB_NETWORK", "handoff/lab/network.yaml")))
c, net, px = n["clusters"]["lab01"], n["network"], n["proxmox"]
machine = {"cpu": 2, "memoryGiB": 4, "diskGiB": 25}
print(yaml.safe_dump({
    "name": "lab01", "class": "proxmox", "kubernetesVersion": "v1.36.6",
    "apiVIP": c["apiVIP"], "podCIDR": c["podCIDR"], "serviceCIDR": c["serviceCIDR"],
    "nodes": {"registryMirror": "mirror.invalid", "trustBundle": open(os.environ["CA"]).read(),
              "ntpServers": net["ntp"], "sshAuthorizedKeys": [os.environ["PUB"]], "dnsServers": net["dns"]},
    "network": {"nodeIPRange": c["nodeIPRange"], "prefix": net["prefix"], "gateway": net["gateway"]},
    "controlPlane": {"replicas": 3, "machine": machine},
    "workerPools": [{"name": "general", "replicas": 1, "machine": machine}],
    "proxmox": {"allowedNodes": [px["node"]], "templateNode": px["node"], "templateID": int(os.environ["TPL"]),
                "storage": px["storage"], "bridge": px["bridge"]},
}, sort_keys=False))
PY
  helm lint "$ROOT/charts/capi-cluster" -f "$LAB_DIR/$NAME-values.yaml"
  kubectl create namespace "$NS"
  : "${CAPMOX_TOKEN_ID:?}" "${CAPMOX_TOKEN_SECRET:?}"
  kubectl -n "$NS" create secret generic "$NAME-proxmox-credentials" \
    --from-literal=url="${PVE_URL%/api2/json}" --from-literal=token="$CAPMOX_TOKEN_ID" --from-literal=secret="$CAPMOX_TOKEN_SECRET" >/dev/null
  echo "values for $NAME: template $tpl_name = VMID $tpl"
}

build() {
  T0="$(now)"
  helm upgrade --install "$NAME" "$ROOT/charts/capi-cluster" -n "$NS" -f "$LAB_DIR/$NAME-values.yaml"
  local seen=""
  have() { case "$seen" in *"|$1|"*) ;; *) seen="$seen|$1|"; note "milestone: $1" ;; esac; }
  while (( $(now) - T0 < MGMT_LIMIT )); do
    kubectl -n "$NS" get cluster "$NAME" -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}' 2>/dev/null \
      | grep -q '^TopologyReconciled=True' && have topology
    [[ "$(kubectl -n "$NS" get proxmoxmachines --no-headers 2>/dev/null | wc -l)" -ge 1 ]] && have proxmoxmachine
    curl -sk -m 3 "https://$(vip):6443/readyz" >/dev/null 2>&1 && have api-vip
    local total running
    total="$(kubectl -n "$NS" get machines --no-headers 2>/dev/null | wc -l)"
    running="$(kubectl -n "$NS" get machines --no-headers 2>/dev/null | grep -c Running || true)"
    if (( total >= 4 && running == total )); then have all-running; break; fi
    sleep 15
  done
  [[ "$seen" == *"|all-running|"* ]] || { diagnostics; lab_die "not all Machines Running after ${MGMT_LIMIT}s (milestones:$seen)"; }
  kubectl -n "$NS" get secret "$NAME-kubeconfig" -o jsonpath='{.data.value}' | base64 -d > "$WK"; chmod 600 "$WK"
}

diagnostics() {
  kubectl -n "$NS" get cluster,kcp,md,machines,proxmoxmachines -o wide || true
  kubectl -n capmox-system logs deploy/capmox-controller-manager --tail=100 || true
  kubectl -n capi-system logs deploy/capi-controller-manager --tail=100 || true
}

cilium() { "$ROOT/addons/cilium/install.sh" "$LAB_DIR/$NAME-values.yaml" "$WK"; wk wait node --all --for=condition=Ready --timeout=10m; }

checks() {
  wk -n kube-system get ds kube-proxy 2>&1 | grep -q NotFound
  wk -n kube-system exec ds/cilium -c cilium-agent -- cilium-dbg status | grep -E 'KubeProxyReplacement: +True'
  python3 - "$(wk get nodes -o json)" "$(lab_get clusters.lab01.podCIDR)" <<'PY'
import ipaddress, json, sys
nodes, pod = json.loads(sys.argv[1]), ipaddress.ip_network(sys.argv[2])
for n in nodes["items"]:
    assert all(ipaddress.ip_network(c).subnet_of(pod) for c in n["spec"]["podCIDRs"]), n["metadata"]["name"]
PY
  wk create deploy web --image=registry.k8s.io/e2e-test-images/agnhost:2.53 --replicas=2 -- /agnhost netexec --http-port=8080
  wk expose deploy web --port=80 --target-port=8080
  wk rollout status deploy/web --timeout=5m
  wk run cl --restart=Never --image=registry.k8s.io/e2e-test-images/agnhost:2.53 --command -- /agnhost connect web.default.svc.cluster.local:80 --timeout=15s
  wk wait --for=jsonpath='{.status.phase}'=Succeeded pod/cl --timeout=3m
  wk delete deploy/web svc/web pod/cl --wait=false
  [[ "$(wk -n kube-system get lease plndr-cp-lock -o jsonpath='{.spec.holderIdentity}')" == *lab01* ]]   # exactly one kube-vip leader
  echo "checks ok"
}

# replace KIND: delete a Machine and wait until the cluster is whole again.
replace_one() {
  local sel="$1" machine start
  machine="$(kubectl -n "$NS" get machines --no-headers | awk -v s="$sel" '$1 ~ s {print $1; exit}')"
  [[ -n "$machine" ]] || lab_die "no Machine matching $sel"
  start="$(now)"; kubectl -n "$NS" delete machine "$machine" --wait=false
  until ! kubectl -n "$NS" get machine "$machine" >/dev/null 2>&1 \
     && [[ "$(kubectl -n "$NS" get machines --no-headers | grep -c Running)" == 4 ]]; do
    (( $(now) - start < MGMT_LIMIT )) || { diagnostics; lab_die "replacing $machine took over ${MGMT_LIMIT}s"; }
    sleep 10
  done
  echo "replaced $machine in $(( $(now) - start ))s"
}

replace() {
  local fails=0 pid
  ( while :; do wk get --raw /readyz --request-timeout=4s >/dev/null 2>&1 || echo F >> "$LAB_DIR/readyz.fail"; sleep 5; done ) &
  pid=$!
  : > "$LAB_DIR/readyz.fail"
  replace_one "-general-"
  replace_one "^$NAME-[a-z0-9]{5}-[a-z0-9]{5}$"
  kill "$pid" 2>/dev/null || true
  fails="$(wc -l < "$LAB_DIR/readyz.fail")"
  echo "/readyz failures through the VIP while replacing: $fails"
  wk get nodes
}

down() {
  helm -n "$NS" uninstall "$NAME" --wait --timeout 10m 2>&1 || true
  export PVE_NODE; PVE_NODE="$(lab_get proxmox.node)"
  pve_init 2>/dev/null && for _ in $(seq 1 60); do
    pve GET "/cluster/resources?type=vm" | jq -e 'any(.[]; (.name // "") | startswith("lab01-"))' >/dev/null || break
    sleep 10
  done
  sudo env KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name "$KIND" || true
}

case "${1:-}" in
  kind) kind_up ;; values) shift; values "$@" ;; build) build ;; cilium) cilium ;; checks) checks ;;
  replace) replace ;; down) down ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
