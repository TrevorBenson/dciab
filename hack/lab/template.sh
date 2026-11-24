#!/usr/bin/env bash
# Publish a node image as a Proxmox template, and test a clone of it. Through the API only.
#
#   hack/lab/template.sh publish DISK.qcow2 NAME    upload, create the VM, make it a template
#   hack/lab/template.sh test NAME                  clone, grow, boot, check, reboot, delete
#   hack/lab/template.sh delete NAME                delete a template (by name)
#
# Environment: PVE_* (hack/lab/pve.sh), handoff/lab/network.yaml. The publish needs the
# storage `import_storage` to allow the content type "import" (see handoff/LAB.md).
# `publish` prints the new template's VMID on its last line; `test` fails on any check.
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
  publish) shift; publish "$@" ;;
  test) shift; test_template "$@" ;;
  delete) shift; delete_template "$@" ;;
  *) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
