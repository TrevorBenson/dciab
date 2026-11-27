#!/usr/bin/env bash
# Publish a node image as a Proxmox template, and test a clone of it. Through the API only.
#
#   hack/lab/template.sh dciab DISK.qcow2           the product's step: lay DISK out as an archive payload
#                                                   (build/archive.py nodes --local-disk), write a lab site
#                                                   config, run `dciab templates` twice (the second must keep it)
#   hack/lab/template.sh publish DISK.qcow2 NAME    by hand: upload, create the VM, make it a template
#   hack/lab/template.sh test NAME                  clone, grow, boot, check, reboot, delete
#   hack/lab/template.sh delete NAME                delete a template (by name)
#
# Environment: PVE_* (hack/lab/pve.sh: the publisher token, which `dciab` gets as the site's
# templateImport token), CAPMOX_TOKEN_ID (the site's CAPMOX token ID; dciab templates never
# uses it), handoff/lab/network.yaml. `publish` needs the storage `import_storage` to allow
# the content type "import" (see handoff/LAB.md); `dciab templates` adds it to `local` itself.
# `dciab` prints the template's name and `publish` its VMID on the last line; `test` fails
# on any check.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/pve.sh"
pve_init
PVE_NODE="$(lab_get proxmox.node)"; export PVE_NODE
STORAGE="$(lab_get proxmox.storage)"; IMPORT="$(lab_get proxmox.import_storage)"; BRIDGE="$(lab_get proxmox.bridge)"
Q="/nodes/$PVE_NODE/qemu"

drop_import() { pve DELETE "/nodes/$PVE_NODE/storage/$IMPORT/content/$IMPORT:import/$IMPORT_FILE" >/dev/null 2>&1 || true; }
drop_test_vm() {
  [[ -n "${TEST_VM:-}" ]] || return 0
  pve POST "$Q/$TEST_VM/status/stop" >/dev/null 2>&1 || true; sleep 3
  pve DELETE "$Q/$TEST_VM?purge=1&destroy-unreferenced-disks=1" >/dev/null 2>&1 || true
  rm -rf "${WORK:-/nonexistent}"
}

task() { pve_wait "$(tr -d '"' <<<"$1")" "${2:-1800}"; }

publish() {
  local disk="${1:?disk}" name="${2:?name}" vmid
  [[ -f "$disk" ]] || lab_die "no such file: $disk"
  [[ -z "$(pve_vmid_by_name "$name")" ]] || lab_die "a VM or template called $name already exists"
  pve GET "/storage/$IMPORT" | jq -e '.content | split(",") | index("import")' >/dev/null \
    || lab_die "storage '$IMPORT' does not allow the content type 'import'. One-time fix, as a Proxmox admin: pvesm set $IMPORT --content <current list>,import"
  vmid="$(pve_free_vmid "$(lab_get vmid.template.first)" "$(lab_get vmid.template.last)")"
  IMPORT_FILE="$name.qcow2"; file="$IMPORT_FILE"
  lab_log "upload $disk as $IMPORT:import/$file"
  task "$(pve POST "/nodes/$PVE_NODE/storage/$IMPORT/upload" -F content=import -F "filename=@$disk;filename=$file")"
  trap drop_import EXIT
  lab_log "create VM $vmid ($name), import the disk into $STORAGE"
  task "$(pve POST "$Q" \
    --data-urlencode "vmid=$vmid" --data-urlencode "name=$name" --data-urlencode "ostype=l26" \
    --data-urlencode "cpu=x86-64-v3" --data-urlencode "cores=2" --data-urlencode "memory=4096" \
    --data-urlencode "bios=seabios" --data-urlencode "scsihw=virtio-scsi-single" \
    --data-urlencode "agent=enabled=1" --data-urlencode "net0=virtio,bridge=$BRIDGE" \
    --data-urlencode "serial0=socket" --data-urlencode "vga=serial0" --data-urlencode "tags=dciab-ci" \
    --data-urlencode "scsi0=$STORAGE:0,import-from=$IMPORT:import/$file,discard=on,ssd=1" \
    --data-urlencode "boot=order=scsi0")" 3600
  # No cloud-init drive: CAPMOX attaches its own, and a second NoCloud source wins over it.
  pve GET "$Q/$vmid/config" | jq -e 'has("ide2") | not' >/dev/null || lab_die "template $vmid has an ide2 drive"
  lab_log "convert $vmid to a template"
  pve POST "$Q/$vmid/template" >/dev/null
  echo "$vmid"
}

# The product's step on the lab: dciab templates from a payload holding DISK, as a site whose
# Proxmox VE already runs (templateImport: the publisher token). Run twice: the second must keep it.
dciab_publish() {
  local disk="${1:?disk}" work vmid cfg name addr iface root
  [[ -f "$disk" ]] || lab_die "no such file: $disk"
  root="$(cd "$HERE/../.." && pwd)"
  work="$(mktemp -d)"; mkdir -p "$work/site"
  python3 "$root/build/archive.py" nodes --out "$work/payload" --local-disk "proxmox=$disk" >&2
  vmid="$(pve_free_vmid "$(lab_get vmid.template.first)" "$(lab_get vmid.template.last)")"
  addr="$(ip -4 route get "$(lab_get network.gateway)" | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  iface="$(ip -4 route get "$(lab_get network.gateway)" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)"
  ssh-keygen -q -t ed25519 -N '' -f "$work/admin-key" >/dev/null
  cfg="$work/site/dciab.yaml"
  TPL="$vmid" ADDR="$addr" IFACE="$iface" PUB="$(cat "$work/admin-key.pub")" \
    LAB_NETWORK="$root/handoff/lab/network.yaml" EXAMPLE="$root/examples/proxmox.yaml" python3 - <<'PY' > "$cfg"
import os, yaml
n = yaml.safe_load(open(os.environ["LAB_NETWORK"]))
c = yaml.safe_load(open(os.environ["EXAMPLE"]))
s, px, net, site, m = c["spec"], c["spec"]["platform"]["proxmox"], n["network"], n["site"], n["clusters"]["mgmt"]
node = n["proxmox"]["node"]
px.update(url=os.environ["PVE_URL"], tlsFingerprint=os.environ["PVE_TLS_FINGERPRINT"], nodes=[node], templateNode=node,
          templateID=int(os.environ["TPL"]), storage=n["proxmox"]["storage"], bridge=n["proxmox"]["bridge"])
px.pop("vlanTag", None)
px["credentials"] = {"tokenID": os.environ["CAPMOX_TOKEN_ID"], "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}
px["templateImport"] = {"credentials": {"tokenID": os.environ["PVE_TOKEN_ID"],
                                        "tokenSecret": {"env": "DCIAB_TEMPLATE_TOKEN"}}}
s["bootstrap"].update(interface=os.environ["IFACE"], address=os.environ["ADDR"], managementVLAN="untagged")
s["network"].update(domain=site["domain"], upstreamResolvers=net["dns"], dnsServiceIPs=site["dnsServiceIPs"])
s["network"].pop("isolated", None); s["network"].pop("storage", None)
s["network"]["nodes"] = {"cidr": ".".join(net["gateway"].split(".")[:3]) + ".0/%d" % net["prefix"],
                         "gateway": net["gateway"], "vlan": 1}
s["time"] = {"servedBy": "site", "servers": net["ntp"]}
s["failureDomains"] = {"domains": [{"name": node, "proxmox": {"node": node}}],
                       "notHighlyAvailable": {"reason": "the lab has one Proxmox node"}}
s["clusters"]["mgmt"].update(apiVIP=m["apiVIP"], nodeIPRange=m["nodeIPRange"], lbPool=site["lbPool"],
                             podCIDR=m["podCIDR"], serviceCIDR=m["serviceCIDR"])
i = s["clusters"]["infra"]
i.update(apiVIP=site["infra"]["apiVIP"], nodeIPRange=site["infra"]["nodeIPRange"], lbPool=site["infra"]["lbPool"],
         podCIDR=site["infra"]["podCIDR"], serviceCIDR=site["infra"]["serviceCIDR"])
i.pop("storageIPRanges", None)
s["pki"] = {"mode": "generate-root", "commonName": "DCiaB lab root CA"}
s["identity"]["initialAdmins"] = [{"username": "ci", "email": "ci@lab.invalid", "sshPublicKey": os.environ["PUB"]}]
s["storage"] = {"block": {"provider": "longhorn", "longhorn": {"replicaCount": 2}}}
s["backup"]["targets"]["site"] = {"url": "nfs://%s:/exports/longhorn" % net["gateway"]}
print(yaml.safe_dump(c, sort_keys=False))
PY
  # The tokens reach root's dciab through the environment, never on a command line.
  export DCIAB_PVE_TOKEN="${CAPMOX_TOKEN_SECRET:-}" DCIAB_TEMPLATE_TOKEN="$PVE_TOKEN_SECRET"
  # shellcheck disable=SC2054  # sudo's --preserve-env takes a comma-separated list
  local run=(sudo --preserve-env=DCIAB_PVE_TOKEN,DCIAB_TEMPLATE_TOKEN "$root/dciab" templates "$cfg"
             --state "$work/state" --payload "$work/payload")
  # On failure: remove what this run may have left at its VMID (the lab range is CI's own).
  failed() { sudo rm -rf "$work"; pve GET "$Q/$vmid/config" >/dev/null 2>&1 \
    && { task "$(pve DELETE "$Q/$vmid?purge=1&destroy-unreferenced-disks=1")" || true; }; lab_die "$1"; }
  "${run[@]}" >&2 || failed "dciab templates failed"
  lab_log "dciab templates again: the template must be kept" >&2
  local again
  again="$("${run[@]}" 2>&1)" || { echo "$again" >&2; failed "the second dciab templates failed"; }
  echo "$again" >&2
  grep -q "template $vmid on $PVE_NODE: kept" <<<"$again" || failed "the second dciab templates didn't keep template $vmid"
  name="$(python3 -c 'import sys, yaml; print("dciab-node-" + yaml.safe_load(open(sys.argv[1]))["disks"][0]["sha256"][:12])' \
    "$work/payload/nodes/nodes.yaml")"
  sudo rm -rf "$work"
  [[ "$(pve_vmid_by_name "$name")" == "$vmid" ]] || lab_die "no template called $name at VMID $vmid"
  echo "$name"
}

# SSH to the clone with a throwaway key; the key never leaves the runner.
vm_ssh() { ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
  -o ConnectTimeout=5 -o BatchMode=yes "rocky@$IP" "$@"; }
wait_ssh() { for _ in $(seq 1 60); do vm_ssh true 2>/dev/null && return 0; sleep 5; done; lab_die "no SSH on $IP after 5 minutes"; }

# agent_exec VMID CMD...: the guest agent's guest-exec, as CAPMOX uses it; prints out-data.
agent_exec() {
  local vmid="$1" args=() pid st; shift
  for a in "$@"; do args+=(-d "command=$a"); done
  pid="$(pve POST "$Q/$vmid/agent/exec" "${args[@]}" | jq -r .pid)"
  for _ in $(seq 1 30); do
    st="$(pve GET "$Q/$vmid/agent/exec-status?pid=$pid")"
    [[ "$(jq -r .exited <<<"$st")" == 1 ]] && { jq -r '."out-data" // ""' <<<"$st"; return 0; }
    sleep 2
  done
  lab_die "guest-exec did not finish: $*"
}

test_template() {
  local name="${1:?template name}" tpl vmid pub
  tpl="$(pve_vmid_by_name "$name")"; [[ -n "$tpl" ]] || lab_die "no template called $name"
  vmid="$(pve_free_vmid "$(lab_get vmid.test.first)" "$(lab_get vmid.test.last)")"
  IP="$(lab_get testVM.ip)"
  WORK="$(mktemp -d)"; KEY="$WORK/key"; ssh-keygen -q -t ed25519 -N '' -f "$KEY"
  pub="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(open(sys.argv[1]).read().strip(), safe=""))' "$KEY.pub")"
  TEST_VM="$vmid"; trap drop_test_vm EXIT
  lab_log "clone $tpl to $vmid, 25G, with a cloud-init drive on the clone"
  task "$(pve POST "$Q/$tpl/clone" --data-urlencode "newid=$vmid" --data-urlencode "name=lab-test-$vmid" \
    --data-urlencode "full=1" --data-urlencode "storage=$STORAGE")"
  pve PUT "$Q/$vmid/resize" --data-urlencode "disk=scsi0" --data-urlencode "size=25G" >/dev/null
  pve PUT "$Q/$vmid/config" --data-urlencode "ide2=$STORAGE:cloudinit" \
    --data-urlencode "ipconfig0=ip=$IP/$(lab_get network.prefix),gw=$(lab_get network.gateway)" \
    --data-urlencode "nameserver=$(lab_get network.dns | jq -r '.[0]')" --data-urlencode "ciuser=rocky" \
    --data-urlencode "sshkeys=$pub" --data-urlencode "tags=dciab-ci" >/dev/null
  task "$(pve POST "$Q/$vmid/status/start")"
  wait_ssh; sleep 20   # let cloud-init finish
  lab_log "checks on the first boot"
  # The checks run on the VM, so nothing expands here.
  # shellcheck disable=SC2016
  vm_ssh 'set -e
    systemctl is-active dciab-growroot
    [[ "$(getenforce)" == Enforcing ]]
    df -BG --output=size /sysroot | tail -1 | awk "{ exit !(\$1+0 >= 23) }"   # 25G disk
    cloud-init status --long | head -3
    sudo semodule -l | grep -x dciab-qemu-ga
    systemctl is-active crio
    iscsiadm --version
    systemctl is-enabled iscsid
    grep -q "allow-rpcs=guest-exec,guest-exec-status" /etc/sysconfig/qemu-ga'
  lab_log "guest-exec, as CAPMOX reads cloud-init"
  agent_exec "$vmid" cloud-init status | tee /dev/stderr | grep -q "status: done"
  lab_log "reboot: growroot must find nothing to do"
  pve POST "$Q/$vmid/status/reboot" >/dev/null; sleep 15; wait_ssh
  vm_ssh 'sudo journalctl -u dciab-growroot -b --no-pager | grep "already fills the disk"'
  echo "template $name ($tpl): ok"
}

delete_template() {
  local name="${1:?name}" id
  id="$(pve_vmid_by_name "$name")"; [[ -n "$id" ]] || { echo "no template called $name"; return 0; }
  task "$(pve DELETE "$Q/$id?purge=1&destroy-unreferenced-disks=1")"
}

case "${1:-}" in
  dciab) shift; dciab_publish "$@" ;;
  publish) shift; publish "$@" ;;
  test) shift; test_template "$@" ;;
  delete) shift; delete_template "$@" ;;
  *) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
