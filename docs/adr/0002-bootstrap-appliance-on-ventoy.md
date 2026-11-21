# ADR 0002: The bootstrap host runs Rocky Linux 10: an appliance installed from the DCiaB USB (Ventoy), or a host you provide

- Status: accepted. Revised 2026-11-21: the appliance is an installer ISO, not a Ventoy vDisk (§3).
- Date: 2026-11-20

## Context

The bootstrap host runs Day 0 (ADR 0001) and then `seed` through `finalize`: for hours it must own addresses on the node network (`bootstrap.address` and the DNS service IPs), accept connections from BMCs, installers and new VMs, run rootful containers with host networking and kind, and hand addresses off to MGMT. Everything it runs is x86_64, like the servers.

Operators arrive with Windows, macOS on Apple silicon, or Linux laptops. Podman Desktop runs containers in a Linux VM whose traffic is NATed through the laptop, with ports forwarded by gvproxy. It can't own addresses on the site network or receive broadcasts, every inbound path needs per-OS forwarding and firewall rules, and Apple silicon emulates x86_64.

## Decisions

### 1. The bootstrap host is Rocky Linux 10 (x86_64), one of two ways

| | Server zero (default) | A host you provide |
| --- | --- | --- |
| What | One x86_64 server in the rack, installed from the DCiaB USB with the DCiaB bootstrap appliance (`images/bootstrap-bootc/`, Rocky Linux 10 image mode) | A Rocky Linux 10 x86_64 machine, such as a Linux laptop, with a wired NIC on the node network |
| `spec.bootstrap.workDisk` | `disk:` the server's disk the appliance installs onto (wiped) | `path:` a directory on it |
| The operator's laptop | Any OS or architecture: SSH and a browser | It is the bootstrap host |

Both run the same steps (`validate`, `media`, `services`, `hypervisors`, `seed`, ...) on the same OS, so the workflow and its tests don't depend on which one a site uses. The appliance pins podman, kind, helm and kubectl; a host you provide must have them at the `bundle.lock` versions.

Not supported as the bootstrap host: other Linux distributions, Podman Desktop / podman machine on macOS or Windows, and Apple silicon. Any of them can still edit the config and run `dciab validate`, which needs no network.

### 2. The USB is a Ventoy stick

- Partition 1 is exFAT, labelled `DCIAB`: the config, secrets, payload and installers, editable from any laptop. RHEL 9 and later read exFAT natively.
- The appliance is an installer ISO on it (`dciab-bootstrap-<version>.iso`), with an embedded kickstart. The operator picks it from the Ventoy menu by hand; there's no default entry or timeout, because it wipes a disk.
- The same stick boots other installers by hand (Proxmox VE, ESXi, Rocky, vendor firmware tools) when a BMC or Redfish can't be used.
- The pipeline builds the whole stick as one image (`images/usb/`), with Ventoy's release pinned in `bundle.lock`, so every operator writes it with an ordinary imaging tool. Ventoy's own installer doesn't run on macOS.

### 3. The appliance is installed, not run from the stick

The first design booted the appliance from the stick as a Ventoy Linux vDisk, so server zero's disks stayed untouched. Two things changed that:
- With Secure Boot on, Ventoy didn't boot the vDisk: it returned to its menu, while the Proxmox VE and Rocky ISOs booted fine once Ventoy's key was enrolled. vtoyboot's tracker has open reports that vDisk boot doesn't support Secure Boot.
- `dciab media` (§5) writes to server zero's disk anyway.

So the appliance ISO boots through Rocky's own signed shim, like any Rocky installer, and installs unattended onto `spec.bootstrap.workDisk.disk`:
- The kickstart's `%pre` script reads `dciab.yaml` from the stick and picks the one disk matching `workDisk.disk` (rootDeviceHints keys, matched as Metal3 does). The stick is never a candidate. No match, or more than one, stops the install with a message listing the disks.
- The script needs PyYAML, which the installer environment lacks, so the stick carries a pure-Python PyYAML (pinned in `bundle.lock`) and the helper (`lib/dciab_bootstrap_host/install.py`) under `.dciab/installer/`.
- On every boot, the installed appliance copies `dciab.yaml` and `secrets/` from the stick, when it's plugged in, to `/var/lib/dciab/site`, then configures its network and admin logins from that copy. After the first boot and `dciab media`, the stick can be removed and used elsewhere, for example to install a host by hand while the bootstrap runs.

### 4. Secure Boot

Ventoy boots under Secure Boot through a Microsoft-signed shim, but each machine needs Ventoy's key enrolled once in MokManager, at the console. Every Ventoy stick shares that key, and it doesn't restrict what else can boot, so some security teams won't accept it on production servers. Past Ventoy's menu, the appliance installer and the hypervisor installers boot through their own vendors' signed boot loaders.

- **Default:** enroll the key (or disable Secure Boot) only on server zero and on hosts installed by hand from the stick, for the duration of the bootstrap. When a host is (re)installed as a hypervisor, `dciab` removes the enrollment and enables Secure Boot over Redfish according to `spec.hypervisors.bootMode`.
- **Strict sites:** the pipeline also builds a plain stick without Ventoy: the appliance ISO written as the stick's boot image, plus the `DCIAB` data partition. It loses the multi-installer menu, nothing else.

### 5. `dciab media`: copy the payload to the work disk

The stick holds the payload; the bootstrap's heavy I/O happens on local storage. The first step on the bootstrap host copies the payload there, and everything after runs from it:

- `dciab media` copies the bundle (installer ISOs, node disk images, OCI archives) from the stick to `spec.bootstrap.workDisk`, and puts the heavy state there: container storage, kind and the seed registry.
- On server zero, that's the disk the appliance was installed on. On a host you provide, it's the directory `workDisk.path`.
- It copies files directly. It doesn't build ISO images or write another stick.
- Re-running it is safe: a lock stops two runs at once; each file is copied to a temporary name, checked against its sha256, then renamed into place; files already present and correct are skipped.
- Hosts whose BMC can't be used are installed by hand from the stick's `isos/` menu, once the stick is free after `dciab media`, or later over PXE from server zero (ADR 0001's roadmap).

### 6. Config

- `spec.bootstrap.interface` accepts a NIC name or a quoted MAC, because server zero's NIC names aren't known before it boots.
- `spec.bootstrap.managementVLAN` (`tagged` or `untagged`) says whether the bootstrap NIC tags `network.nodes.vlan`. The appliance requires it.
- `spec.bootstrap.workDisk` is `disk:` (rootDeviceHints) for the appliance, or `path:` (an absolute directory) for a host you provide.
- The appliance's logins come from `identity.initialAdmins` (SSH keys, no passwords).

## Consequences

- Recipes: `images/bootstrap-bootc/` (the appliance and its kickstart) and `images/usb/` (the stick). Both are under evaluation until task 0005 reports.
- `bundle.lock` has `bootstrapAppliance`: Ventoy, PyYAML for the installer, and the checksums of the tools baked into the appliance.
- dciab's step state lives under `/var` (the appliance's `/usr` is read-only), and its heavy state on the work disk.
- Server zero's work disk is wiped by the appliance install, and again when server zero is reinstalled as a hypervisor.
- Day 0 images can be preloaded into the appliance image later, so `dciab services` starts without loading archives.

## Not decided here (roadmap)

- `dciab media` implementation. It comes before `dciab services`, which runs from the work disk.
- Reclaiming server zero as the last hypervisor at `finalize`.
- The plain (non-Ventoy) stick build.
