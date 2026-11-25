#!/usr/bin/env bash
# An emulated bare server for dciab hypervisors (hack/e2e/hypervisors/README.md). Run as root on a
# throwaway machine with KVM (a GitHub-hosted runner):
#
#   setup.sh untagged|tagged WORK
#
#   switch      Linux bridge dciabsw0 with VLAN filtering and no default VLAN. The VM's port is an
#               access port on the node VLAN (untagged) or carries it tagged only (tagged).
#   bootstrap   veth dciab-boot (bootstrap.interface by MAC), an access port on the node VLAN,
#               with bootstrap.address and the node gateway. dciab services adds the DNS service IPs.
#   BMC         sushy-tools (Redfish in front of libvirt) in network namespace dciab-bmc at
#               10.30.0.21:8000 over HTTPS, routed from the bootstrap host as a real BMC network is.
#               It fetches each host ISO from bootstrap.address from its own address, like a BMC.
#   server      libvirt VM pve01: q35, UEFI (Secure Boot capable OVMF), a SATA disk with serial
#               1234570, a virtio NIC on the switch. on_reboot=destroy: run.py turns the end of a
#               boot from Cd into a boot from disk, as a BMC's one-time boot does.
#
# Writes WORK/env (values run.py reads), WORK/bmc.crt and WORK/bin/{age,age-keygen}.
set -euo pipefail

MODE="${1:?untagged or tagged}"
WORK="${2:?work directory}"
case "$MODE" in untagged|tagged) ;; *) echo "setup.sh: mode is untagged or tagged" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "setup.sh: run as root" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HERE="$ROOT/hack/e2e/hypervisors"

VLAN=110
BOOT_MAC=b8:ca:3a:00:00:05
VM=pve01
VM_MAC=52:54:00:dc:1a:01
VM_UUID=7f3e9a5c-1d2b-4c6e-9f80-dc1ab0000001
TAP=hv-pve01
BMC_IP=10.30.0.21
BMC_PORT=8000
DISK_SERIAL=1234570
IMAGES=/var/lib/libvirt/images/dciab
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd
OVMF_VARS=/usr/share/OVMF/OVMF_VARS_4M.fd
OVMF_VARS_MS=/usr/share/OVMF/OVMF_VARS_4M.ms.fd

lock() { python3 -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1]))
for k in sys.argv[2].split("/"): d = d[k]
print(d)' "$ROOT/bundle.lock" "$1"; }

mkdir -p "$WORK/bin"
[ -w /dev/kvm ] || { echo "setup.sh: no usable /dev/kvm" >&2; exit 1; }
for f in "$OVMF_CODE" "$OVMF_VARS" "$OVMF_VARS_MS"; do [ -f "$f" ] || { echo "setup.sh: missing $f (ovmf)" >&2; exit 1; }; done

# age (bundle.lock tools.age, checked against binarySHA256), for the sealed root passwords and tokens.
age_v="$(lock tools/age)"
age_sum="$(lock binarySHA256/age-linux-amd64.tar.gz)"
curl -fsSL --retry 3 -o "$WORK/age.tgz" "https://github.com/FiloSottile/age/releases/download/$age_v/age-$age_v-linux-amd64.tar.gz"
echo "$age_sum  $WORK/age.tgz" | sha256sum -c - >/dev/null
tar -xzf "$WORK/age.tgz" -C "$WORK" && install -m 755 "$WORK/age/age" "$WORK/age/age-keygen" "$WORK/bin/"

# Bridged frames must not go through iptables: Docker's FORWARD policy on the runner is DROP.
modprobe br_netfilter 2>/dev/null || true
for k in iptables ip6tables arptables; do sysctl -qw "net.bridge.bridge-nf-call-$k=0" 2>/dev/null || true; done

# The switch, and the bootstrap host's access port on it.
ip link add dciabsw0 type bridge vlan_filtering 1 vlan_default_pvid 0
ip link set dciabsw0 up
# The MAC is given at creation, so the kernel marks it as set (addr_assign_type 3). Set afterwards,
# it races systemd-udevd: MACAddressPolicy=persistent (99-default.link on Ubuntu) can still replace
# a veth's random MAC after our change, and dciab services then finds no NIC with BOOT_MAC (#232).
ip link add dciab-boot address "$BOOT_MAC" type veth peer name dciab-boot-sw
if command -v nmcli >/dev/null 2>&1; then
  nmcli device set dciab-boot managed no 2>/dev/null || true
  nmcli device set dciab-boot-sw managed no 2>/dev/null || true
fi
ip link set dciab-boot-sw master dciabsw0
bridge vlan add dev dciab-boot-sw vid "$VLAN" pvid untagged
ip link set dciab-boot-sw up
ip link set dciab-boot up
ip addr add 10.30.110.5/24 dev dciab-boot
ip addr add 10.30.110.1/24 dev dciab-boot          # the node gateway the hosts are given

# Wait until udev is done with the new links and the bootstrap veth shows BOOT_MAC and is up.
udevadm settle --timeout=30 2>/dev/null || true
boot_ready() {
  python3 - "$BOOT_MAC" <<'PY'
import json, subprocess, sys
want = sys.argv[1].lower()
links = json.loads(subprocess.run(["ip", "-j", "link", "show", "dev", "dciab-boot"],
                                  capture_output=True, text=True, check=True).stdout)
ok = bool(links) and links[0].get("address", "").lower() == want and "UP" in links[0].get("flags", [])
sys.exit(0 if ok else 1)
PY
}
for _ in $(seq 1 30); do boot_ready && break; sleep 1; done
if ! boot_ready; then
  echo "setup.sh: dciab-boot doesn't show $BOOT_MAC and UP after 30 s" >&2
  ip -br link >&2
  cat /sys/class/net/dciab-boot/addr_assign_type >&2 2>/dev/null || true
  exit 1
fi

# The BMC network: its own namespace, routed from the bootstrap host.
ip netns add dciab-bmc
ip link add dciab-bmc-host type veth peer name dciab-bmc0
ip link set dciab-bmc0 netns dciab-bmc
ip addr add 10.30.0.1/24 dev dciab-bmc-host
ip link set dciab-bmc-host up
ip -n dciab-bmc addr add "$BMC_IP/24" dev dciab-bmc0
ip -n dciab-bmc link set lo up
ip -n dciab-bmc link set dciab-bmc0 up
ip -n dciab-bmc route add default via 10.30.0.1

# The VM's switch port, set each time libvirt starts it (a new tap every start).
install -d /etc/libvirt/hooks /etc/dciab-emulator
if [ "$MODE" = untagged ]; then echo "$TAP access $VLAN"; else echo "$TAP tagged $VLAN"; fi > "/etc/dciab-emulator/$VM.port"
cat > /etc/libvirt/hooks/qemu <<'HOOK'
#!/bin/sh
# dciab emulator: put a VM's tap on the switch as its .port file says (access or tagged only).
[ "$2" = started ] || exit 0
f="/etc/dciab-emulator/$1.port"
[ -f "$f" ] || exit 0
read -r tap mode vid < "$f"
bridge vlan del dev "$tap" vid 1 2>/dev/null || true
if [ "$mode" = access ]; then
    bridge vlan add dev "$tap" vid "$vid" pvid untagged
else
    bridge vlan add dev "$tap" vid "$vid"
fi
HOOK
chmod 755 /etc/libvirt/hooks/qemu
systemctl restart libvirtd
for _ in $(seq 30); do virsh -c qemu:///system version >/dev/null 2>&1 && break; sleep 1; done

install -d "$IMAGES" "$IMAGES/sushy"
qemu-img create -q -f qcow2 "$IMAGES/$VM.qcow2" 32G
# sushy-tools uploads each inserted ISO into the libvirt pool "default".
if ! virsh -q -c qemu:///system pool-info default >/dev/null 2>&1; then
    virsh -q -c qemu:///system pool-define-as default dir --target "$IMAGES"
    virsh -q -c qemu:///system pool-start default
fi
virsh -q -c qemu:///system pool-refresh default
cat > "$WORK/$VM.xml" <<XML
<domain type='kvm'>
  <name>$VM</name>
  <uuid>$VM_UUID</uuid>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <cpu mode='host-passthrough'/>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' secure='no' type='pflash'>$OVMF_CODE</loader>
    <nvram template='$OVMF_VARS'>/var/lib/libvirt/qemu/nvram/${VM}_VARS.nosecboot.fd</nvram>
  </os>
  <features><acpi/><apic/><smm state='on'/></features>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>destroy</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$IMAGES/$VM.qcow2'/>
      <target dev='sda' bus='sata'/>
      <serial>$DISK_SERIAL</serial>
    </disk>
    <interface type='bridge'>
      <source bridge='dciabsw0'/>
      <mac address='$VM_MAC'/>
      <target dev='$TAP'/>
      <model type='virtio'/>
    </interface>
    <serial type='file'><source path='$WORK/$VM-serial.log' append='on'/></serial>
    <video><model type='vga'/></video>
    <graphics type='vnc' listen='127.0.0.1'/>
  </devices>
</domain>
XML
virsh -q -c qemu:///system define "$WORK/$VM.xml"

# sushy-tools and its dependencies, every one pinned with a hash (bundle.lock hypervisorEmulator),
# patched to keep Secure Boot across boot mode changes.
sushy_v="$(lock hypervisorEmulator/sushyTools/version)"
sushy_sum="$(lock hypervisorEmulator/sushyTools/sha256)"
reqs="$ROOT/$(lock hypervisorEmulator/requirements)"
if ! grep -q "^sushy-tools==$sushy_v " "$reqs" || ! grep -q "sha256:$sushy_sum" "$reqs"; then
    echo "setup.sh: $reqs doesn't pin sushy-tools $sushy_v with the wheel sha256 in bundle.lock" >&2
    exit 1
fi
# A venv that sees the distro's python3-libvirt; sushy-tools' own dependencies go into the venv.
python3 -m venv --system-site-packages "$WORK/sushy-venv"
py="$WORK/sushy-venv/bin/python3"
"$py" -m pip install -q --require-hashes --only-binary :all: -r "$reqs"
"$py" "$HERE/patch_sushy.py" "$("$py" -c 'import sushy_tools, os; print(os.path.dirname(sushy_tools.__file__))')/emulator/resources/systems/libvirtdriver.py"
"$py" -c 'import libvirt' || { echo "setup.sh: the venv doesn't see python3-libvirt" >&2; exit 1; }

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$BMC_IP" -addext "subjectAltName=IP:$BMC_IP" \
    -keyout /etc/dciab-emulator/bmc.key -out /etc/dciab-emulator/bmc.crt 2>/dev/null
cp /etc/dciab-emulator/bmc.crt "$WORK/bmc.crt"
cat > /etc/dciab-emulator/sushy.conf <<CONF
SUSHY_EMULATOR_LISTEN_IP = '$BMC_IP'
SUSHY_EMULATOR_LISTEN_PORT = $BMC_PORT
SUSHY_EMULATOR_SSL_CERT = '/etc/dciab-emulator/bmc.crt'
SUSHY_EMULATOR_SSL_KEY = '/etc/dciab-emulator/bmc.key'
SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'
SUSHY_EMULATOR_STATE_DIR = '$IMAGES/sushy'
SUSHY_EMULATOR_BOOT_LOADER_MAP = {'UEFI': {'x86_64': '$OVMF_CODE'}, 'Legacy': {'x86_64': None}}
SUSHY_EMULATOR_SECURE_BOOT_ENABLED_NVRAM = '$OVMF_VARS_MS'
SUSHY_EMULATOR_SECURE_BOOT_DISABLED_NVRAM = '$OVMF_VARS'
SUSHY_EMULATOR_VMEDIA_VERIFY_SSL = False
SUSHY_EMULATOR_STORAGE = {'$VM_UUID': [{'Id': '1', 'Name': 'SATA controller', 'StorageControllers': [
    {'MemberId': '0', 'Name': 'AHCI', 'SpeedGbps': 6}], 'Drives': ['1']}]}
SUSHY_EMULATOR_DRIVES = {('$VM_UUID', '1'): [{'Id': '1', 'Name': 'Boot disk', 'CapacityBytes': 34359738368,
                                               'Protocol': 'SATA'}]}
SUSHY_EMULATOR_VOLUMES = {('$VM_UUID', '1'): []}
CONF
ip netns exec dciab-bmc env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    nohup "$WORK/sushy-venv/bin/sushy-emulator" --config /etc/dciab-emulator/sushy.conf > "$WORK/sushy.log" 2>&1 &
echo $! > "$WORK/sushy.pid"
for _ in $(seq 60); do
    curl -fsk --noproxy '*' "https://$BMC_IP:$BMC_PORT/redfish/v1/Systems/$VM_UUID" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsk --noproxy '*' "https://$BMC_IP:$BMC_PORT/redfish/v1/Systems/$VM_UUID" >/dev/null \
    || { echo "setup.sh: sushy-emulator didn't answer" >&2; tail -50 "$WORK/sushy.log" >&2; exit 1; }

cat > "$WORK/env" <<ENV
MODE=$MODE
VM=$VM
VM_UUID=$VM_UUID
VM_MAC=$VM_MAC
TAP=$TAP
VLAN=$VLAN
BOOT_MAC=$BOOT_MAC
BMC_URL=https://$BMC_IP:$BMC_PORT
DISK_SERIAL=$DISK_SERIAL
OVMF_VARS_MS=$OVMF_VARS_MS
ENV
echo "setup.sh: $MODE emulator ready: $VM on dciabsw0 ($( [ "$MODE" = untagged ] && echo "access port, VLAN $VLAN" || echo "VLAN $VLAN tagged only")), BMC https://$BMC_IP:$BMC_PORT"
