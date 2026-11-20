# Roadmap

Each phase ends in a gate that must pass on both a vSphere lab and a Proxmox lab.

| Phase | Scope | Gate |
| --- | --- | --- |
| 0. Spikes | The three ClusterClasses (vSphere, Proxmox, Metal3); Kea behind a LoadBalancer with DHCP relay; DNS IP handoff from the bootstrap host to MGMT; CAPMOX `v1alpha2` templates | A cluster from each class builds, survives node replacement and a Kubernetes upgrade, and moves from one class revision to the next |
| 1. Skeleton | Config schema and `validate`; archive build pipeline (`bundle.lock`, SBOMs, signing); `seed`; `mgmt` | MGMT builds and pivots on both labs; re-running any step changes nothing |
| 2. MGMT services | Metal3 (BMO + Ironic), PowerDNS, Kea, DNS handoff | A bare-metal host provisions over Redfish virtual media; DNS answers from MGMT with the bootstrap host unplugged |
| 3. INFRA and platform | CloudNativePG, OpenBao, Forgejo, Argo CD and adoption of script-installed releases; OpenTofu stacks; Authentik SSO; metrics and logs | `platform` from a clean MGMT ends with every application Synced and the nightly drift job clean |
| 4. Hosts and clusters | OpenVox enrollment and profiles; standalone VMs; `finalize`; workload clusters from each class | A full run in the nested lab, with step timings recorded |
| 5. Hardening and handover | Failure injection, an air-gapped rehearsal, README and troubleshooting guide | An engineer who didn't build it completes a run from the README alone |

## Phase 1 status

- [x] `schema/dciab.schema.json`
- [x] `dciab validate`: schema and offline semantic checks
- [ ] `dciab validate`: live checks (hypervisor, BMC Redfish, NTP, upstream DNS, ARP probes for unused addresses)
- [x] `bundle.lock` with pinned versions (digests come with the build pipeline)
- [x] `capi-clusterclasses` chart: `r1` classes (draft until the first release; see [VERSIONING.md](VERSIONING.md)) for vSphere, Proxmox and Metal3
- [x] `capi-cluster` chart
- [x] ClusterClass end-to-end test: CAPI and all providers on kind, one Cluster per class, generated objects verified (no infrastructure)
- [x] Node template recipe: image-builder with a Rocky 10 Proxmox target (`images/node/`)
- [x] First Proxmox template built and verified
- [ ] Node templates for vSphere and Metal3
- [x] Cilium install for any cluster (`addons/cilium`), with kube-proxy replaced
- [ ] Real-cluster spike on Proxmox from the template
- [ ] Archive build pipeline
- [ ] `dciab seed`
- [ ] `dciab mgmt`

## Day 0 for bare servers

Decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md).

- [x] Config: `spec.hypervisors` and `spec.bootstrap.ntp`, with `validate` checks
- [x] `bundle.lock`: Proxmox VE installer pinned; ESXi entry (not shipped); Day 0 service images
- [x] Day 0 image digests pinned (CoreDNS, Caddy, chrony base)
- [ ] ESXi build tested and pinned
- [ ] `dciab services`: CoreDNS, chrony and Caddy as one pod on the DNS service IPs
- [ ] `dciab hypervisors` for Proxmox VE: Redfish preparation, virtual media install, API token, bridge and storage
- [ ] `dciab hypervisors` for ESXi
- [ ] Live checks: BMC routes, Redfish and credentials

## Backlog

- **vCenter deployment** on the first ESXi host (datacenter, cluster, networks). Until then, ESXi sites need an existing vCenter.
- **Proxmox VE clustering** and shared storage across hosts installed by `dciab hypervisors`.
- **Only if a site needs them:** PXE as a second install method; a direct out-of-band interface on the bootstrap host, with DHCP for BMCs; separate NTP service addresses.

- **Node image recipe: image-builder (containerd) or image mode (bootc, CRI-O).** The bootc recipe in `images/node-bootc/` is under evaluation against the image-builder template.
- **CRI-O instead of containerd** (preferred). Node templates use containerd today: image-builder installs it, and it's the only runtime image-builder supports. The switch needs:
  - a CRI-O role in the node image recipe
  - the classes' registry mirror files moved from containerd `hosts.toml` to `/etc/containers/registries.conf.d`
  - `criSocket` set in the kubeadm configuration

  Ideally it lands before `r1` is released; after that it needs a new class revision.
