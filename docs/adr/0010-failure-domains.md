# ADR 0010: Failure domains — placement, topology labels, minimum hosts, network and time

- Status: proposed
- Date: 2026-11-23

## Context

Nothing in DCiaB places machines or replicas with the hardware in mind:

- **No failure-domain model.** No `zone`, `failureDomain`, anti-affinity or `topology.kubernetes.io` key exists in `schema/`, `charts/` (the `r1` classes in `charts/capi-clusterclasses/classes/r1/` and `charts/capi-cluster/`), `addons/` or `lib/`. A VM cluster has sizes and addresses only (`schema/dciab.schema.json:309-321`). Proxmox takes one storage and a flat node list (`:164-186`). CAPMOX gets every node as `allowedNodes` (`lib/dciab_cluster/values.py:135-141`, `charts/capi-clusterclasses/classes/r1/proxmox.yaml:499-501`). The Cluster topology sets control plane replicas and worker pools, with no failure domain (`charts/capi-cluster/templates/cluster.yaml:25-43`).
- **So quorum can sit on one host.** All three etcd members of MGMT or INFRA, and every Longhorn replica, can land on one hypervisor. Losing that host loses quorum and data. MGMT has no worker pools in the examples (`examples/proxmox-bare.yaml:63`), so its Longhorn replicas live on its control plane nodes too.
- **The only resilience check** warns on one control plane node or one Longhorn replica (`lib/dciab_validate/checks.py:540-548`).
- **The bare-Proxmox path is one host.** `examples/proxmox-bare.yaml:15-20` points at one API (`pve01`), lists three nodes, and uses `local-lvm`. Hosts installed by `dciab hypervisors` aren't clustered; Proxmox clustering and shared storage are backlog (`docs/ROADMAP.md:73`). A clone on local storage can't target another node: CAPMOX's own clone `target` is "only allowed if the original VM is on shared storage". Every MGMT, INFRA and workload VM therefore lands on `pve01`. The comment at `:76`, "the Ceph datastore already replicates", is wrong for this example. The project's design notes accept single-host local storage as a v1 limitation; no product doc says so.
- **One flat network.** Hypervisors, the bootstrap host, MGMT, INFRA and workload clusters share `network.nodes`: one CIDR, gateway and VLAN (`schema/dciab.schema.json:226-235`). There is no MTU field anywhere. Bare-metal nodes get one untagged `phy` link with no bond (`charts/capi-cluster/templates/_helpers.tpl:60-69`).
- **No production NTP.** [ADR 0001](0001-day0-hypervisors-and-services.md):77 says the production platform must serve NTP on the DNS service addresses after the handoff whenever `spec.bootstrap.ntp` was used. [COMPONENTS.md](../COMPONENTS.md) has chrony only on the bootstrap host (line 22), and nothing replaces it.

ADR 0008 decides which block and object providers a site uses (an array first, Longhorn or OpenEBS only on local disks, never two layers of replication) and gives every cluster `replicated`, `replicated-retain` and, where it exists, `local` StorageClasses. ADR 0009 decides backups and their targets. This ADR decides where machines and replicas go so that losing one host, rack or zone doesn't take quorum or the only copy with it.

The `r1` classes are still a draft ([VERSIONING.md](../VERSIONING.md)). Whatever the classes must carry has to land before the first release freezes them.

## Decisions

### 1. A failure domain is a named set of hardware, declared once per site

A new optional `spec.failureDomains` lists the site's failure domains. Each has a name and, per platform, the hardware in it. The name is the value of `topology.kubernetes.io/zone` on every node placed there (decision 3).

```yaml
spec:
  failureDomains:
    - name: rack-a
      proxmox: {nodes: [pve01]}                 # Proxmox: one or more PVE nodes
    - name: rack-b
      proxmox: {nodes: [pve02]}
    - name: rack-c
      proxmox: {nodes: [pve03]}
    # vSphere instead:
    # - name: az1
    #   vsphere: {computeCluster: c1, hostGroup: az1-hosts, vmGroup: az1-vms, datastore: az1-ds, network: dciab-nodes}
  baremetal:
    hosts:
      - name: bm01
        failureDomain: rack-a                   # new; must name a spec.failureDomains entry
        ...
```

- **Granularity is the site's choice.** The smallest useful unit is one hypervisor host. A rack, a power feed or a room works too, as long as each domain fails independently.
- **One list for the whole site.** A Proxmox site with bare-metal workload clusters uses the same names for both, so `rack-a` means the same rack to every cluster.
- **Clusters use all of them by default.** A cluster or a worker pool may narrow the list (`failureDomains: [rack-a, rack-b]`), which is how a workload cluster stays in one room.
- **No list means one domain.** A site without `spec.failureDomains` is a single failure domain, and `validate` says so (decision 6).

`topology.kubernetes.io/region` isn't set in v1: a DCiaB site is one region.

### 2. How each platform places machines

Cluster API has one model: the infrastructure cluster reports failure domains, KubeadmControlPlane spreads control plane machines across those marked for the control plane, and each MachineDeployment names one failure domain. DCiaB expands a worker pool into one MachineDeployment per failure domain, named `<pool>-<domain>`, and splits the replicas as evenly as it can (3 over 3 domains is 1+1+1; 4 is 2+1+1). The providers differ in how far they support this today.

| | Control plane spread | Worker placement | Zone label source |
| --- | --- | --- | --- |
| **CAPM3** (Metal3) | Yes. `Metal3Cluster.spec.failureDomains`; KCP spreads; CAPM3 picks a BareMetalHost labelled `infrastructure.cluster.x-k8s.io/failure-domain=<name>` | MachineDeployment `failureDomain` | CAPM3 label sync from BareMetalHost labels |
| **CAPV** (vSphere), with `spec.failureDomains` | Yes. `VSphereFailureDomain` + `VSphereDeploymentZone` per domain; KCP spreads | MachineDeployment `failureDomain` | vSphere CPI, from `k8s-region`/`k8s-zone` tags |
| **CAPV**, one compute cluster, no domains | Host spread inside the cluster through CAPV's cluster modules (DRS VM-VM anti-affinity) | Same, per MachineDeployment | none (one zone) |
| **CAPMOX** (Proxmox VE) `v0.9.2` | **Not possible yet** | Per-MachineDeployment `allowedNodes` | kubelet `--node-labels`, workers only |

**Metal3.** CAPM3 `v1.14.2` (pinned) has `failureDomains` on `Metal3Cluster` and `failureDomain` on `Metal3Machine`. DCiaB renders one entry per domain that has hosts, with `controlPlane: true`, and puts two labels on each BareMetalHost: the CAPM3 failure-domain label and `topology.kubernetes.io/zone`. If no host is free in the chosen domain, CAPM3 takes one from another domain, so `validate` checks there are enough hosts per domain (decision 6). The existing `hostSelector` (`charts/capi-clusterclasses/classes/r1/metal3.yaml:477-480`) still selects the host role; the failure domain narrows it.

**vSphere.** Two modes:

- *Several zones* (compute clusters, or DRS host groups inside one cluster). Each `vsphere` entry renders a `VSphereFailureDomain` (region type `ComputeCluster`, zone type `HostGroup`; or `Datacenter`/`ComputeCluster` for one cluster per zone) and a `VSphereDeploymentZone` with `controlPlane: true`. The `VSphereClusterTemplate` gets `failureDomainSelector: {}`, so the control plane uses every zone. CAPV no longer creates the vSphere side (`autoConfigure` is gone), so the site creates the tag categories, tags and DRS groups first. `validate --live` checks they exist. Today the schema requires `vsphere.cluster` but `values.py` never passes it on (`lib/dciab_cluster/values.py:145-148`); in this mode it becomes the default compute cluster.
- *One compute cluster, no zones.* DCiaB enables CAPV's `NodeAntiAffinity` feature gate (`EXP_NODE_ANTI_AFFINITY`, off by default, in `bundle.lock`'s provider variables). CAPV then puts the control plane and each MachineDeployment in their own cluster module, and DRS keeps each group's VMs on separate hosts. This needs DRS on the cluster.

**Proxmox.** CAPMOX `v0.9.2` (pinned) has "zones", but they are IP pools per zone, not Cluster API failure domains: `ProxmoxCluster.status.failureDomains` is never set, and `ProxmoxMachine` has no `failureDomain`. Upstream issue #717 proposes the contract (a zone lists its PVE nodes, and the machine controller limits `allowedNodes` and the IP pool to the machine's failure domain). It is open. Until it ships:

- **Workers** are placed per MachineDeployment. `ProxmoxMachine` has its own `allowedNodes`, which overrides the cluster's. The class sets it from a per-MachineDeployment `failureDomain` variable to that domain's PVE nodes, and adds `topology.kubernetes.io/zone=<domain>` to the kubelet's `--node-labels`. The kubelet may set this label on its own Node.
- **The control plane can't be pinned.** KubeadmControlPlane uses one machine template, and CAPMOX places each VM on any allowed node by free memory. Three control plane VMs may share a host. DCiaB can't fix this without the upstream contract, so it reports it: `dciab status` lists each quorum member's PVE node and warns when two share one. Control plane spread on Proxmox comes in the class revision that pins a CAPMOX release with failure domains.
- **Shared storage is required for any spread.** With node-local storage, the template exists on one node and a clone can't go anywhere else. Spreading needs a clustered Proxmox with shared storage (Ceph RBD, or a SAN), or one template copy per node. Both are roadmap (decision 7).

### 3. One topology label, set from the start

Every node carries `topology.kubernetes.io/zone=<failure domain name>`. Nothing in DCiaB uses another key. The label must be there before anything schedules on the node, because Longhorn and the Kubernetes scheduler place replicas from it.

| Platform | Control plane nodes | Worker nodes |
| --- | --- | --- |
| Metal3 | CAPM3 label sync (`metal3.io/metal3-label-sync-prefixes: topology.kubernetes.io` on the `Metal3Cluster`) | same |
| vSphere, zones | vSphere CPI (`labels: {region: k8s-region, zone: k8s-zone}` in its config) | same |
| vSphere, one cluster | not set | not set |
| Proxmox | not set until CAPMOX failure domains (decision 2) | kubelet `--node-labels`, from the MachineDeployment's `failureDomain` variable |

Core Cluster API syncs only `node-role.kubernetes.io`, `node-restriction.kubernetes.io` and `node.cluster.x-k8s.io` labels from Machines to Nodes, and sets no zone label itself. When Proxmox gets control plane failure domains, the control plane nodes still need a source. That is open question 3.

### 4. How stateful services use the label

The services keep their own replicas apart by zone, and by host inside a zone. ADR 0008 puts services that replicate themselves on the `local` class where one exists, so their spread is the only thing between one host failure and data loss.

| Service | Members | Rule |
| --- | --- | --- |
| etcd (every cluster) | 3 (or 5) control plane machines | Decision 2. KCP spreads where the provider reports failure domains |
| Longhorn (`replicated`, on local disks only, per ADR 0008) | `replicaCount` | `replicaSoftAntiAffinity: false` (never two replicas on one node, Longhorn's default, now set explicitly). `replicaZoneSoftAntiAffinity: false` when the cluster has at least `replicaCount` zones with Longhorn nodes, `true` otherwise, so a volume can still become healthy. `replicaAutoBalance: best-effort`, so replicas move back after a zone returns |
| Longhorn `local` class | 1, `strict-local` | The service's own replicas are what is spread |
| CloudNativePG | 3 instances | Keep its default pod anti-affinity on `kubernetes.io/hostname`, set `podAntiAffinityType: required`, and add a topology spread constraint on `topology.kubernetes.io/zone` (`maxSkew: 1`; `DoNotSchedule` with at least 3 zones, `ScheduleAnyway` below) |
| OpenBao (Raft) | 3 voters (5 at large sites) | Required pod anti-affinity on hostname, and the same zone spread constraint. OpenBao's autopilot has no redundancy zones (a Vault Enterprise feature), so placement is the only protection |
| SeaweedFS | 3 masters, N volume servers | Masters: same as OpenBao. Volume servers start with `-dataCenter=<site>` and `-rack=<zone>`, read from their node's label. Replication follows ADR 0009: `010` (a copy in another rack, here another zone) with 2 or more failure domains, `001` (another server) with one |
| OpenSearch | 3 cluster-manager nodes, data nodes | The same zone spread; shard allocation awareness on the zone attribute |

**When the site's array provides redundancy** (vSphere CSI, Ceph CSI or a vendor CSI driver under ADR 0008), the array keeps the disk safe but not the VM. A dead ESXi host still stops every pod on it until vSphere HA restarts its VMs elsewhere, so the services' own replicas still spread across zones and hosts, as in the table. What changes is the storage layer:

- No Longhorn, so no replica rules. The array's own layout decides where the copies are, and it is the site's.
- StorageClasses use `volumeBindingMode: WaitForFirstConsumer`, so a volume is created after the scheduler has picked a zone for the pod.
- With vSphere zones, the CSI driver needs topology too (`useTopologyCategories` with the same `k8s-region`/`k8s-zone` tags), so a volume lands on a datastore its zone can reach. A datastore shared by every zone needs nothing.

### 5. Minimum hosts per tier

These are the numbers that survive the loss of one failure domain. "Domains" means distinct failure domains the members can use.

| Tier | Members | Domains to survive one loss | Below that |
| --- | --- | --- | --- |
| MGMT control plane (etcd; Longhorn and controllers when it has no workers) | 3 | 3 | warning |
| INFRA control plane | 3 | 3 | warning |
| INFRA workers (Longhorn, CloudNativePG, OpenBao, SeaweedFS, OpenSearch) | at least 3 | 3 | warning |
| Workload cluster with 3 control plane nodes | 3 | 3 | warning |
| Workload cluster with 1 control plane node | 1 | 1 | the existing warning |

- **Two domains are a trap.** Three members over two domains put two in one of them; losing that one loses quorum. So two domains get their own warning: they protect workers and Longhorn replicas, not etcd.
- **Capacity is N+1.** The VMs of the failed domain must fit on the others, or the cluster stays degraded. `validate --live` adds up each tier's VM memory and warns when the remaining hypervisors can't hold it after the largest domain fails.
- **A full HA site needs at least three hypervisor hosts** (or three bare-metal hosts per control plane), each its own domain, with shared storage on Proxmox.

### 6. What `dciab validate` checks

Offline:

- **Errors** (the config can't be right):
  - a failure domain names a Proxmox node not in `platform.proxmox.nodes`, a node appears in two domains, or two domains share a name;
  - a cluster, worker pool or BareMetalHost names a domain that isn't declared;
  - a generated MachineDeployment name (`<pool>-<domain>`) breaks the name rules;
  - `spec.hypervisors` installs Proxmox (so DCiaB knows the storage is a fresh install's local storage: `local`, `local-lvm` or `local-zfs`) and `platform.proxmox.nodes` lists more than the template node. The clones can't go there.
- **Warnings** (it works, but a single failure hurts more than it looks):
  - control plane replicas greater than the domains the control plane can use (for example 3 control plane nodes on fewer than 3 domains), named with what the loss of the busiest domain would do;
  - exactly two domains, for any cluster with 3 control plane nodes;
  - Longhorn `replicaCount` greater than the zones with Longhorn nodes;
  - a Metal3 domain with fewer free hosts than the machines KCP or a MachineDeployment will put there;
  - Proxmox with failure domains: the control plane can't be spread yet (decision 2);
  - `spec.bootstrap.ntp.serveLocalClock: true`, which today is only a note: the site then has one time source (decision 9).

`--live`:

- **Errors:** a node in `platform.proxmox.nodes` that the API at `platform.proxmox.url` doesn't know (an unclustered host); node-local storage (the storage's `shared` flag is off) with more than one allowed node.
- **Warnings:** the N+1 capacity check (decision 5); vSphere tag categories, tags or DRS groups missing for a declared domain; no DRS on the compute cluster when cluster modules are used.

After the build, `dciab status` shows each quorum member's host and warns when two share one.

The existing `check_resilience` (`lib/dciab_validate/checks.py:540-548`) becomes part of this check. Its "unless the datastore underneath already replicates" hint goes away: ADR 0008's double-replication rule covers that case.

### 7. The single-host limitation in v1

**Stated plainly:** in v1, a Proxmox site installed from bare servers by `dciab hypervisors` is one failure domain. The hosts aren't clustered, CAPMOX talks to one host's API, and local storage can't clone to another node. Every MGMT, INFRA and workload VM runs on one host. If that host fails, the whole platform stops, and its data survives only in ADR 0009's backups on the site and off-site targets.

How it is surfaced:

- `validate` prints one warning at `spec.platform.proxmox`, whichever check finds it: "this site is one failure domain: losing <host> stops MGMT, INFRA and every workload cluster; restore is from backups (ADR 0009)". With failure domains declared, a second hypervisor that `spec.hypervisors` installs but CAPMOX can't use is an error (decision 6).
- The product docs say it where an operator chooses the path: `README.md`, `docs/HYPERVISORS.md` and `docs/PROXMOX.md` get a "single failure domain" note, and `examples/proxmox-bare.yaml` lists only `pve01` with its wrong Ceph comment removed.
- It ends when Proxmox clustering with shared storage (Ceph, or a SAN) is built. That moves from backlog to a Phase 2 gate. Clustering also gives CAPMOX one API for all nodes; `platform.proxmox.url` should then point at a name or address that more than one node answers, not one host.

### 8. Network: MTU, bonds and separate VLANs

| Field | Where it goes | When |
| --- | --- | --- |
| `mtu` on `network.nodes` and on each `baremetal.networks` entry (optional, default 1500) | Proxmox and vSphere machine NICs (both providers have an `mtu` field), Metal3 `networkData`. Cilium reads the MTU from the node's interface | **v1.** The VM classes need a variable in `r1` |
| `bond` on a BareMetalHost: `mode` (`802.3ad` or `active-backup`) and the member NICs' MACs | Metal3 `networkData` `bonds` (CAPM3 has `bondMode` and `bondLinks`); the node IP moves to the bond | **v1.** Lives in `capi-cluster`'s `Metal3DataTemplate`, not in a class |
| `tagged: true` on a `baremetal.networks` entry | A `vlans` link (`vlanID`, `vlanLink`) on the NIC or bond, for trunk ports | **v1**, same place |
| Bonds and VLANs on hypervisors | `dciab hypervisors` answer files and bridge setup | Roadmap, with the existing "tagged management VLAN, bridge" item |
| Separate node networks per cluster for VM clusters (`network: <name>`, as Metal3 already has) | Each cluster's port group, or bridge and VLAN | With workload clusters (Phase 4). The bridge and port group are already per-cluster class variables, so no class change |
| A storage network (Longhorn replication, iSCSI) on its own VLAN | A second NIC on the machine templates; Longhorn's storage network needs Multus | Roadmap. A class revision after `r1` |
| BMC traffic | Already separate: `baremetal.bmcNetworks`, routed | No change |

The reference VLAN plan the docs should show: out-of-band (BMCs), hypervisor management, platform nodes (MGMT and INFRA), tenant nodes, and storage. In v1 the first two are separate and the platform shares one node network; tenant separation arrives with workload clusters.

### 9. Production NTP after the Day 0 handoff

When `spec.bootstrap.ntp` is set, every node, hypervisor and BMC takes its time from the two DNS service addresses. So the platform takes over NTP when it takes over DNS:

- **Where.** Two chrony Deployments on MGMT, one replica each, in different failure domains. Each sits behind a Cilium LoadBalancer Service on one of the two DNS service addresses, UDP 123, sharing the address with DNS (Cilium LB IPAM's sharing key). `externalTrafficPolicy: Local` keeps client addresses for chrony's `allow` list (the node network). Not a host-network DaemonSet: the node's own `chronyd` holds UDP 123, the same conflict `dciab services` works around on Day 0.
- **Upstreams.** The same as the bootstrap host's: `spec.bootstrap.ntp.upstream`. The two servers also peer with each other. With no upstream (`serveLocalClock: true`) both run `local stratum 10 orphan`, so they agree with each other rather than drift apart. The docs recommend at least two hardware or GPS-backed sources for isolated sites, because otherwise the site's time is just a guess that stays consistent.
- **Handoff without a step.** Before the addresses move, MGMT's chrony syncs from the bootstrap host's own address (`bootstrap.address`), so the time on both sides is the same when clients switch. That upstream is removed at `finalize`.
- **When the site has its own NTP** (`spec.bootstrap.ntp` unset), nodes keep using `network.ntpServers` and DCiaB runs no NTP service.
- COMPONENTS.md gains a "Time" row: chrony, MGMT, Planned. It lands with the DNS handoff in `dciab mgmt`.

### 10. What lands in `r1`, and what can wait

`r1` is a draft until the first release. Anything a class must carry goes in now, or waits for `r2` and rolls every cluster that moves to it.

**In `r1`, before the first release:**

- Proxmox: a `failureDomain` variable, overridable per MachineDeployment, that sets the worker `ProxmoxMachineTemplate`'s `allowedNodes` and adds `topology.kubernetes.io/zone` to the worker kubelet's `--node-labels`.
- vSphere: a variable that sets `failureDomainSelector: {}` on the `VSphereClusterTemplate` when the site declares zones.
- Metal3: a variable that fills `Metal3ClusterTemplate`'s `failureDomains`, and the label-sync annotation on the `Metal3Cluster`. Whether the class can set that annotation through the template's metadata, or DCiaB sets it after the cluster exists, is checked in the implementing PR.
- Proxmox and vSphere: an optional `mtu` variable on the machine NIC.

**Outside the classes, any time:** the schema and `validate` (decisions 1, 6, 7); `values.py` and `capi-cluster` (per-domain MachineDeployments with `failureDomain`, BareMetalHost labels, `VSphereFailureDomain` and `VSphereDeploymentZone` objects, Metal3 bonds, VLANs and MTU); `EXP_NODE_ANTI_AFFINITY` in `bundle.lock`; the Longhorn settings in `addons/longhorn/values.yaml`; the service charts' spread rules; production NTP; the docs and the example.

**`r2` or later:** Proxmox control plane spread and control plane zone labels, once a CAPMOX release with failure domains is pinned; MachineHealthCheck, which should come with failure domains so a replacement doesn't land beside the survivor; the second NIC for a storage network.

## Alternatives considered

- **Leave placement to the hypervisor.** vSphere DRS rules or Proxmox HA rules alone can keep VMs apart, but Kubernetes, Longhorn and the operators never learn the topology, so replicas can still pile into one zone. Proxmox's HA rules also need a clustered Proxmox with shared storage, which v1 doesn't have. Kept only as CAPV's cluster modules, where CAPV manages the rules itself.
- **Pod anti-affinity on hostname only.** Cheap, and CloudNativePG does it by default. Three VMs on one host are three hostnames, so it protects nothing on its own. Rejected as the only rule.
- **Reject single-domain sites.** Safer, but it removes the bare-Proxmox path that small and lab sites depend on. Rejected for v1: the limitation is warned about and documented instead.
- **Wait for CAPMOX failure domains before spreading anything on Proxmox.** Workers can be placed today with per-machine `allowedNodes`. Rejected for workers; accepted for the control plane, because there is no clean way around it.
- **A DCiaB controller that moves Proxmox control plane VMs apart.** It would fight CAPMOX's placement and duplicate the upstream proposal. Rejected.
- **Hard zone anti-affinity in Longhorn always.** With fewer zones than replicas, volumes never become healthy. Rejected; it is hard only when there are enough zones.
- **Separate VLANs for every tier in v1.** Needs a second NIC in every class and Multus for Longhorn's storage network. Deferred, except for what costs no class change.
- **NTP on the hypervisors.** They are NTP clients of the same addresses, ESXi isn't meant to serve time, and the hypervisors aren't DCiaB's to configure after the install. Rejected.

## Consequences

- The schema gains `spec.failureDomains`, `baremetal.hosts[].failureDomain`, `bond`, `tagged` and `mtu`. All are optional, so today's configs stay valid, and they become single-domain sites with a warning.
- A worker pool becomes one MachineDeployment per failure domain. Pool names, replica counts and `dciab status` output change to match.
- vSphere sites with zones must prepare tags, tag categories and DRS groups before `dciab mgmt`. The docs list them, and `validate --live` checks them.
- Proxmox control plane quorum stays unprotected until CAPMOX ships failure domains and DCiaB pins it in a new class revision. `dciab status` makes it visible.
- `examples/proxmox-bare.yaml` changes (one node, no Ceph comment), or the new `validate` error fails it. `examples/proxmox.yaml` already changes under ADR 0008.
- MGMT gains an NTP service that must be up before the DNS addresses move. The DNS handoff test covers NTP too.
- Phase 2's gate gains Proxmox clustering with shared storage, which ends the single-host limitation.

## Open questions for Trevor

1. **Proxmox control plane before CAPMOX failure domains.** Accept unspread control planes on Proxmox in `r1` (reported by `dciab status`), or help land upstream PR #720 and pin a CAPMOX build with it before the first release?
2. **Single-domain sites.** Is a warning enough, or should the config have to say so (for example `failureDomains: []` with a `reason`, as ADR 0009 does for off-site backups) before `validate` passes?
3. **Zone labels for Proxmox control plane nodes**, once they can be spread: a small DCiaB controller on MGMT that copies `Machine.spec.failureDomain` to the Node, or wait for a CAPMOX or Cluster API feature?
4. **Two-host sites.** Support them at all? etcd needs a third member somewhere; DCiaB has no witness role.
5. **CAPV cluster modules by default.** They need DRS, which not every vSphere licence includes. Turn `NodeAntiAffinity` on for every vSphere site, or only when the config asks?
6. **Where time sources live.** Production NTP reads `spec.bootstrap.ntp.upstream`. Move the time settings to their own block (`spec.time`) now, while the schema is `v1alpha1`?
7. **Storage network priority.** A second NIC for Longhorn and iSCSI traffic in `r2`, or only when a site asks?

## References

- CAPMOX `v0.9.2` components (zones as IP pools, `allowedNodes` on `ProxmoxMachine`, clone `target` needs shared storage): https://github.com/ionos-cloud/cluster-api-provider-proxmox/releases/download/v0.9.2/infrastructure-components.yaml
- CAPMOX issue #717, "implement CAPI failure domain contract for v1alpha2 zones": https://github.com/ionos-cloud/cluster-api-provider-proxmox/issues/717
- Cluster API failure domains (introduced in v1alpha3): https://kubernetes.io/blog/2020/04/21/cluster-api-v1alpha3-delivers-new-features-and-an-improved-user-experience/
- Cluster API metadata propagation (labels synced from Machines to Nodes): https://main.cluster-api.sigs.k8s.io/reference/api/metadata-propagation
- CAPM3 failure domains: https://metal3io.netlify.app/capm3/failure_domain
- CAPM3 label synchronization: https://metal3io.netlify.app/capm3/label_sync
- CAPM3 `v1.14.2` components (`failureDomains`, `bondMode`, `bondLinks`, `vlanID`, `mtu`): https://github.com/metal3-io/cluster-api-provider-metal3/releases/download/v1.14.2/infrastructure-components.yaml
- CAPV `v1.17.0` components (`VSphereFailureDomain`, `VSphereDeploymentZone`, `failureDomainSelector`, `NodeAntiAffinity` gate): https://github.com/kubernetes-sigs/cluster-api-provider-vsphere/releases/download/v1.17.0/infrastructure-components.yaml
- vSphere multi-AZ with CAPV (zone types, tag categories, `autoConfigure` removed, CSI topology): https://docs.vmware.com/en/VMware-Tanzu-Kubernetes-Grid/2.5/using-tkg/workload-clusters-multi-az-vsphere.html
- Node anti-affinity with vSphere cluster modules: https://apps-cloudmgmt.techzone.vmware.com/blog/tkg-node-anti-affinity
- vSphere CPI with zones (`labels` region and zone): https://cloud-provider-vsphere.sigs.k8s.io/tutorials/deploying_cpi_with_multi_dc_vc_aka_zones
- Labels the kubelet may set on its own Node (includes `topology.kubernetes.io/zone`): https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kubelet/pkg/apis/well_known_labels.go
- Pod topology spread constraints: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints
- Longhorn replica scheduling (node, zone and disk anti-affinity; zone from `topology.kubernetes.io/zone`): https://longhorn.io/docs/1.10.0/nodes-and-volumes/nodes/scheduling
- CloudNativePG scheduling (anti-affinity defaults): https://cloudnative-pg.io/docs/1.28/scheduling
- OpenBao autopilot (no redundancy zones): https://openbao.org/docs/concepts/integrated-storage/autopilot/
- Vault redundancy zones (Enterprise only, for contrast): https://developer.hashicorp.com/vault/docs/v1.21.x/enterprise/redundancy-zones
- SeaweedFS replication (`xyz` placement, `-dataCenter` and `-rack`): https://github.com/seaweedfs/seaweedfs/wiki/Replication
