# Roadmap

What each release builds, against the component list in [COMPONENTS.md](COMPONENTS.md). Release names and stages are defined in [VERSIONING.md](VERSIONING.md): release 1 follows the site's lifecycle, `r1alpha` for Day 0, `r1beta` for Day 1 and `r1` for Day 2.

**Now: `r1alpha` (Day 0).** `VERSION` is `1.0.0-alpha.1`. Some `r1beta` and `r1` work is already built; it is listed under its own stage, marked *built early*, and still has to pass that stage's gate.

The checkboxes on this page are the source of truth for what exists. GitHub issues carry a milestone per stage (`r1alpha`, `r1beta`, `r1`) and a `day0`, `day1` or `day2` label; an issue with no milestone isn't scheduled.

| Stage | Scope | Gate |
| --- | --- | --- |
| `r1alpha`: Day 0 | Config schema and `validate`; archive build pipeline; the bootstrap appliance, USB and `media`; Day 0 services on the bootstrap host (DNS, NTP, HTTP, and DHCP where a site needs it), each used only when the site doesn't provide its own; hypervisors installed over Redfish, deployed to directly; `seed` (seed registry, kind); `mgmt` (MGMT built and Cluster API moved into it); the minimal INFRA services Day 0 or the pivot need; unit tests for all of it. **Proxmox VE required, vSphere best-effort** (a parallel lane once an ESXi lab is available) | On Proxmox VE, from bare servers installed over Redfish or from an existing Proxmox VE: `services`, `hypervisors`, `seed` and `mgmt` run offline from the archive, and MGMT builds and pivots; a cluster from the Proxmox class survives node replacement; re-running any step changes nothing; unit tests and the hosted CI jobs pass |
| `r1beta`: Day 1 | Day 0's DNS, NTP, DHCP and Redfish dependencies pivot to production services, on MGMT and INFRA or provided by the site; hypervisors joined to their manager (Proxmox Datacenter Manager or vCenter); Metal3; Proxmox VE clustering and shared storage; like-for-like moves from hypervisors DCiaB installed to the site's own (vSphere to vSphere, Proxmox VE to Proxmox VE); `infra` and `platform`, with tested Argo CD app-of-apps Helm templates for INFRA and for Cluster API tenant clusters; identity and platform TLS on INFRA; OpenVox enrollment of hosts; workload clusters; `finalize`. **vSphere required** | End to end on the lab, on Proxmox VE and vSphere: DNS, NTP and DHCP answer from MGMT, INFRA or the site with the bootstrap host unplugged; a bare-metal host provisions over Redfish virtual media; on Proxmox VE, MGMT and INFRA run on more than one clustered host; `platform` from a clean MGMT ends with every application Synced and the nightly drift job clean; a workload cluster from each class; one like-for-like move per platform; a full run on nested virtualization, with step timings recorded |
| `r1`: Day 2 | Persistent storage and backups complete; upgrades, rollback and importing a new archive; Kubernetes upgrades and class revision moves; observability; reboot, cold start and failure handling; `verify`; the operator documentation | Every class survives a Kubernetes upgrade and a move to a new class revision; restore drills and a full-site cold start pass; failure injection and an air-gapped rehearsal; an engineer who didn't build it completes a run from the README alone |

**Day 0 services:** DCiaB provides each Day 0 service (DNS, NTP, DHCP, HTTP) when the site needs it, and uses the site's own out of the box when it has them.

**Hypervisor managers:** `r1alpha` deploys straight to the hypervisors. Joining them to a manager (Proxmox Datacenter Manager, vCenter) is Day 1 work in `r1beta`. For a platform whose hypervisor lane itself starts in `r1beta`, joining its manager moves to `r1`, as long as DCiaB can deploy to that platform directly in the meantime. Cluster API's vSphere provider needs a vCenter, so until DCiaB deploys one, "directly" on ESXi means through a vCenter the site provides.

Moving clusters between platforms (vSphere to Proxmox VE or back) joins `r1` only if a simple, low-cost way is found; otherwise it is roadmap work for a later release.

## `r1alpha`: Day 0

### Config, archive and charts

- [x] `schema/dciab.schema.json`
- [x] `dciab validate`: schema and offline semantic checks
- [x] `dciab validate --live`: live checks (hypervisor, BMC Redfish, NTP, upstream DNS, ARP probes for unused addresses); not yet run against real infrastructure
- [x] `bundle.lock` with pinned versions (digests come with the build pipeline)
- [x] `capi-clusterclasses` chart: `r1` classes (draft until `r1` GA; see [VERSIONING.md](VERSIONING.md)) for vSphere, Proxmox and Metal3
- [x] `capi-cluster` chart
- [x] `dciab cluster-values`: capi-cluster values for MGMT, INFRA and workload clusters, generated from the site config
- [x] ClusterClass end-to-end test: CAPI and all providers on kind, one Cluster per class, generated objects verified (no infrastructure)
- [ ] Archive build pipeline (`build/README.md` in the source repository; not shipped in the archive): image inventory, image pull, charts, providers, binaries, checksums, SBOMs, packaging and signing (`dciab verify-archive`) done; node images next
- [ ] A release workflow that tags `v`+`VERSION` (`vN.0.0-alpha.M`, `vN.0.0-beta.M`, `vN.0.0`) and creates the GitHub release named `rNalphaM`, `rNbetaM` or `rN` ([VERSIONING.md](VERSIONING.md))

### Node images and the first cluster

- [x] Node template recipe: image-builder with a Rocky 10 Proxmox target (`images/node/`)
- [x] First Proxmox template built and verified
- [x] Cilium install for any cluster (`addons/cilium`), with kube-proxy replaced
- [x] Real-cluster spike on Proxmox from the template (a 3+1 cluster on the bootc template with Cilium, node replacement without `/readyz` failures)
- [ ] Node images in the archive
- [ ] Remove the image-builder recipe and the classes' containerd mirror files, now that a cluster passes on the bootc template (see Decisions)

### Bootstrap appliance and USB

Decided in [ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md).

- [x] Config: `bootstrap.interface` by name or MAC; `bootstrap.managementVLAN`; `bootstrap.workDisk`
- [x] Recipes under evaluation: `images/bootstrap-bootc/` (appliance installer ISO), `images/usb/` (Ventoy stick)
- [ ] Spike: unattended install of server zero from the stick with Secure Boot on, first boot from `dciab.yaml`, a short end-to-end check, installers from the same stick
- [ ] Day 0 images preloaded in the appliance
- [x] `dciab media`: copy the USB payload to the work disk, and run the bootstrap's heavy state from it ([MEDIA.md](MEDIA.md))
- [ ] The bootstrap host survives a reboot: service addresses, firewall rules, the services pod and the seed registry come back

### Day 0 services and hypervisors

Decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md).

- [x] Config: `spec.hypervisors` and `spec.time` (which replaced `spec.bootstrap.ntp` and `network.ntpServers`, ADR 0010), with `validate` checks
- [x] `bundle.lock`: Proxmox VE installer pinned; ESXi entry (not shipped); Day 0 service images
- [x] Day 0 image digests pinned (CoreDNS, Caddy, chrony base)
- [x] DCiaB's chrony image published by CI and pinned (`day0.images.chrony`), so the archive ships it
- [x] `dciab services`: CoreDNS, chrony and Caddy as one pod on the DNS service IPs
- [ ] `dciab services` lifecycle: config changes, teardown
- [ ] Day 0 DHCP on the bootstrap host, for sites that need it and have none (for example PXE installs or BMC addressing); Redfish virtual media installs need none
- [x] `dciab hypervisors` for Proxmox VE, first part: answer files inside per-host installer ISOs (served only to each host's BMC), root password from the config or generated and sealed with age, Redfish virtual media boot, wait for the install ([docs/HYPERVISORS.md](HYPERVISORS.md); ADR 0001 amendment 1)
- [x] The CAPMOX API token's exact Proxmox VE privileges, one role per path, checked by `dciab validate --live` ([docs/PROXMOX.md](PROXMOX.md))
- [x] `dciab hypervisors` reinstall guard: the Proxmox VE API's answer, not an open port; OS boot entries and `--first-install` for hosts without an install record ([docs/HYPERVISORS.md](HYPERVISORS.md#the-reinstall-guard))
- [x] `dciab hypervisors` Redfish preparation before an install: UEFI boot mode, Secure Boot per `bootMode`, virtualization extensions, a disk matching `rootDeviceHints` ([docs/HYPERVISORS.md](HYPERVISORS.md#redfish-preparation))
- [x] `dciab hypervisors` post-install through the Proxmox VE API: the CAPMOX roles, user, token and ACLs (secret sealed with age), the VLAN-aware bridge, the storage ([docs/HYPERVISORS.md](HYPERVISORS.md#post-install))
- [ ] `dciab hypervisors`: tagged and untagged management VLANs (a first-boot hook on each host's ISO for tagged; [docs/HYPERVISORS.md](HYPERVISORS.md#the-management-vlan)): built; hardware proof pending (handoff task 0013)
- [x] Live checks: BMC routes, Redfish and credentials (`dciab validate --live`)

### Seed, bootstrap cluster and MGMT

- [x] `dciab status`: read-only report of the Day 0 pod, the platform CA and registry certificate, the seed registry, the bootstrap cluster and where MGMT's Cluster lives
- [x] `dciab seed`: platform CA (generated with its key age-encrypted, or imported), read-only Zot loaded from the payload, kind pulling through it, providers and ClusterClasses from the payload, DNS for `registry.<domain>`; `--remove` undoes it on the host ([SEED.md](SEED.md))
- [ ] Minimal INFRA for Day 0: settle which INFRA services, if any, Day 0 or the pivot need ahead of `r1beta`'s full `dciab infra`, and build them
- [ ] `dciab mgmt`: builds MGMT, installs Cilium and the providers, and moves Cluster API into MGMT; picks up `dciab seed`'s kubeconfig and trust bundle and runs offline from the payload (CI runs seed then mgmt up to the Cluster; not yet proven on real infrastructure). On vSphere, the cloud provider (CPI) is installed before Cilium (`addons/vsphere-cpi`, the `cpi` step)

### vSphere lane (best-effort in `r1alpha`, required in `r1beta`)

Runs in parallel once an ESXi lab is available; otherwise it moves to `r1beta`.

- [ ] ESXi build tested and pinned
- [ ] `dciab hypervisors` for ESXi
- [ ] vCenter deployment on the first ESXi host (datacenter, cluster, networks). Until then, ESXi sites need an existing vCenter
- [ ] vSphere node template: the bootc recipe builds a `vmdk` with VMware Tools; not yet tested

## `r1beta`: Day 1

### Spikes

- [ ] Kea behind a LoadBalancer with DHCP relay
- [ ] DNS service addresses handed over from the bootstrap host to MGMT
- [ ] CAPMOX `v1alpha2` templates, and how a class revision carries them

### Pivot from the bootstrap host

- [ ] MGMT services: Metal3 (BMO + Ironic), PowerDNS, Kea, NetBox, and the DNS handoff; the engine, lease and image-cache questions of ADR 0008 decision 4a
- [ ] Production NTP on MGMT (`spec.time`, ADR 0010 decision 9)
- [ ] Hypervisors joined to their manager: Proxmox VE nodes to Proxmox Datacenter Manager, ESXi hosts to vCenter. Until then DCiaB talks to each hypervisor's API directly (`r1` for vCenter if the vSphere lane starts in `r1beta`)
- [ ] Proxmox VE clustering and shared storage across hosts installed by `dciab hypervisors` ([ADR 0010](adr/0010-failure-domains.md)); ends the `r1alpha` single-host limitation
- [ ] Like-for-like moves from hypervisors DCiaB installed to the site's own: vSphere to vSphere, Proxmox VE to Proxmox VE
- [ ] `dciab finalize`: the images move to Artifact Keeper on INFRA, the seed is torn down and server zero is reclaimed as the last hypervisor
- [ ] Metal3: nodes get `spec.providerID`; the `metal` node image variant tested

### INFRA, platform and tenant clusters

- [ ] `dciab infra`: INFRA with Argo CD, Forgejo, OpenBao and CloudNativePG
- [ ] `dciab platform`: repos pushed, Argo CD and OpenTofu bring up the rest; Argo CD app-of-apps Helm templates for INFRA and for Cluster API tenant clusters, and adoption of script-installed releases
- [ ] Platform TLS: cert-manager issuers from the platform CA and OpenBao PKI
- [ ] Cilium load balancing, Gateway API and network policies
- [ ] Enterprise policy: OpenVox Server, OpenVoxDB and the ENC; host enrollment and profiles
- [ ] Workload clusters and standalone VMs from each class
- [ ] vSphere CPI for INFRA and workload clusters
- [ ] Plain (non-Ventoy) stick for strict Secure Boot sites
- [ ] Archive signing: confirm cosign v3's offline flags before moving off v2

### Identity

- [x] *Built early:* `r1` structured authentication: an `AuthenticationConfiguration` with several JWT issuers, each with its own username and groups prefix (ADR 0008)
- [x] *Built early:* identity provider ([IDENTITY.md](IDENTITY.md), ADR 0008 decision 3): `spec.identity.provider` (`authentik` or `external`), `consumers`, `roles` and `identity.kubernetes` with `validate`'s rules, the tier rule included; each cluster's JWT issuers built from them; the three platform roles as ClusterRoleBindings, applied on MGMT by `dciab mgmt`; Authentik blueprints and the clients list rendered
- [ ] Identity, next: install the blueprints with Authentik on INFRA (platform step); `authentik.upstream` (the site directory behind Authentik); role bindings on INFRA and workload clusters; Proxmox VE and vCenter identity on opt-in; other consumers' role mappings with each service

## `r1`: Day 2

### Storage and failure domains

- [x] *Built early:* block storage step in `dciab mgmt` (the `storage` step: `spec.storage.block` is INFRA's; MGMT keeps no volumes and the step installs nothing there unless `overrides.mgmt` opts in, as workload clusters do with `overrides.workload`; the four StorageClasses; Longhorn from `addons/longhorn` with its chart and images from the payload, on a data disk on vSphere; the Proxmox CSI plugin and the vSphere CSI driver from `addons/csi`, their pinned manifests in the payload)
- [x] *Built early:* `r1` failure domains (ADR 0010): `spec.failureDomains` with `validate` checks; worker pools per domain; vSphere zones and cluster modules; Metal3 failure domains; Proxmox workers pinned per domain, control plane not spread (reported by `validate` and `dciab status`)
- [x] *Built early:* `r1` storage networks on VM nodes (`network.storage`): one NIC per network, MTU, multipath modules
- [ ] Failure domains, next: Metal3 storage NICs, bonds and VLANs; `validate --live` capacity, vSphere tag and Proxmox shared-storage checks; multipath packages in the node image; BareMetalHost topology labels; Longhorn zone settings
- [ ] Storage, next: ceph-csi and the pinned vendor SAN CSI drivers; follow-ups from the storage review
- [ ] Object storage: SeaweedFS (and the S3 shim for NFS-only sites) and COSI bucket provisioning

### Backups

- [x] *Built early:* backups, first part ([BACKUP.md](BACKUP.md), ADR 0009): `spec.backup` (site and off-site targets, tiers, schedules and retention) with `validate`'s tier rule; Longhorn recurring snapshots and backups on MGMT
- [x] *Built early:* backups, MGMT: hourly etcd snapshots and daily CAPI exports (CronJobs), CAPI exports before and after the move, all age-encrypted to the recipients and a backup key; `dciab status` shows the last success of each; restore runbook. The backup image (`images/backup`) is built by CI, pinned once published
- [ ] Backups, next: the off-site copier and removable exports; `validate --live` target checks; INFRA and workload clusters' backups; snapshots before control plane changes; restore drills in CI
- [ ] Hypervisor root-password material moves from the bootstrap state folder into OpenBao (or the site's Vault)

### Upgrades and operations

- [ ] Kubernetes upgrade gate: the recipe builds the starting image (`build.sh --upgrade-from`); not yet used in a test
- [ ] Class revision move on a real cluster, watched until it settles
- [ ] Upgrades, rollback and importing a new archive
- [ ] Full-site cold-start runbook
- [ ] Observability: Prometheus, Alertmanager, Grafana, Fluent Bit, OpenSearch
- [ ] `dciab verify`: checks a running site end to end
- [ ] Failure injection, an air-gapped rehearsal, README and troubleshooting guide

## Later releases

Candidates for `r2` and later. Nothing here is scheduled until that release's roadmap is frozen ([VERSIONING.md](VERSIONING.md)).

- Moving clusters between platforms (vSphere to Proxmox VE or back), unless a simple, low-cost way gets it into `r1`
- Platform services proposed in ADRs 0004–0007: workload identity with SPIFFE/SPIRE ([ADR 0004](adr/0004-identity-and-sso.md)); the IaC path with Terramate, Terratest and Terrakube with OPA ([ADR 0006](adr/0006-iac-path.md)); detection and response with Tetragon, Suricata and a SOAR ([ADR 0007](adr/0007-detection-to-response.md))
- AWX as an Ansible service for tenants
- Tenant volume backups with Velero (ADR 0009); overrides for one workload cluster at a time; a signed, site-built overlay archive for other vendor CSI drivers; a hybrid `secrets: external` mode; OpenEBS Local PV LVM if a site needs node-local disks beside an array (ADR 0008)
- **Only if a site needs them:** PXE as a second install method; a direct out-of-band interface on the bootstrap host (which Day 0 DHCP for BMCs needs); separate NTP service addresses

## Platform interoperability

Two pieces exist: `dciab cluster-values` already gives each cluster its JWT issuers from `spec.identity` ([IDENTITY.md](IDENTITY.md)), and the ClusterClasses already write a Kubernetes API audit log for log collection to pick up. Authentik SSO and OpenBao with External Secrets Operator come with INFRA in `r1beta`.

- [ADR 0004](adr/0004-identity-and-sso.md): identity and SSO (Authentik, SPIFFE/SPIRE)
- [ADR 0005](adr/0005-secrets.md): secrets (OpenBao, External Secrets Operator, platform PKI)
- [ADR 0006](adr/0006-iac-path.md): the IaC path (OpenTofu, Terramate, Terratest, Terrakube with OPA)
- [ADR 0007](adr/0007-detection-to-response.md): detection to response (Tetragon, Suricata, OpenSearch, SOAR)

## Decisions

- **Node image recipe: image mode (bootc) with CRI-O is the choice.** It builds and boots on Proxmox VE with SELinux enforcing, and a cluster has passed on the bootc template. The image-builder recipe and the classes' containerd mirror files are removed next (open item under `r1alpha`).
- **CRI-O instead of containerd.** It comes with the bootc recipe, which installs CRI-O. The `r1` classes already write CRI-O's mirror files next to containerd's, and kubeadm finds the only runtime socket on its own. What's left is removing the containerd files and the image-builder recipe.
