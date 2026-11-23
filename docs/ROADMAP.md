# Roadmap

What each phase builds, against the component list in [COMPONENTS.md](COMPONENTS.md). Each phase ends in a gate that must pass on both a vSphere environment and a Proxmox VE environment.

| Phase | Scope | Gate |
| --- | --- | --- |
| 0. Spikes | The three ClusterClasses (vSphere, Proxmox, Metal3); Kea behind a LoadBalancer with DHCP relay; DNS IP handoff from the bootstrap host to MGMT; CAPMOX `v1alpha2` templates | A cluster from each class builds, survives node replacement and a Kubernetes upgrade, and moves from one class revision to the next |
| 1. Skeleton | Config schema and `validate`; archive build pipeline (`bundle.lock`, SBOMs, signing); `seed`; `mgmt` | MGMT builds and pivots on both platforms; re-running any step changes nothing |
| 2. MGMT services | Metal3 (BMO + Ironic), PowerDNS, Kea, DNS handoff; Proxmox VE clustering and shared storage across hosts installed by `dciab hypervisors` ([ADR 0010](adr/0010-failure-domains.md)) | A bare-metal host provisions over Redfish virtual media; DNS answers from MGMT with the bootstrap host unplugged; on Proxmox VE, MGMT and INFRA VMs run on more than one clustered host |
| 3. INFRA and platform | CloudNativePG, OpenBao, Forgejo, Argo CD and adoption of script-installed releases; OpenTofu stacks; Authentik SSO; metrics and logs | `platform` from a clean MGMT ends with every application Synced and the nightly drift job clean |
| 4. Hosts and clusters | OpenVox enrollment and profiles; standalone VMs; `finalize`; workload clusters from each class | A full run on nested virtualization, with step timings recorded |
| 5. Hardening and handover | Failure injection, an air-gapped rehearsal, README and troubleshooting guide | An engineer who didn't build it completes a run from the README alone |

## Phase 1 status

- [x] `schema/dciab.schema.json`
- [x] `dciab validate`: schema and offline semantic checks
- [x] `dciab validate --live`: live checks (hypervisor, BMC Redfish, NTP, upstream DNS, ARP probes for unused addresses); not yet run against real infrastructure
- [x] `bundle.lock` with pinned versions (digests come with the build pipeline)
- [x] `capi-clusterclasses` chart: `r1` classes (draft until the first release; see [VERSIONING.md](VERSIONING.md)) for vSphere, Proxmox and Metal3
- [x] `capi-cluster` chart
- [x] ClusterClass end-to-end test: CAPI and all providers on kind, one Cluster per class, generated objects verified (no infrastructure)
- [x] Node template recipe: image-builder with a Rocky 10 Proxmox target (`images/node/`)
- [x] First Proxmox template built and verified
- [ ] Node templates for vSphere and Metal3: the bootc recipe builds them (`vmdk` with VMware Tools; a `metal` variant with drivers and firmware); not yet tested on either platform
- [ ] Node image for the Kubernetes upgrade gate: the recipe builds it (`build.sh --upgrade-from`); not yet used in a test
- [x] Cilium install for any cluster (`addons/cilium`), with kube-proxy replaced
- [ ] Real-cluster spike on Proxmox from the template
- [ ] Archive build pipeline (`build/README.md` in the source repository; not shipped in the archive): image inventory, image pull, charts, providers, binaries, checksums, SBOMs, packaging and signing (`dciab verify-archive`) done; node images next
- [x] `dciab cluster-values`: capi-cluster values for MGMT, INFRA and workload clusters, generated from the site config
- [x] `dciab status`: read-only report of the Day 0 pod, the platform CA and registry certificate, the seed registry, the bootstrap cluster and where MGMT's Cluster lives
- [x] `dciab seed`: platform CA (generated with its key age-encrypted, or imported), read-only Zot loaded from the payload, kind pulling through it, providers and ClusterClasses from the payload, DNS for `registry.<domain>` ([SEED.md](SEED.md))
- [ ] `dciab mgmt`: builds MGMT, installs Cilium and the providers, and moves Cluster API into MGMT; picks up `dciab seed`'s kubeconfig and trust bundle and runs offline from the payload (CI runs seed then mgmt up to the Cluster; not yet proven on real infrastructure). Longhorn installer built (`addons/longhorn`, the `longhorn` step, chart and images from the payload). On vSphere, the cloud provider (CPI) is installed before Cilium (`addons/vsphere-cpi`, the `cpi` step). Still to come: MGMT's other services (Metal3, PowerDNS, Kea) and the DNS handoff

## Day 0 for bare servers

Decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md).

- [x] Config: `spec.hypervisors` and `spec.bootstrap.ntp`, with `validate` checks
- [x] `bundle.lock`: Proxmox VE installer pinned; ESXi entry (not shipped); Day 0 service images
- [x] Day 0 image digests pinned (CoreDNS, Caddy, chrony base)
- [x] DCiaB's chrony image published by CI and pinned (`day0.images.chrony`), so the archive ships it
- [ ] ESXi build tested and pinned
- [x] `dciab services`: CoreDNS, chrony and Caddy as one pod on the DNS service IPs
- [x] `dciab hypervisors` for Proxmox VE, first part: answer files inside per-host installer ISOs (served only to each host's BMC), root password from the config or generated and sealed with age, Redfish virtual media boot, wait for the install ([docs/HYPERVISORS.md](HYPERVISORS.md); ADR 0001 amendment 1)
- [x] The CAPMOX API token's exact Proxmox VE privileges, one role per path, checked by `dciab validate --live` ([docs/PROXMOX.md](PROXMOX.md))
- [ ] `dciab hypervisors` for Proxmox VE, next: answer file delivery to the installer (prepared ISO), tagged management VLAN, Redfish preparation (boot mode, Secure Boot, disk check), post-install API token, bridge and storage
- [ ] `dciab hypervisors` for ESXi
- [x] Live checks: BMC routes, Redfish and credentials (`dciab validate --live`)

## Bootstrap appliance and USB

Decided in [ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md).

- [x] Config: `bootstrap.interface` by name or MAC; `bootstrap.managementVLAN`; `bootstrap.workDisk`
- [x] Recipes under evaluation: `images/bootstrap-bootc/` (appliance installer ISO), `images/usb/` (Ventoy stick)
- [ ] Spike: unattended install of server zero from the stick with Secure Boot on, first boot from `dciab.yaml`, a short end-to-end check, installers from the same stick
- [ ] Day 0 images preloaded in the appliance
- [x] `dciab media`: copy the USB payload to the work disk, and run the bootstrap's heavy state from it ([MEDIA.md](MEDIA.md))
- [ ] Plain (non-Ventoy) stick for strict Secure Boot sites

## Platform interoperability

Proposed for phases 3 and 4. Two pieces exist: `dciab cluster-values` already gives workload clusters Authentik's OIDC issuer, and the ClusterClasses already write a Kubernetes API audit log for log collection to pick up.

- [ADR 0004](adr/0004-identity-and-sso.md): identity and SSO (Authentik, SPIFFE/SPIRE)
- [ADR 0005](adr/0005-secrets.md): secrets (OpenBao, External Secrets Operator, platform PKI)
- [ADR 0006](adr/0006-iac-path.md): the IaC path (OpenTofu, Terramate, Terratest, Terrakube with OPA)
- [ADR 0007](adr/0007-detection-to-response.md): detection to response (Tetragon, Suricata, OpenSearch, SOAR)

## Backlog

- **vCenter deployment** on the first ESXi host (datacenter, cluster, networks). Until then, ESXi sites need an existing vCenter.
- **Reclaim server zero** as the last hypervisor at `finalize`.
- **Only if a site needs them:** PXE as a second install method; a direct out-of-band interface on the bootstrap host, with DHCP for BMCs; separate NTP service addresses.

- **Node image recipe: image mode (bootc) with CRI-O is the choice.** It builds and boots on Proxmox VE with SELinux enforcing. The image-builder recipe stays until a cluster passes on the bootc template; then it's removed, along with the classes' containerd mirror files.
- **CRI-O instead of containerd.** It comes with the bootc recipe, which installs CRI-O. The `r1` classes already write CRI-O's mirror files next to containerd's, and kubeadm finds the only runtime socket on its own. What's left is a cluster passing on a bootc template (the real-cluster spike on Proxmox above), then removing the containerd files and the image-builder recipe.
