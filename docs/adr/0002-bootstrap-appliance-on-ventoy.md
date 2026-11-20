# ADR 0002: The bootstrap host is an appliance booted from the DCiaB USB (Ventoy)

- Status: accepted, pending the vDisk spike (handoff task 0005). If that spike fails, §3's fallback applies.
- Date: 2026-11-20

## Context

The bootstrap host runs Day 0 (ADR 0001) and then `seed` through `finalize`: for hours it must own addresses on the node network (`bootstrap.address` and the DNS service IPs), accept connections from BMCs, installers and new VMs, run rootful containers with host networking and kind, and hand addresses off to MGMT. Everything it runs is x86_64, like the servers.

Operators arrive with Windows, macOS on Apple silicon, or Linux laptops. Podman Desktop runs containers in a Linux VM whose traffic is NATed through the laptop, with ports forwarded by gvproxy. It can't own addresses on the site network or receive broadcasts, every inbound path needs per-OS forwarding and firewall rules, and Apple silicon emulates x86_64. Requiring Rocky Linux 10 on the laptop works, but means reimaging corporate laptops and keeping one awake on the node network for hours.

## Decisions

### 1. Server zero: the bootstrap host is an appliance in the rack

The DCiaB USB boots a Rocky Linux 10 bootc appliance (`images/bootstrap-bootc/`) on one x86_64 server on the node network. It runs from the stick, so the server's own disks are untouched. The operator's laptop is only a terminal (SSH) and a browser, on any OS or architecture. Every site therefore runs the bootstrap on the same OS, kernel, podman and tools, which is the environment CI and the lab test.

Supported alternatives, running the same workflow:
- the same appliance as a VM (qcow2/vmdk/vhd) on an x86_64 laptop or host, with a wired NIC bridged to the node network
- an existing Rocky Linux 10 host, running dciab directly

Not supported as the bootstrap host: Podman Desktop / podman machine on macOS or Windows, and Apple silicon. They remain fine for editing the config and for `dciab validate`, which needs no network.

### 2. The USB is a Ventoy stick

- Partition 1 is exFAT, labelled `DCIAB`: the config, secrets, payload and installers, editable from any laptop. RHEL 9 and later read exFAT natively.
- The appliance is a fixed-size raw disk file on it (`dciab-bootstrap-<version>.raw.vtoy`), booted with Ventoy's Linux vDisk support. Its menu entry is the default.
- The same stick boots other installers by hand (Proxmox VE, ESXi, Rocky, vendor firmware tools) when a BMC or Redfish can't be used.
- The pipeline builds the whole stick as one image (`images/usb/`), with Ventoy's release pinned in `bundle.lock`, so every operator writes it with an ordinary imaging tool. Ventoy's own installer doesn't run on macOS.

Ventoy's vDisk boot needs its hook in the initramfs, re-applied whenever the initramfs changes. In image mode the initramfs is built once with the image, so the hook is installed in the image build, and the appliance is never upgraded in place: a new release replaces the file.

### 3. Fallback if vDisk boot doesn't hold up

If the spike shows vDisk boot unreliable over a multi-hour run (it's a Ventoy feature, not something the bootc project tests, and exFAT has no journal), the stick stays a Ventoy stick, but the appliance is installed from an installer ISO onto a dedicated disk in server zero. That gives up "server zero's disks stay untouched", nothing else.

### 4. Secure Boot

Ventoy boots under Secure Boot through a Microsoft-signed shim, but each machine needs Ventoy's key enrolled once in MokManager, at the console. Every Ventoy stick shares that key, and it doesn't restrict what else can boot, so some security teams won't accept it on production servers.

- **Default:** enroll the key (or disable Secure Boot) only on server zero and on hosts installed by hand from the stick, for the duration of the bootstrap. When a host is (re)installed as a hypervisor, `dciab` removes the enrollment and enables Secure Boot over Redfish according to `spec.hypervisors.bootMode`.
- **Strict sites:** the pipeline also builds a plain stick without Ventoy (appliance raw disk plus the `DCIAB` data partition). It loses the multi-installer menu, nothing else.

### 5. `dciab media`: installer media off the busy stick

Server zero runs from the stick for the whole bootstrap. Using that stick, or the appliance's I/O, for anything else would compete with the bootstrap, and the stick can't be in two servers at once. So installer media for hosts whose BMC can't be used is produced separately:

- `dciab media <dir or device>` writes the prepared installers (the same Proxmox VE ISO and ESXi kickstart ISO `dciab hypervisors` attaches over virtual media) plus a `ventoy.json` onto a second stick.
- It needs no network, so it also runs before the bootstrap, on any laptop, in a container. There it writes per-host ISOs with their answers embedded, so broken-BMC hosts can be installed before server zero starts.
- Installs from that media are the same as over Redfish: same ISOs, same answers, same addresses and DNS service IPs.

### 6. Config

- `spec.bootstrap.interface` accepts a NIC name or a quoted MAC, because server zero's NIC names aren't known before it boots.
- `spec.bootstrap.managementVLAN` (`tagged` or `untagged`) says whether the bootstrap NIC tags `network.nodes.vlan`. The appliance requires it.
- The appliance's logins come from `identity.initialAdmins` (SSH keys, no passwords).

## Consequences

- New recipes: `images/bootstrap-bootc/` (the appliance) and `images/usb/` (the stick). Both are under evaluation until task 0005 reports.
- `bundle.lock` gains `bootstrapAppliance` (vtoyboot, Ventoy).
- dciab's future step state must live under `/var` (the appliance's `/usr` is read-only).
- Day 0 images can be preloaded into the appliance image later, so `dciab services` starts without loading archives.

## Not decided here (roadmap)

- `dciab media` implementation (after `dciab services` and `dciab hypervisors`).
- Reclaiming server zero as the last hypervisor at `finalize`.
- The plain (non-Ventoy) stick build.
