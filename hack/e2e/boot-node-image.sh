#!/usr/bin/env bash
# Boots a node image disk (qcow2) under QEMU/KVM and checks what a Proxmox clone depends on:
# cloud-init finishes, SELinux is Enforcing with the dciab-qemu-ga module loaded, the guest
# agent, CRI-O, kubelet and iSCSI are present and enabled, and the root grows to the disk.
# It needs no hypervisor and no credentials, so it runs for pull requests on a hosted runner.
#
#   hack/e2e/boot-node-image.sh DISK.qcow2 [SIZE_GIB]     (default 25)
#
# Needs: qemu-system-x86_64, qemu-img, xorriso, ssh. KVM is used when /dev/kvm is writable.
# Exit 0 only if every check passes. The serial console log is printed on failure.
set -euo pipefail

disk_in="${1:?usage: boot-node-image.sh DISK.qcow2 [SIZE_GIB]}"
size="${2:-25}"
work="$(mktemp -d)"
port="${SSH_PORT:-2222}"
pidfile="$work/qemu.pid"
cleanup() {
  [[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null || true
  [[ "${KEEP_WORK:-}" == 1 ]] || rm -rf "$work"
}
trap cleanup EXIT

accel=tcg
[[ -w /dev/kvm ]] && accel=kvm
echo "== accelerator: $accel"

qemu-img convert -O qcow2 "$disk_in" "$work/disk.qcow2"
qemu-img resize -q "$work/disk.qcow2" "${size}G"

ssh-keygen -q -t ed25519 -N '' -C boot-test -f "$work/key"
cat > "$work/user-data" <<EOF
#cloud-config
user: rocky
ssh_authorized_keys:
  - $(cat "$work/key.pub")
EOF
printf 'instance-id: boot-test\nlocal-hostname: boot-test\n' > "$work/meta-data"
xorriso -as mkisofs -quiet -o "$work/seed.iso" -V cidata -J -r "$work/user-data" "$work/meta-data"

qemu-system-x86_64 -machine q35,accel="$accel" -cpu max -smp 2 -m 4096 \
  -drive "file=$work/disk.qcow2,if=virtio,format=qcow2" \
  -drive "file=$work/seed.iso,media=cdrom,readonly=on" \
  -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$port-:22" \
  -display none -serial "file:$work/serial.log" -daemonize -pidfile "$pidfile"

ssh_cmd=(ssh -i "$work/key" -p "$port" -o BatchMode=yes -o ConnectTimeout=5
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR rocky@127.0.0.1)
fail() {
  echo "FAIL: $*" >&2
  echo "== serial console (last 80 lines)" >&2
  tail -80 "$work/serial.log" >&2 || true
  exit 1
}

echo "== waiting for SSH (up to 10 minutes)"
for _ in $(seq 1 120); do
  "${ssh_cmd[@]}" true 2>/dev/null && break
  sleep 5
done
"${ssh_cmd[@]}" true 2>/dev/null || fail "SSH never came up"

guest() { "${ssh_cmd[@]}" "$@"; }

# cloud-init: done (0) or degraded (2, the known 'user:' notice); anything else is a failure.
rc=0; guest 'sudo cloud-init status --wait' || rc=$?
[[ $rc == 0 || $rc == 2 ]] || fail "cloud-init status exited $rc"
echo "ok: cloud-init (exit $rc)"

[[ "$(guest getenforce)" == Enforcing ]] || fail "SELinux is not Enforcing"
echo "ok: SELinux Enforcing"
guest 'sudo semodule -l | grep -qx dciab-qemu-ga' || fail "SELinux module dciab-qemu-ga is not loaded"
echo "ok: dciab-qemu-ga loaded"
for unit in crio kubelet qemu-guest-agent iscsid dciab-growroot chronyd sshd; do
  guest "systemctl is-enabled $unit >/dev/null" || fail "$unit is not enabled"
done
echo "ok: units enabled (crio kubelet qemu-guest-agent iscsid dciab-growroot chronyd sshd)"
guest 'systemctl is-active crio >/dev/null' || fail "crio is not active"
echo "ok: crio active"
guest 'kubelet --version && kubeadm version -o short && iscsiadm --version' || fail "kubelet, kubeadm or iscsiadm missing"
# The guest agent must allow guest-exec (CAPMOX runs cloud-init status through it).
guest 'grep -rhE "allow-rpcs|FILTER_RPC_ARGS" /etc/sysconfig/qemu-ga /usr/lib/systemd/system/qemu-guest-agent.service /etc/qemu-ga 2>/dev/null | grep -q guest-exec' \
  || fail "qemu-ga allow-list does not include guest-exec"
echo "ok: qemu-ga allows guest-exec"
avail="$(guest "df -BG --output=size /var | tail -1 | tr -dc 0-9")"
(( avail >= size - 3 )) || fail "/var is ${avail}G after growroot, expected about ${size}G"
echo "ok: /var grew to ${avail}G"
guest 'sudo bootc status --format humanreadable | sed -n 1,3p'
echo "== node image boots: all checks passed"
