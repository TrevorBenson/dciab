# ADR 0001: Day 0 for bare servers — hypervisors over Redfish, and pivot services on the bootstrap host

- Status: accepted
- Date: 2026-11-20

## Context

`seed` and `mgmt` assume a working hypervisor platform: a Proxmox VE API for CAPMOX, or vCenter for CAPV. Bare metal exists in DCiaB only as Metal3 workload clusters, and Ironic runs in MGMT. So a site that has servers, power and network, but no hypervisor, has no way to start.

Installing the first hypervisors needs services that such a site may not have either:

- **DNS**, forward and reverse. vCenter's installer expects its name to resolve both ways, hosts are added to vCenter by name, and certificates carry names.
- **NTP**. ESXi, vCenter, certificates, kubeadm and etcd all fail with skewed clocks.
- **HTTP**, to serve installer ISOs and per-host answer files.

These are needed only until the platform's own services (PowerDNS and the rest, deployed by Helm on MGMT and INFRA) exist.

## Decisions

### 1. Two optional steps before `seed`

| Step | Command | Result |
| --- | --- | --- |
| 0 | `dciab validate` | Config, files and checksums valid |
| 1 | `dciab services` | DNS, NTP and HTTP on the bootstrap host |
| 2 | `dciab hypervisors` | Hypervisors installed on the servers and configured for CAPMOX or CAPV |
| 3+ | `seed`, `mgmt`, ... | As before |

Steps 1 and 2 run only when the config has `spec.hypervisors` (so `spec.bootstrap.ntp` requires it too). A site whose hypervisors already exist leaves it out and starts at `seed`.

### 2. Hypervisors are installed over Redfish virtual media

`dciab hypervisors` attaches the installer ISO to each server through its BMC, sets a one-time boot from it, and powers the server on. The installer fetches a per-host answer file (Proxmox VE) or kickstart (ESXi) from the bootstrap host.

- It needs no DHCP or TFTP, can't collide with a site's DHCP, and doesn't need the bootstrap host on the servers' network segment.
- It is the vendor-supported automated install for Proxmox VE (an ISO prepared with `proxmox-auto-install-assistant`); PXE for Proxmox VE is a community workaround.
- The schema already describes BMCs this way for Metal3 hosts, so `spec.hypervisors.hosts` reuses the same `bmc`, `bootMACAddress` and `rootDeviceHints` definitions.

`spec.hypervisors.method` has a single value, `virtualmedia`. PXE is a roadmap item, added only if a site needs it.

### 3. BMCs are reached by routing through `bootstrap.interface`

The bootstrap host keeps one interface. It reaches the BMC networks through that interface's default gateway. Site prerequisites:

- the node network's gateway routes to the BMC networks, and back
- firewalls allow HTTPS from the bootstrap host to each BMC (Redfish), and HTTP from each BMC to the bootstrap host (virtual media fetching the ISO)
- BMCs already have their addresses and credentials

A direct out-of-band interface on the bootstrap host (which would also allow handing BMCs their addresses over DHCP) is a roadmap item, added only if a site needs it.

### 4. Installers are pinned in `bundle.lock`, whether or not the archive ships them

`bundle.lock` defines the versions DCiaB was tested with; it doesn't imply the archive carries the file. Each installer entry says which:

- **Proxmox VE**: pinned by version, URL and sha256; shipped in the archive.
- **ESXi**: pinned by build and sha256, but not shipped, because it can't be redistributed. The site supplies the ISO (`spec.hypervisors.installerFile`) and `validate` checks its sha256 against the pin. A different build is a `bundle.lock` change, tested like any other.

### 5. Day 0 services are small containers, not the production stack

They exist only until the platform's services take over, so they're chosen for simplicity, not to match the Helm-deployed production services:

| Need | Image | Notes |
| --- | --- | --- |
| DNS | CoreDNS (official image) | Forward and reverse zone files (`file` plugin, with SOA and NS) are rendered from one record list, so they can't disagree. (Amended at implementation: first planned as the `hosts` plugin.) `forward` covers `network.upstreamResolvers`; an empty list means no forwarding |
| NTP | chrony, in a DCiaB image (`images/day0-chrony/`) on Rocky Linux 10 minimal | Runs with `-x`: it serves time without setting the host clock, so it needs no `SYS_TIME` capability. One chronyd per DNS service IP, because chrony binds to one IPv4 address only |
| HTTP | Caddy (official image) | Static file server with correct ranged reads, which BMCs use for virtual media; its access log shows each BMC fetching its ISO |

Images are pinned by digest in `bundle.lock` (`day0.images`) and shipped in the archive. `dciab services` renders their configuration from `dciab.yaml` and runs them as one pod with host networking, from a generated Kubernetes-style manifest (`podman kube play --replace`); teardown is `podman kube down`. They run rootful, because they bind ports 53 and 123 on the site network.

### 6. The pivot services answer on the production service addresses

`spec.network.dnsServiceIPs` already reserves two node-network addresses for the DNS service, and those addresses move from the bootstrap host to MGMT when MGMT's DNS takes over.

- `dciab services` adds both addresses to `bootstrap.interface`, and CoreDNS answers only on them.
- Hypervisors, and later vCenter, are configured with those addresses from their first boot, so nothing installed on Day 0 needs reconfiguring after the handoff.
- When the site has no NTP of its own, `spec.bootstrap.ntp` makes the bootstrap host serve NTP on the same addresses, and `network.ntpServers` must list exactly them. With no upstream, `serveLocalClock: true` must be set explicitly, because the whole site then takes its time from the bootstrap host's clock.
- The production platform must therefore also serve NTP on the DNS service addresses after the handoff, whenever `spec.bootstrap.ntp` was used.

HTTP listens on `bootstrap.address`: only installers and BMCs use it, and it goes away with the bootstrap host.

## Consequences

- New optional config: `spec.hypervisors` and `spec.bootstrap.ntp`. `validate` checks them: the hypervisor type matches `platform.type`; host addresses sit in the node network without overlapping anything; MACs and BMCs are unique across hypervisor and Metal3 hosts; Proxmox hosts appear in `platform.proxmox.nodes`; the ESXi ISO matches its pin; NTP servers match the DNS service addresses when the bootstrap host serves NTP.
- `bundle.lock` gains `hypervisors` and `day0` sections; the image digest check covers `day0.images`.
- `dciab services` must handle the workstation: ports already in use (Rocky's own `chronyd` holds UDP 123, so the pod binds only to the service addresses), runtime-only firewalld rules opened for the step and closed at teardown, and a readiness check that queries DNS (both directions), NTP and ranged HTTP from the network.
- Before a server is installed, the hypervisor step checks or sets over Redfish: UEFI boot mode, Secure Boot per `spec.hypervisors.bootMode`, virtualization extensions, and that a disk matches `rootDeviceHints`.

## Amendment 1 (2026-11-22): answer files inside per-host ISOs; root by SSH key only

**Context.** The Proxmox VE automated installer reads its answer file from the ISO, from a labelled partition, or over HTTP. HTTP mode POSTs system information to a URL and relies on DHCP while the installer runs, and Day 0 has no DHCP; Caddy's file server also answers only GET. A partition needs a second virtual medium, which not every BMC can attach alongside a CD.

**Decisions.**
- `dciab hypervisors` builds one ISO per host, `proxmox-auto-install-assistant prepare-iso --fetch-from iso` with that host's answer file inside. It runs the assistant from a pinned container image (`images/pve-assistant`, a Debian package), with no network. Caddy serves `/host-isos/<host>.iso` only to that host's BMC address; the answer files themselves are no longer served.
- Root on each hypervisor logs in with the `identity.initialAdmins` SSH keys only. The installer requires a root password, so each host gets a random 256-bit one, hashed at once and never stored: nobody knows it. `spec.hypervisors.rootPassword` is removed.
- The keyboard, country and time zone the installer requires are config keys (`spec.hypervisors.installer`), not built-in values.
- DCiaB and CAPMOX use the Proxmox VE API with a dedicated API token with exactly the privileges [PROXMOX.md](../PROXMOX.md) lists; nothing DCiaB runs needs SSH to a hypervisor.

**Consequences.** One base-ISO-sized file per host on the work disk, rebuilt only when that host's answer file or the base ISO changes. A host's ISO (with its root password hash, of a password nobody knows) is readable from its BMC's address over plain HTTP, as ISOs already were. With no root password, console and web UI password login are gone until Authentik SSO is a Proxmox VE realm (ADR 0004); break-glass is SSH with an admin key. Post-install configuration (the API token, bridge, storage) can't log in with a password, so it needs its own credential path: a first-boot hook in the answer file is the candidate.

## Not decided here (roadmap)

- **vCenter deployment** on the first ESXi host, and creating its datacenter, cluster and networks. Until then, ESXi sites need an existing vCenter.
- **Proxmox VE clustering** and shared storage across the installed hosts.
- **PXE** as a second install method, for servers without licensed virtual media.
- **A direct out-of-band interface** on the bootstrap host, and DHCP for BMCs.
- **Separate NTP service addresses**, if sharing the DNS service addresses proves limiting.
