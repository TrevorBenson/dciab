#!/usr/bin/env bash
# dciab seed and dciab mgmt on Proxmox (task 0007's steps), one subcommand per step:
#
#   hack/lab/mgmt.sh payload                    build the archive payload (cached by bundle.lock)
#   hack/lab/mgmt.sh config TEMPLATE_NAME       write dciab.yaml for the lab and validate it
#   hack/lab/mgmt.sh seed                       dciab seed (root), then again: nothing may change
#   hack/lab/mgmt.sh mgmt                       dciab mgmt (root), each step line timed
#   hack/lab/mgmt.sh checks                     MGMT manages itself, keeps no volumes and trusts the site's
#                                               OIDC issuer with the platform roles; replace a control plane Machine
#   hack/lab/mgmt.sh rerun                      dciab mgmt again: no step changes, no Machine rolls
#   hack/lab/mgmt.sh down                       delete MGMT, the bootstrap cluster and what seed added
#
# Environment: PVE_* (hack/lab/pve.sh), CAPMOX_TOKEN_ID/CAPMOX_TOKEN_SECRET (MGMT's token),
# handoff/lab/network.yaml. UNTAINT_CONTROL_PLANE=1 removes the control plane NoSchedule taint
# on MGMT after Cilium, the workaround for issue #112 (off by default, so CI shows the failure).
# Root work runs through sudo with state in /root/.dciab/ci-state, which the lab runner
# removes with its other per-job state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/hack/lab/pve.sh"
LAB_DIR="${LAB_DIR:-$HOME/.dciab-ci}"; mkdir -p "$LAB_DIR"; chmod 700 "$LAB_DIR"
CACHE=/var/cache/dciab-lab
STATE=/root/.dciab/ci-state
KEY="$(sha256sum "$ROOT/bundle.lock" | cut -c1-16)"
PAYLOAD="$CACHE/payload-$KEY"
CONFIG="$LAB_DIR/dciab.yaml"
# The token reaches root's dciab through the environment (sudo --preserve-env), never on a command line.
export DCIAB_PVE_TOKEN="${CAPMOX_TOKEN_SECRET:-}"
rootenv() { sudo --preserve-env=DCIAB_PVE_TOKEN env "PATH=$PAYLOAD/bin:$PATH" "$@"; }
sk() { sudo kubectl --kubeconfig "$STATE/$1.kubeconfig" "${@:2}"; }

payload() {
  [[ ! -e "$PAYLOAD/.complete" ]] || { echo "payload $KEY cached ($(du -sh "$PAYLOAD" | cut -f1))"; return 0; }
  sudo install -d -o "$(id -u)" -g "$(id -g)" "$CACHE"; rm -rf "${PAYLOAD:?}"
  cd "$ROOT"
  local crds; crds="$(python3 hack/fetch_crds.py)"
  addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > "$LAB_DIR/cilium.yaml"
  python3 build/inventory.py --manifests "$crds" --extra "$LAB_DIR/cilium.yaml" --format yaml > "$LAB_DIR/images.yaml"
  python3 build/archive.py binaries --out "$PAYLOAD"
  python3 build/archive.py images --inventory "$LAB_DIR/images.yaml" --out "$PAYLOAD" --oras "$PAYLOAD/bin/oras"
  python3 build/archive.py charts --out "$PAYLOAD"
  python3 build/archive.py providers --out "$PAYLOAD" --manifests "$crds"
  python3 build/archive.py sums --out "$PAYLOAD"
  touch "$PAYLOAD/.complete"; du -sh "$PAYLOAD"
}

config() {
  local tpl_name="${1:?template name}" tpl addr iface
  export PVE_NODE; PVE_NODE="$(lab_get proxmox.node)"; pve_init
  tpl="$(pve_vmid_by_name "$tpl_name")"; [[ -n "$tpl" ]] || lab_die "no template called $tpl_name"
  addr="$(ip -4 route get "$(lab_get network.gateway)" | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  iface="$(ip -4 route get "$(lab_get network.gateway)" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)"
  "$PAYLOAD/bin/age-keygen" -o "$LAB_DIR/age1.key" 2>/dev/null; "$PAYLOAD/bin/age-keygen" -o "$LAB_DIR/age2.key" 2>/dev/null
  ssh-keygen -q -t ed25519 -N '' -f "$LAB_DIR/admin-key" <<<y >/dev/null 2>&1 || true
  TPL="$tpl" ADDR="$addr" IFACE="$iface" R1="$("$PAYLOAD/bin/age-keygen" -y "$LAB_DIR/age1.key")" \
  R2="$("$PAYLOAD/bin/age-keygen" -y "$LAB_DIR/age2.key")" PUB="$(cat "$LAB_DIR/admin-key.pub")" \
  python3 - <<'PY' > "$CONFIG"
import os, yaml
root = os.environ.get("ROOT_DIR", ".")
n = yaml.safe_load(open(os.environ.get("LAB_NETWORK", "handoff/lab/network.yaml")))
c = yaml.safe_load(open("examples/proxmox.yaml"))
s, px, net, site, m = c["spec"], c["spec"]["platform"]["proxmox"], n["network"], n["site"], n["clusters"]["mgmt"]
px.update(url=os.environ["PVE_URL"] + "", tlsFingerprint=os.environ["PVE_TLS_FINGERPRINT"], nodes=[n["proxmox"]["node"]],
          templateNode=n["proxmox"]["node"], templateID=int(os.environ["TPL"]), storage=n["proxmox"]["storage"],
          bridge=n["proxmox"]["bridge"])
px.pop("vlanTag", None); px["credentials"]["tokenID"] = os.environ["CAPMOX_TOKEN_ID"]
s["bootstrap"].update(interface=os.environ["IFACE"], address=os.environ["ADDR"], managementVLAN="untagged",
                      recipients=[os.environ["R1"], os.environ["R2"]])
s["network"].update(domain=site["domain"], upstreamResolvers=net["dns"], dnsServiceIPs=site["dnsServiceIPs"])
s["network"].pop("isolated", None); s["network"].pop("storage", None)
s["time"] = {"servedBy": "site", "servers": net["ntp"]}
node = n["proxmox"]["node"]
s["failureDomains"] = {"domains": [{"name": node, "proxmox": {"node": node}}],
                       "notHighlyAvailable": {"reason": "the lab has one Proxmox node"}}
s["network"]["nodes"] = {"cidr": ".".join(net["gateway"].split(".")[:3]) + ".0/%d" % net["prefix"], "gateway": net["gateway"], "vlan": 1}
s["clusters"]["mgmt"].update(apiVIP=m["apiVIP"], nodeIPRange=m["nodeIPRange"], lbPool=site["lbPool"],
                             podCIDR=m["podCIDR"], serviceCIDR=m["serviceCIDR"],
                             controlPlane={"replicas": 3, "cpu": 2, "memoryGiB": 4, "diskGiB": 25}, workerPools=[])
i = s["clusters"]["infra"]
i.update(apiVIP=site["infra"]["apiVIP"], nodeIPRange=site["infra"]["nodeIPRange"], lbPool=site["infra"]["lbPool"],
         podCIDR=site["infra"]["podCIDR"], serviceCIDR=site["infra"]["serviceCIDR"])
i.pop("storageIPRanges", None)
s["pki"] = {"mode": "generate-root", "commonName": "DCiaB lab root CA"}
s["identity"]["initialAdmins"] = [{"username": "ci", "email": "ci@lab.invalid", "sshPublicKey": os.environ["PUB"]}]
# INFRA's block storage (INFRA isn't built here): Longhorn needs no token of its own. MGMT
# gets none (ADR 0008, decision 4a); checks verifies that.
s["storage"] = {"block": {"provider": "longhorn", "longhorn": {"replicaCount": 2}}}
# MGMT's API trusts a site OIDC issuer (provider external, ADR 0008 decision 3), so checks can
# verify the AuthenticationConfiguration on a real API server and the role bindings. Nothing
# answers at the issuer's URL: the API server must load the file and keep serving anyway.
s["identity"].update(provider="external", consumers=["kubernetes"], roles={"viewer": "dciab-lab-viewers"},
                     external={"issuer": "https://sso.lab.invalid/realms/dciab",
                               "claims": {"username": "preferred_username", "groups": "groups"},
                               "clients": {"kubernetes": {"clientID": "dciab-lab"}}})
s["backup"]["targets"]["site"] = {"url": "nfs://%s:/exports/longhorn" % net["gateway"]}
print(yaml.safe_dump(c, sort_keys=False))
PY
  DCIAB_PVE_TOKEN="$CAPMOX_TOKEN_SECRET" "$ROOT/dciab" validate "$CONFIG"
}

seed() {
  sudo mkdir -p "$STATE"
  # shellcheck disable=SC2024
  rootenv "$ROOT/dciab" seed "$CONFIG" --payload "$PAYLOAD" --state "$STATE" 2>&1 | tee "$LAB_DIR/seed1.log"
  echo "== second run: nothing may change"
  # shellcheck disable=SC2024
  rootenv "$ROOT/dciab" seed "$CONFIG" --payload "$PAYLOAD" --state "$STATE" 2>&1 | tee "$LAB_DIR/seed2.log"
  if ! { grep -q "pki: unchanged" "$LAB_DIR/seed2.log" && grep -q "0 loaded now" "$LAB_DIR/seed2.log"; }; then
    lab_die "the second seed changed something"
  fi
}

# Prefixes each line of a command's output with the seconds since it started.
timed() { local t0; t0="$(date +%s)"; "$@" 2>&1 | while IFS= read -r l; do printf '[%4ss] %s\n' "$(( $(date +%s) - t0 ))" "$l"; done; return "${PIPESTATUS[0]}"; }

mgmt_run() { rootenv "$ROOT/dciab" mgmt "$CONFIG" --state "$STATE" --payload "$PAYLOAD" "$@"; }

mgmt() {
  if [[ "${UNTAINT_CONTROL_PLANE:-}" == 1 ]]; then
    timed mgmt_run --until cilium
    sk mgmt taint nodes --all node-role.kubernetes.io/control-plane- || true
  fi
  timed mgmt_run | tee "$LAB_DIR/mgmt1.log"
}

checks() {
  sk mgmt -n cluster-mgmt wait cluster/mgmt --for=condition=Available --timeout=5m
  sk mgmt get clusterclasses -A
  [[ -z "$(sk bootstrap get clusters -A --no-headers)" ]] || lab_die "the bootstrap cluster still holds a Cluster: the move did not happen"
  sudo env "KUBECONFIG=$STATE/mgmt.kubeconfig" helm list -A | tee "$LAB_DIR/helm.txt"
  grep -q '^classes' "$LAB_DIR/helm.txt" && grep -q '^mgmt' "$LAB_DIR/helm.txt"
  no_volumes
  oidc
  local machine start
  machine="$(sk mgmt -n cluster-mgmt get machines --no-headers | awk 'NR==1 {print $1}')"
  [[ -n "$machine" ]] || lab_die "MGMT holds no Machines"
  start="$(date +%s)"; sk mgmt -n cluster-mgmt delete machine "$machine" --wait=false
  until [[ "$(sk mgmt -n cluster-mgmt get kcp -o jsonpath='{.items[0].status.readyReplicas}')" == 3 ]] \
     && ! sk mgmt -n cluster-mgmt get machine "$machine" >/dev/null 2>&1; do
    (( $(date +%s) - start < 1500 )) || lab_die "MGMT did not replace $machine in 25 minutes"; sleep 10
  done
  echo "MGMT replaced $machine in $(( $(date +%s) - start ))s"
}

# MGMT keeps no volumes (ADR 0008, decision 4a): the config sets no overrides.mgmt, so the
# storage step installs no driver and MGMT has no StorageClass, PVC or PV.
no_volumes() {
  local log="$LAB_DIR/mgmt1.log" left
  grep -q "block storage: none" "$log" || lab_die "dciab mgmt's storage step didn't report 'block storage: none'"
  left="$(sk mgmt get storageclass,pvc,pv -A --no-headers 2>/dev/null || true)"
  [[ -z "$left" ]] || lab_die "MGMT has storage objects, but the config gives it none: $left"
  for ns in longhorn-system csi-proxmox vmware-system-csi; do
    ! sk mgmt get namespace "$ns" >/dev/null 2>&1 || lab_die "MGMT has namespace $ns: a storage driver was installed"
  done
  echo "MGMT keeps no volumes: no StorageClass, PVC, PV or storage driver"
}

# MGMT's API server reads the AuthenticationConfiguration with the site issuer, keeps serving
# though nothing answers at the issuer's URL, and dciab mgmt's identity step bound the platform
# roles to the issuer's prefixed groups (site:<group>).
oidc() {
  local auth=/etc/kubernetes/authentication/config.yaml issuer=https://sso.lab.invalid/realms/dciab admins
  admins="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["spec"]["identity"]["adminGroup"])' "$CONFIG")"
  grep -q "identity: MGMT's API trusts:" "$LAB_DIR/mgmt1.log" || lab_die "dciab mgmt's identity step didn't report MGMT's issuer"
  sk mgmt -n kube-system get pods -l component=kube-apiserver -o json \
    | jq -e --arg a "--authentication-config=$auth" '.items | length == 3 and all(.[]; .spec.containers[0].command | index($a))' >/dev/null \
    || lab_die "not every kube-apiserver on MGMT runs with --authentication-config=$auth"
  sk mgmt -n cluster-mgmt get kcp -o json \
    | jq -e --arg p "$auth" --arg i "$issuer" '.items[0].spec.kubeadmConfigSpec.files[] | select(.path == $p) | .content | contains($i) and contains("site:")' >/dev/null \
    || lab_die "MGMT's control plane has no AuthenticationConfiguration with $issuer and the site: prefix"
  sk mgmt get --raw /readyz >/dev/null || lab_die "MGMT's API isn't ready with the site issuer configured"
  sk mgmt get clusterrolebinding dciab-admin-site dciab-viewer-site -o wide
  [[ "$(sk mgmt auth can-i '*' '*' --as=site:ci --as-group="site:$admins" || true)" == yes ]] \
    || lab_die "site:$admins isn't cluster-admin on MGMT"
  [[ "$(sk mgmt auth can-i list pods -A --as=site:v --as-group=site:dciab-lab-viewers || true)" == yes ]] \
    || lab_die "site:dciab-lab-viewers can't view MGMT"
  [[ "$(sk mgmt auth can-i delete pods -A --as=site:v --as-group=site:dciab-lab-viewers || true)" == no ]] \
    || lab_die "site:dciab-lab-viewers can change MGMT"
  [[ "$(sk mgmt auth can-i list pods -A --as=dciab-lab-viewers --as-group=dciab-lab-viewers || true)" == no ]] \
    || lab_die "an unprefixed group got the viewer role"
  echo "MGMT trusts $issuer; the platform roles are bound to its site: groups"
}

rerun() {
  local before after
  before="$(sk mgmt -n cluster-mgmt get machines -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
  timed mgmt_run | tee "$LAB_DIR/mgmt2.log"
  after="$(sk mgmt -n cluster-mgmt get machines -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | sort)"
  [[ "$before" == "$after" ]] || lab_die "a Machine rolled during the re-run"
  grep -q "already moved" "$LAB_DIR/mgmt2.log" || lab_die "the re-run did not report 'already moved'"
}

down() {
  export PVE_NODE; PVE_NODE="$(lab_get proxmox.node)"
  if sk mgmt -n cluster-mgmt get cluster mgmt >/dev/null 2>&1; then sk mgmt -n cluster-mgmt delete cluster mgmt --wait=false
  elif sk bootstrap -n cluster-mgmt get cluster mgmt >/dev/null 2>&1; then sk bootstrap -n cluster-mgmt delete cluster mgmt --wait=false; fi
  pve_init 2>/dev/null && for _ in $(seq 1 90); do
    pve GET "/cluster/resources?type=vm" | jq -e 'any(.[]; (.name // "") | startswith("mgmt-"))' >/dev/null || break
    sleep 10
  done
  sudo env KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name dciab-bootstrap || true
  for n in $(sudo podman ps -a --format '{{.Names}}' | grep -E '^dciab' || true); do sudo podman rm -f "$n"; done
  for p in $(sudo podman pod ps --format '{{.Name}}' | grep -E '^dciab' || true); do sudo podman pod rm -f "$p"; done
  for a in $(lab_get site.dnsServiceIPs | jq -r '.[]'); do sudo ip addr del "$a/$(lab_get network.prefix)" dev "$(ip -4 route get "$(lab_get network.gateway)" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)" 2>/dev/null || true; done
  sudo firewall-cmd --reload || true
}

case "${1:-}" in
  payload) payload ;; config) shift; config "$@" ;; seed) seed ;; mgmt) mgmt ;; checks) checks ;;
  rerun) rerun ;; down) down ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
