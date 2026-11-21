# ADR 0002: The bootstrap host is an appliance booted from the DCiaB USB (Ventoy)

- Status: accepted, pending the vDisk spike (handoff task 0005). If that spike fails, §3's fallback applies.
- Date: 2026-11-20

## Context

The bootstrap host runs Day 0 (ADR 0001) and then `seed` through `finalize`: for hours it must own addresses on the node network (`bootstrap.address` and the DNS service IPs), accept connections from BMCs, installers and new VMs, run rootful containers with host networking and kind, and hand addresses off to MGMT. Everything it runs is x86_64, like the servers.

Operators arrive with Windows, macOS on Apple silicon, or Linux laptops. Podman Desktop runs containers in a Linux VM whose traffic is NATed through the laptop, with ports forwarded by gvproxy. It can't own addresses on the site network or receive broadcasts, every inbound path needs per-OS forwarding and firewall rules, and Apple silicon emulates x86_64. Requiring Rocky Linux 10 on the laptop works, but means reimaging corporate laptops and keeping one awake on the node network for hours.

## Decisions

### 1. Server zero: the bootstrap host is an appliance in the rack

The DCiaB USB boots a Rocky Linux 10 bootc appliance (`images/bootstrap-bootc/`) on one x86_64 server on the node network. It boots from the stick; `dciab media` (§5) then copies the payload to a local disk, where the bootstrap's heavy I/O happens. The operator's laptop is only a terminal (SSH) and a browser, on any OS or architecture. Every site therefore runs the bootstrap on the same OS, kernel, podman and tools, which is the environment CI and the lab test.

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

If the spike shows vDisk boot unreliable (it's a Ventoy feature, not something the bootc project tests), the stick stays a Ventoy stick, but the appliance is installed from an installer ISO onto a disk in server zero.

### 4. Secure Boot

Ventoy boots under Secure Boot through a Microsoft-signed shim, but each machine needs Ventoy's key enrolled once in MokManager, at the console. Every Ventoy stick shares that key, and it doesn't restrict what else can boot, so some security teams won't accept it on production servers.

- **Default:** enroll the key (or disable Secure Boot) only on server zero and on hosts installed by hand from the stick, for the duration of the bootstrap. When a host is (re)installed as a hypervisor, `dciab` removes the enrollment and enables Secure Boot over Redfish according to `spec.hypervisors.bootMode`.
- **Strict sites:** the pipeline also builds a plain stick without Ventoy (appliance raw disk plus the `DCIAB` data partition). It loses the multi-installer menu, nothing else.

### 5. `dciab media`: copy the payload to the bootstrap host's local disk

A USB stick shouldn't carry hours of registry, container and kind I/O, and exFAT has no journal. So the stick only boots the appliance and holds the payload. The first step on the bootstrap host copies the payload to local storage, and everything after runs from there:

- `dciab media` copies the bundle (installer ISOs, node disk images, OCI archives) from `/var/mnt/dciab` to the work area named in `spec.bootstrap.workDisk`. It also moves the appliance's heavy state there: container storage, kind and the seed registry.
- `spec.bootstrap.workDisk` is either:
  - a local disk, selected like `rootDeviceHints`, that DCiaB wipes and formats: server zero's spare disk, or a laptop VM's second disk. This is the only choice for the appliance booted from the stick, because its own filesystem is the stick.
  - an existing directory, only where dciab runs on an installed OS (a Rocky Linux 10 host, or the appliance installed on a disk per §3).
- It copies the payload files directly onto that disk. It doesn't build ISO images or write another stick. The stick stays in server zero because the appliance's OS runs from it, but carries no heavy I/O after the copy.
- Re-running it is safe: a lock stops two runs at once; each file is copied to a temporary name, checked against its sha256, then renamed into place; files already present and correct are skipped.
- Hosts whose BMC can't be used are installed by hand from the Ventoy stick's `isos/` menu, before server zero starts the bootstrap or after it finishes, or later over PXE from server zero (ADR 0001's roadmap).

### 6. Config

- `spec.bootstrap.interface` accepts a NIC name or a quoted MAC, because server zero's NIC names aren't known before it boots.
- `spec.bootstrap.managementVLAN` (`tagged` or `untagged`) says whether the bootstrap NIC tags `network.nodes.vlan`. The appliance requires it.
- The appliance's logins come from `identity.initialAdmins` (SSH keys, no passwords).

## Consequences

- New recipes: `images/bootstrap-bootc/` (the appliance) and `images/usb/` (the stick). Both are under evaluation until task 0005 reports.
- `bundle.lock` gains `bootstrapAppliance` (vtoyboot, Ventoy).
- dciab's future step state must live under `/var` (the appliance's `/usr` is read-only), and its heavy state on the work disk.
- The vDisk only needs to hold the OS and dciab, so it can stay small; there's no long-running I/O on the stick to test for.
- Server zero's work disk is wiped when DCiaB formats it, and again when server zero is reinstalled as a hypervisor.
- Day 0 images can be preloaded into the appliance image later, so `dciab services` starts without loading archives.

## Not decided here (roadmap)

- `dciab media` and `spec.bootstrap.workDisk` (schema, validation, implementation). They come before `dciab services`, which runs from the work disk.
- Reclaiming server zero as the last hypervisor at `finalize`.
- The plain (non-Ventoy) stick build.
