# ADR 0010: Failure domains — placement, topology labels, minimum hosts, network and time

- Status: Accepted (2026-11-23)
- Date: 2026-11-23
- Amended 2026-11-23: MachineHealthCheck moves from `r2` to `r1` (decision 10).
- Amended 2026-11-23, in the implementation (decisions 2 and 10):
  - vSphere: each cluster gets its own `VSphereFailureDomain` and `VSphereDeploymentZone` per domain (`<cluster>-<domain>`, labelled `dciab.io/cluster`), and the selector matches that label instead of `failureDomainSelector: {}`. Both kinds are cluster-scoped: `{}` would take every cluster's zones, two Helm releases can't own one object, and a cluster couldn't narrow its list. The zone tag stays the domain name.
  - Proxmox: a cluster's `allowedNodes` are its domains' nodes, not every node; with no domains declared, the template's node. Every node then carries the zone label the Proxmox CSI driver needs.
  - Metal3: the label-sync annotation sits in the `Metal3ClusterTemplate`'s `spec.template.metadata`, which the topology controller copies to the `Metal3Cluster`. That answers the open question in decision 10.

## Context

Nothing in DCiaB places machines or replicas with the hardware in mind:

- **No failure-domain model.** No `zone`, `failureDomain`, anti-affinity or `topology.kubernetes.io` key exists in `schema/`, `charts/` (the `r1` classes in `charts/capi-clusterclasses/classes/r1/` and `charts/capi-cluster/`), `addons/` or `lib/`. A VM cluster has sizes and addresses only (`schema/dciab.schema.json:309-321`). Proxmox takes one storage and a flat node list (`:164-186`). CAPMOX gets every node as `allowedNodes` (`lib/dciab_cluster/values.py:135-141`, `charts/capi-clusterclasses/classes/r1/proxmox.yaml:499-501`). The Cluster topology sets control plane replicas and worker pools, with no failure domain (`charts/capi-cluster/templates/cluster.yaml:25-43`).
- **So quorum can sit on one host.** All three etcd members of MGMT or INFRA, and every Longhorn replica, can land on one hypervisor. Losing that host loses quorum and data. MGMT has no worker pools in the examples (`examples/proxmox-bare.yaml:63` on `main` @ 11cebc3), so its Longhorn replicas live on its control plane nodes too.
- **The only resilience check** warns on one control plane node or one Longhorn replica (`lib/dciab_validate/checks.py:540-548`).
- **The bare-Proxmox path is one host.** On `main` @ 11cebc3, `examples/proxmox-bare.yaml:15-20` points at one API (`pve01`), lists three nodes, and uses `local-lvm`. Hosts installed by `dciab hypervisors` aren't clustered; Proxmox clustering and shared storage were in the roadmap's backlog (now a Phase 2 gate, decision 7). A clone on local storage can't target another node: CAPMOX's own clone `target` is "only allowed if the original VM is on shared storage". Every MGMT, INFRA and workload VM therefore lands on `pve01`. Its comment at `:76`, "the Ceph datastore already replicates", is wrong for this example (an open PR rewrites it). The project's design notes accept single-host local storage as a v1 limitation; no product doc says so.
- **One flat network.** Hypervisors, the bootstrap host, MGMT, INFRA and workload clusters share `network.nodes`: one CIDR, gateway and VLAN (`schema/dciab.schema.json:226-235`). There is no MTU field anywhere. Bare-metal nodes get one untagged `phy` link with no bond (`charts/capi-cluster/templates/_helpers.tpl:60-69`).
- **No production NTP.** [ADR 0001](0001-day0-hypervisors-and-services.md):77 says the production platform must serve NTP on the DNS service addresses after the handoff whenever `spec.bootstrap.ntp` was used. [COMPONENTS.md](../COMPONENTS.md) has chrony only on the bootstrap host (line 22), and nothing replaces it.

[ADR 0008](0008-provider-pattern.md) decides which block and object providers a site uses. Its v1 block providers are Longhorn, vSphere CSI, the Proxmox CSI driver (the default on Proxmox), ceph-csi when the site supplies a Ceph key, and a pinned set of vendor SAN drivers; OpenEBS is not in v1. The rule is the site's storage first, Longhorn only on local disks, and never two layers of replication. Every cluster gets the `replicated`, `replicated-retain`, `local` and `local-retain` StorageClasses. [ADR 0009](0009-backup-and-dr.md) decides backups and their targets. This ADR decides where machines and replicas go so that losing one host, rack or zone doesn't take quorum or the only copy with it.

The `r1` classes are still a draft ([VERSIONING.md](../VERSIONING.md)). Whatever the classes must carry has to land before the first release freezes them.

## Decisions

### 1. A failure domain is a named set of hardware, declared once per site

A new `spec.failureDomains` block lists the site's failure domains. Each has a name and, per platform, the hardware in it. The name is the value of `topology.kubernetes.io/zone` on every node placed there (decision 3).

```yaml
spec:
  failureDomains:
    domains:
      - name: pve01
        proxmox: {node: pve01}                  # Proxmox: one PVE node, and the name is the node's
      - name: pve02
        proxmox: {node: pve02}
      - name: pve03
        proxmox: {node: pve03}
      # vSphere instead:
      # - name: az1
      #   vsphere: {computeCluster: c1, hostGroup: az1-hosts, vmGroup: az1-vms, datastore: az1-ds, network: dciab-nodes}
      # Metal3: any name; hosts refer to it (below)
    # Required with fewer than three domains (none, one or two):
    # notHighlyAvailable:
    #   reason: one server at an edge site; restore is from backups
  baremetal:
    hosts:
      - name: bm01
        failureDomain: rack-a                   # new; must name a spec.failureDomains entry
        ...
```

- **Granularity is the site's choice.** The smallest useful unit is one hypervisor host. A rack, a power feed or a room works too, as long as each domain fails independently. On Proxmox, v1 has one PVE node per domain, named after the node, because the Proxmox CSI driver reads the node name from the same label (decision 3). Domains of several PVE nodes come with CAPMOX failure domains (decision 2).
- **One list for the whole site.** A Proxmox site with bare-metal workload clusters uses the same names for both, so `rack-a` means the same rack to every cluster.
- **Clusters use all of them by default.** A cluster or a worker pool may narrow the list (`failureDomains: [rack-a, rack-b]`), which is how a workload cluster stays in one room.
- **Fewer than three domains must be acknowledged.** A site with no domains (one failure domain), one or two must set `notHighlyAvailable.reason`, as ADR 0009 does for `offsite: none`. Without it, `validate` errors (decision 6). Single- and two-host sites are allowed, and the docs say plainly that they aren't highly available.

`topology.kubernetes.io/region` is set only on Proxmox, where the Proxmox CSI driver requires it; its value is the site name (`metadata.name`). Elsewhere a DCiaB site is one region and the label isn't set.

### 2. How each platform places machines

Cluster API has one model: the infrastructure cluster reports failure domains, KubeadmControlPlane spreads control plane machines across those marked for the control plane, and each MachineDeployment names one failure domain. DCiaB expands a worker pool into one MachineDeployment per failure domain, named `<pool>-<domain>`, and splits the replicas as evenly as it can (3 over 3 domains is 1+1+1; 4 is 2+1+1). The providers differ in how far they support this today.

| | Control plane spread | Worker placement | Zone label source |
| --- | --- | --- | --- |
| **CAPM3** (Metal3) | Yes. `Metal3Cluster.spec.failureDomains`; KCP spreads; CAPM3 picks a BareMetalHost labelled `infrastructure.cluster.x-k8s.io/failure-domain=<name>` | MachineDeployment `failureDomain` | CAPM3 label sync from BareMetalHost labels |
| **CAPV** (vSphere), with `spec.failureDomains` | Yes. `VSphereFailureDomain` + `VSphereDeploymentZone` per domain; KCP spreads | MachineDeployment `failureDomain` | vSphere CPI, from `k8s-region`/`k8s-zone` tags |
| **CAPV**, one compute cluster, no domains | Host spread inside the cluster through CAPV's cluster modules (DRS VM-VM anti-affinity), on for every vSphere site | Same, per MachineDeployment | none (one zone) |
| **CAPMOX** (Proxmox VE) `v0.9.2` | **Not in `r1`**; reported by `validate` and `dciab status` | Per-MachineDeployment `allowedNodes` | kubelet `--node-labels` |

**Metal3.** CAPM3 `v1.14.2` (pinned) has `failureDomains` on `Metal3Cluster` and `failureDomain` on `Metal3Machine`. DCiaB renders one entry per domain that has hosts, with `controlPlane: true`, and puts two labels on each BareMetalHost: the CAPM3 failure-domain label and `topology.kubernetes.io/zone`. If no host is free in the chosen domain, CAPM3 takes one from another domain, so `validate` checks there are enough hosts per domain (decision 6). The existing `hostSelector` (`charts/capi-clusterclasses/classes/r1/metal3.yaml:477-480`) still selects the host role; the failure domain narrows it.

**vSphere.** Two modes:

- *Several zones* (compute clusters, or DRS host groups inside one cluster). Each `vsphere` entry renders a `VSphereFailureDomain` (region type `ComputeCluster`, zone type `HostGroup`; or `Datacenter`/`ComputeCluster` for one cluster per zone) and a `VSphereDeploymentZone` with `controlPlane: true`. The `VSphereClusterTemplate` gets `failureDomainSelector: {}`, so the control plane uses every zone. CAPV no longer creates the vSphere side (`autoConfigure` is gone), so the site creates the tag categories, tags and DRS groups first. `validate --live` checks they exist. Today the schema requires `vsphere.cluster` but `values.py` never passes it on (`lib/dciab_cluster/values.py:145-148`); in this mode it becomes the default compute cluster.
- *Cluster modules, on every vSphere site.* DCiaB enables CAPV's `NodeAntiAffinity` feature gate (`EXP_NODE_ANTI_AFFINITY`, off by default upstream, set to `true` in `bundle.lock`'s provider variables). CAPV then puts the control plane and each MachineDeployment in their own cluster module, and DRS keeps each group's VMs on separate hosts. In one compute cluster this is the only host spread; with zones it keeps VMs apart inside each zone too. It needs DRS. A site without DRS turns it off explicitly with `platform.vsphere.clusterModules: false`, which sets `disableClusterModule: true` on the `VSphereCluster`, and `validate` then warns that nothing keeps the VMs apart.

**Proxmox.** CAPMOX `v0.9.2` (pinned) has "zones", but they are IP pools per zone, not Cluster API failure domains: `ProxmoxCluster.status.failureDomains` is never set, and `ProxmoxMachine` has no `failureDomain`. Upstream issue #717 proposes the contract (a zone lists its PVE nodes, and the machine controller limits `allowedNodes` and the IP pool to the machine's failure domain), with PR #720 linked to it. Status as of 2026-11-22, unverified: #717 open, #720 not known to be merged; no CAPMOX release with the contract is known. Until one ships:

- **Workers** are placed per MachineDeployment. `ProxmoxMachine` has its own `allowedNodes`, which overrides the cluster's. The class sets it from a per-MachineDeployment `failureDomain` variable to that domain's PVE node, and adds `topology.kubernetes.io/zone=<domain>` to the kubelet's `--node-labels`. The kubelet may set this label on its own Node.
- **The control plane ships unspread in `r1`.** KubeadmControlPlane uses one machine template, and CAPMOX places each VM on any allowed node by free memory. Three control plane VMs may share a host. DCiaB doesn't work around the upstream gap; it reports it. `validate` warns on every Proxmox site with more than one domain, and `dciab status` lists each quorum member's PVE node and warns when two share one. Control plane spread on Proxmox comes in `r2`, once a CAPMOX release with failure domains is pinned.
- **Shared storage is required for any spread.** With node-local storage, the template exists on one node and a clone can't go anywhere else. Spreading needs a clustered Proxmox with shared storage (Ceph RBD, or LVM over an iSCSI or FC SAN), which is a Phase 2 gate (decision 7).

### 3. One topology label, set from the start

Every node carries `topology.kubernetes.io/zone=<failure domain name>`. Nothing in DCiaB uses another key. The label must be there before anything schedules on the node, because Longhorn and the Kubernetes scheduler place replicas from it.

| Platform | Control plane nodes | Worker nodes |
| --- | --- | --- |
| Metal3 | CAPM3 label sync (`metal3.io/metal3-label-sync-prefixes: topology.kubernetes.io` on the `Metal3Cluster`) | same |
| vSphere, zones | vSphere CPI (`labels: {region: k8s-region, zone: k8s-zone}` in its config) | same |
| vSphere, one cluster | not set | not set |
| Proxmox, one domain | kubelet `--node-labels`, the same value for every node (the one PVE node) | same |
| Proxmox, several domains | not set in `r1` (decision 2) | kubelet `--node-labels`, from the MachineDeployment's `failureDomain` variable |

Core Cluster API syncs only `node-role.kubernetes.io`, `node-restriction.kubernetes.io` and `node.cluster.x-k8s.io` labels from Machines to Nodes, and sets no zone label itself. When Proxmox gets control plane failure domains, the control plane nodes still need a source. DCiaB waits for an upstream answer (CAPMOX or Cluster API) and revisits it with `r2`; it doesn't write its own label controller.

On Proxmox the label does two jobs. The Proxmox CSI driver requires `topology.kubernetes.io/region` (its configured region, here the site name) and `topology.kubernetes.io/zone` set to the PVE node name. The zone is required for volumes on local storage and optional on shared storage. One PVE node per domain, named after the node, keeps DCiaB's meaning and the driver's the same. On a single-domain site every node gets the one node's name. On a multi-domain site the unlabelled control plane nodes can't attach local-storage volumes, which is one more reason spreading needs shared storage.

### 4. How stateful services use the label

The services keep their own replicas apart by zone, and by host inside a zone. ADR 0008 puts platform data that keeps its own copies on `local-retain` (scratch data on `local`). On those classes, their spread is the only thing between one host failure and data loss.

| Service | Members | Rule |
| --- | --- | --- |
| etcd (every cluster) | 3 (or 5) control plane machines | Decision 2. KCP spreads where the provider reports failure domains |
| Longhorn (`replicated` and `replicated-retain`, on local disks only, per [ADR 0008](0008-provider-pattern.md)) | `replicaCount` | `replicaSoftAntiAffinity: false` (never two replicas on one node, Longhorn's default, now set explicitly). `replicaZoneSoftAntiAffinity: false` when the cluster has at least `replicaCount` zones with Longhorn nodes, `true` otherwise, so a volume can still become healthy. `replicaAutoBalance: best-effort`, so replicas move back after a zone returns |
| `local` and `local-retain` (as ADR 0008 maps them per provider) | 1 copy | The service's own replicas are what is spread |
| CloudNativePG | 3 instances | Keep its default pod anti-affinity on `kubernetes.io/hostname`, set `podAntiAffinityType: required`, and add a topology spread constraint on `topology.kubernetes.io/zone` (`maxSkew: 1`; `DoNotSchedule` with at least 3 zones, `ScheduleAnyway` below) |
| OpenBao (Raft) | 3 voters (5 at large sites) | Required pod anti-affinity on hostname, and the same zone spread constraint. OpenBao's autopilot has no redundancy zones (a Vault Enterprise feature), so placement is the only protection |
| SeaweedFS | 3 masters, N volume servers | Masters: same as OpenBao. Volume servers start with `-dataCenter=<site>` and `-rack=<zone>`, read from their node's label. Replication follows [ADR 0009](0009-backup-and-dr.md): `010` (a copy in another rack, here another zone) with 2 or more failure domains, `001` (another server) with one |
| OpenSearch | 3 cluster-manager nodes, data nodes | The same zone spread; shard allocation awareness on the zone attribute |

**When the site's storage provides redundancy** (vSphere CSI, the Proxmox CSI driver on shared storage, ceph-csi or a vendor SAN driver under ADR 0008), the storage keeps the disk safe but not the VM. A dead hypervisor still stops every pod on it until its VMs restart elsewhere, so the services' own replicas still spread across zones and hosts, as in the table. What changes is the storage layer:

- No Longhorn, so no replica rules. The array's own layout decides where the copies are, and it is the site's.
- StorageClasses use `volumeBindingMode: WaitForFirstConsumer`, so a volume is created after the scheduler has picked a zone for the pod.
- With vSphere zones, the CSI driver needs topology too (`useTopologyCategories` with the same `k8s-region`/`k8s-zone` tags), so a volume lands on a datastore its zone can reach. A datastore shared by every zone needs nothing.
- With the Proxmox CSI driver, a volume on shared storage (Ceph RBD, LVM over a SAN) follows its pod to any node. A volume on a node's local storage stays on that PVE node, so its pod can run only in that zone; the service's own replicas in other zones carry it through a host failure.

### 5. Minimum hosts per tier

These are the numbers that survive the loss of one failure domain. "Domains" means distinct failure domains the members can use.

| Tier | Members | Domains to survive one loss | Below that |
| --- | --- | --- | --- |
| MGMT control plane (etcd; Longhorn and controllers when it has no workers) | 3 | 3 | error without `notHighlyAvailable`, then a warning |
| INFRA control plane | 3 | 3 | same |
| INFRA workers (Longhorn, CloudNativePG, OpenBao, SeaweedFS, OpenSearch) | at least 3 | 3 | same |
| Workload cluster with 3 control plane nodes | 3 | 3 | warning (it may use fewer domains than the site has) |
| Workload cluster with 1 control plane node | 1 | 1 | the existing warning |

- **Two domains are a trap.** Three members over two domains put two in one of them; losing that one loses quorum. Two-host sites are allowed but aren't highly available: they protect workers and Longhorn replicas, not etcd. They need the same acknowledgement as a single host (decision 1).
- **Capacity is N+1.** The VMs of the failed domain must fit on the others, or the cluster stays degraded. `validate --live` adds up each tier's VM memory and warns when the remaining hypervisors can't hold it after the largest domain fails.
- **A full HA site needs at least three hypervisor hosts** (or three bare-metal hosts per control plane), each its own domain, with shared storage on Proxmox.

### 6. What `dciab validate` checks

Offline:

- **Errors** (the config can't be right):
  - a failure domain names a Proxmox node not in `platform.proxmox.nodes`, a node appears in two domains, or two domains share a name;
  - a cluster, worker pool or BareMetalHost names a domain that isn't declared;
  - a generated MachineDeployment name (`<pool>-<domain>`) breaks the name rules;
  - `spec.failureDomains` missing, or fewer than three domains without `notHighlyAvailable.reason` (decision 1);
  - a Proxmox domain whose name isn't its PVE node's name;
  - `spec.hypervisors` installs Proxmox (so DCiaB knows the storage is a fresh install's local storage: `local`, `local-lvm` or `local-zfs`) and `platform.proxmox.nodes` lists more than the template node. The clones can't go there.
- **Warnings** (it works, but a single failure hurts more than it looks):
  - control plane replicas greater than the domains the control plane can use (for example 3 control plane nodes on fewer than 3 domains), named with what the loss of the busiest domain would do;
  - exactly two domains, for any cluster with 3 control plane nodes;
  - Longhorn `replicaCount` greater than the zones with Longhorn nodes;
  - a Metal3 domain with fewer free hosts than the machines KCP or a MachineDeployment will put there;
  - `notHighlyAvailable` set: one warning on every run, naming what losing a host stops;
  - Proxmox with more than one domain: the control plane isn't spread in `r1` (decision 2);
  - `platform.vsphere.clusterModules: false`: nothing keeps the VMs apart inside a compute cluster;
  - `spec.time.serveLocalClock: true`, which today is only a note: the site then has one time source (decision 9).

`--live`:

- **Errors:** a node in `platform.proxmox.nodes` that the API at `platform.proxmox.url` doesn't know (an unclustered host); node-local storage (the storage's `shared` flag is off) with more than one allowed node.
- **Warnings:** the N+1 capacity check (decision 5); vSphere tag categories, tags or DRS groups missing for a declared domain; no DRS on the compute cluster when cluster modules are used.

After the build, `dciab status` shows each quorum member's host and warns when two share one.

The existing `check_resilience` (`lib/dciab_validate/checks.py:540-548`) becomes part of this check. Its "unless the datastore underneath already replicates" hint goes away: ADR 0008's double-replication rule covers that case.

### 7. The single-host limitation in v1

**Stated plainly:** in v1, a Proxmox site installed from bare servers by `dciab hypervisors` is one failure domain. The hosts aren't clustered, CAPMOX talks to one host's API, and local storage can't clone to another node. Every MGMT, INFRA and workload VM runs on one host. If that host fails, the whole platform stops, and its data survives only in [ADR 0009](0009-backup-and-dr.md)'s backups on the site and off-site targets.

How it is surfaced:

- The config must say so: `failureDomains.notHighlyAvailable.reason`, or `validate` errors. With it, `validate` prints one warning on every run: "this site is one failure domain: losing <host> stops MGMT, INFRA and every workload cluster; restore is from backups (ADR 0009)". A second hypervisor that `spec.hypervisors` installs but CAPMOX can't use is an error (decision 6).
- The product docs say it where an operator chooses the path: `README.md`, `docs/HYPERVISORS.md` and `docs/PROXMOX.md` get a "single failure domain" note, and `examples/proxmox-bare.yaml` lists only `pve01` and says the storage is local to one host (unless an earlier PR has already done so).
- It ends when Proxmox clustering with shared storage (Ceph, or a SAN) is built. That is now part of Phase 2 and its gate in [ROADMAP.md](../ROADMAP.md). Clustering also gives CAPMOX one API for all nodes; `platform.proxmox.url` should then point at a name or address that more than one node answers, not one host.

### 8. Network: MTU, bonds and separate VLANs

| Field | Where it goes | When |
| --- | --- | --- |
| `mtu` on `network.nodes` and on each `baremetal.networks` entry (optional, default 1500) | Proxmox and vSphere machine NICs (both providers have an `mtu` field), Metal3 `networkData`. Cilium reads the MTU from the node's interface | **v1.** The VM classes need a variable in `r1` |
| `bond` on a BareMetalHost: `mode` (`802.3ad` or `active-backup`) and the member NICs' MACs | Metal3 `networkData` `bonds` (CAPM3 has `bondMode` and `bondLinks`); the node IP moves to the bond | **v1.** Lives in `capi-cluster`'s `Metal3DataTemplate`, not in a class |
| `tagged: true` on a `baremetal.networks` entry | A `vlans` link (`vlanID`, `vlanLink`) on the NIC or bond, for trunk ports | **v1**, same place |
| Bonds and VLANs on hypervisors | `dciab hypervisors` answer files and bridge setup | Roadmap, with the existing "tagged management VLAN, bridge" item |
| Separate node networks per cluster for VM clusters (`network: <name>`, as Metal3 already has) | Each cluster's port group, or bridge and VLAN | With workload clusters (Phase 4). The bridge and port group are already per-cluster class variables, so no class change |
| `network.storage`: one or more storage networks (decision 8a) | One extra NIC per storage network on every node that needs it | **Schema and class support in `r1`**, used only when configured |
| Longhorn's own replication on a storage network | Longhorn's storage network setting needs Multus | Roadmap |
| BMC traffic | Already separate: `baremetal.bmcNetworks`, routed | No change |

The reference VLAN plan the docs should show: out-of-band (BMCs), hypervisor management, platform nodes (MGMT and INFRA), tenant nodes, and one or more storage networks. In v1 out-of-band, hypervisor management and storage can be separate, and the platform shares one node network; tenant separation arrives with workload clusters.

#### 8a. Storage networks

Multipathing (iSCSI with dm-multipath, NVMe-oF with native multipath) needs independent paths: separate NICs on separate subnets, usually two fabrics, not a bond. So `network.storage` is a list, and a node gets one extra NIC per entry:

```yaml
network:
  storage:                       # optional; nothing changes when it's absent
    - name: san-a
      cidr: 10.40.10.0/24        # no gateway: storage traffic stays on the segment
      vlan: 410
      mtu: 9000
    - name: san-b
      cidr: 10.40.20.0/24
      vlan: 420
      mtu: 9000
```

- Each cluster that uses them gets an address range per storage network (`clusters.<name>.storageIPRanges`), allocated like node addresses (in-cluster IPAM on VMs, Metal3 IPAM on bare metal).
- On VMs, the extra NICs are CAPMOX `additionalDevices` (bridge and VLAN per network) or extra CAPV devices (a port group per network). The classes take one `storageNetworks` list variable in `r1`, empty by default, so adding networks later needs no new revision.
- On bare metal, each host names the NIC per storage network by MAC (`storageNICs: {san-a: <mac>, san-b: <mac>}`), rendered into `networkData` by `capi-cluster`.
- They matter for initiators inside the node: vendor SAN drivers and ceph-csi. vSphere CSI and the Proxmox CSI driver attach through the hypervisor, so their storage traffic uses the hypervisor's own storage network, which is the site's.
- The node image must carry the initiators' multipath configuration (`iscsid` and `multipathd`, `nvme-tcp`); vendor drivers' requirements are checked when each is pinned (ADR 0008).

### 9. Time: `spec.time`, production NTP and the pivot

Time settings move to their own block now, while the schema is `v1alpha1`. `spec.time` replaces `spec.bootstrap.ntp` and `network.ntpServers` ([ADR 0001](0001-day0-hypervisors-and-services.md), decision 6):

```yaml
spec:
  time:
    upstream: [ntp1.example.net, ntp2.example.net]   # the site's sources; [] on an isolated site
    serveLocalClock: false        # true only with no upstream: the site's time is then DCiaB's clock
    servedBy: dciab               # dciab: DCiaB serves the node-facing addresses; site: the site does
    servers: [10.30.110.53, 10.30.110.54]            # what nodes, hypervisors and BMCs use
```

- **`servedBy: site`** is today's "the site has its own NTP": `servers` are the site's, and DCiaB runs no NTP service.
- **`servedBy: dciab`** is today's `spec.bootstrap.ntp`: the bootstrap host serves `servers` on Day 0, and the platform takes over when it takes over DNS. `servers` defaults to the DNS service addresses.

**Production NTP.** Two chrony Deployments on MGMT, one replica each, in different failure domains, each answering on one of the two `servers` addresses, UDP 123. Not a host-network DaemonSet: the node's own `chronyd` holds UDP 123, the same conflict `dciab services` works around on Day 0.

- **Upstreams.** `time.upstream`. The two servers also peer with each other. With no upstream (`serveLocalClock: true`) both run `local stratum 10 orphan`, so they agree with each other rather than drift apart. The docs recommend at least two hardware or GPS-backed sources for isolated sites.
- **Handoff without a step.** Before the addresses move, MGMT's chrony syncs from the bootstrap host's own address (`bootstrap.address`), so the time on both sides is the same when clients switch. That upstream is removed at `finalize`.
- **Shared addresses with DNS: a spike decides, before `dciab mgmt` takes over DNS.** The candidate is a Cilium LoadBalancer Service per address that shares it with the DNS Service (Cilium LB IPAM's sharing key), with `externalTrafficPolicy: Local` to keep client addresses for chrony's `allow` list. It breaks if the L2 or BGP announcement goes to a node with no local chrony or DNS endpoint. If the spike shows shared addresses don't hold up, NTP gets its own two addresses: `time.servers` is set to them, apart from `dnsServiceIPs`, and the bootstrap host serves NTP there on Day 0 too, so nothing is reconfigured at the handoff.
- **Pivot to the site's time service**, in two stages. First, DCiaB's chrony takes the site's servers as its only upstream: nodes keep their addresses and nothing rolls. Later, if the site wants clients on its servers directly, `servedBy: site` with the site's `servers`; that changes the `ntpServers` class variable, which rolls cluster nodes, and hypervisors and BMCs are repointed by the same code that set them on Day 0.
- COMPONENTS.md gains a "Time" row: chrony, MGMT, Planned. It lands with the DNS handoff in `dciab mgmt`.

### 10. What lands in `r1`, and what can wait

`r1` is a draft until the first release. Anything a class must carry goes in now, or waits for `r2` and rolls every cluster that moves to it.

**In `r1`, before the first release:**

- Proxmox: a `failureDomain` variable, overridable per MachineDeployment, that sets the worker `ProxmoxMachineTemplate`'s `allowedNodes` and adds `topology.kubernetes.io/zone` to the worker kubelet's `--node-labels`.
- vSphere: a variable that sets `failureDomainSelector: {}` on the `VSphereClusterTemplate` when the site declares zones.
- Metal3: a variable that fills `Metal3ClusterTemplate`'s `failureDomains`, and the label-sync annotation on the `Metal3Cluster`. Whether the class can set that annotation through the template's metadata, or DCiaB sets it after the cluster exists, is checked in the implementing PR.
- Proxmox: a cluster-level zone label (for single-domain sites) and the region label on every node, through the kubelet's `--node-labels`.
- vSphere: a variable for `disableClusterModule`, set only when `platform.vsphere.clusterModules: false`.
- Proxmox and vSphere: an optional `mtu` variable on the machine NIC, and a `storageNetworks` list variable that adds one NIC per storage network (decision 8a), empty by default.
- MachineHealthCheck for the control plane and every worker pool (amended; see below).

**Outside the classes, any time:** the schema and `validate` (decisions 1, 6, 7); `values.py` and `capi-cluster` (per-domain MachineDeployments with `failureDomain`, BareMetalHost labels, `VSphereFailureDomain` and `VSphereDeploymentZone` objects, Metal3 bonds, VLANs and MTU); `EXP_NODE_ANTI_AFFINITY` in `bundle.lock`; storage network addresses and Metal3 storage NICs; the Longhorn settings in `addons/longhorn/values.yaml`; the service charts' spread rules; `spec.time` and production NTP; the docs and the example.

**`r2` or later:** Proxmox control plane spread, control plane zone labels and domains of several PVE nodes, once a CAPMOX release with failure domains is pinned.

**Amended: MachineHealthCheck is in `r1`.** It was planned for `r2`, to come with failure domains so a replacement doesn't land beside the survivor. Failure domains land in `r1` for vSphere and Metal3, so a replacement goes to a domain KubeadmControlPlane and the MachineDeployments choose. On Proxmox a replacement control plane VM may land on the same host as a survivor until CAPMOX spreads the control plane; `validate` and `dciab status` report it (decision 6). Remediation replaces one machine at a time (`unhealthyLessThanOrEqualTo: 1`) and retries a failing control plane machine at most three times, so a site-wide problem never turns into mass replacement.

## Alternatives considered

- **Leave placement to the hypervisor.** vSphere DRS rules or Proxmox HA rules alone can keep VMs apart, but Kubernetes, Longhorn and the operators never learn the topology, so replicas can still pile into one zone. Proxmox's HA rules also need a clustered Proxmox with shared storage, which v1 doesn't have. Kept only as CAPV's cluster modules, where CAPV manages the rules itself.
- **Pod anti-affinity on hostname only.** Cheap, and CloudNativePG does it by default. Three VMs on one host are three hostnames, so it protects nothing on its own. Rejected as the only rule.
- **Reject single- and two-domain sites.** Safer, but it removes the bare-Proxmox path that small and edge sites depend on. Rejected: they need an explicit acknowledgement with a reason, and the docs say they aren't highly available.
- **A warning only for single-domain sites.** Easy to miss on a long `validate` run. Rejected for the acknowledgement above.
- **Wait for CAPMOX failure domains before spreading anything on Proxmox.** Workers can be placed today with per-machine `allowedNodes`. Rejected for workers; accepted for the control plane.
- **Help land CAPMOX's failure-domain work and pin a pre-release build in `r1`.** It would spread the Proxmox control plane sooner, but ties the first release to an unreleased provider. Rejected; `r2` picks it up.
- **A DCiaB controller that copies failure domains to Proxmox control plane Node labels.** Rejected; DCiaB waits for upstream and revisits with `r2`.
- **A DCiaB controller that moves Proxmox control plane VMs apart.** It would fight CAPMOX's placement and duplicate the upstream proposal. Rejected.
- **Hard zone anti-affinity in Longhorn always.** With fewer zones than replicas, volumes never become healthy. Rejected; it is hard only when there are enough zones.
- **Separate VLANs for every tier in v1.** Tenant node networks come with workload clusters and need no class change; Longhorn's storage network needs Multus and is deferred. Storage networks for initiators are in `r1`.
- **One second NIC for storage.** Simpler, but it can't carry two independent paths for iSCSI or NVMe-oF multipathing. Rejected for a list of storage networks.
- **CAPV cluster modules only when the config asks.** Rejected: most sites have DRS, so the safe setting is the default and a site without DRS turns it off.
- **NTP on the hypervisors.** They are NTP clients of the same addresses, ESXi isn't meant to serve time, and the hypervisors aren't DCiaB's to configure after the install. Rejected.

## Consequences

- The schema gains `spec.failureDomains` (required, with `notHighlyAvailable` below three domains), `baremetal.hosts[].failureDomain`, `bond`, `tagged`, `mtu`, `network.storage`, `storageIPRanges`, `storageNICs`, `platform.vsphere.clusterModules` and `spec.time`. `spec.bootstrap.ntp` and `network.ntpServers` move into `spec.time`, so ADR 0001's decision 6 is amended and every example changes.
- A worker pool becomes one MachineDeployment per failure domain. Pool names, replica counts and `dciab status` output change to match.
- vSphere sites with zones must prepare tags, tag categories and DRS groups before `dciab mgmt`. The docs list them, and `validate --live` checks them.
- Proxmox control plane quorum stays unprotected until CAPMOX ships failure domains and DCiaB pins it in a new class revision. `dciab status` makes it visible.
- `examples/proxmox-bare.yaml` lists one node and acknowledges a single domain, or the new `validate` errors fail it. `examples/proxmox.yaml` already changes under ADR 0008.
- Every vSphere site needs DRS, or turns cluster modules off.
- MGMT gains an NTP service that must be up before the DNS addresses move. The DNS handoff test covers NTP too. The shared-address spike runs before `dciab mgmt` takes over DNS (decision 9).
- Phase 2's gate now includes Proxmox clustering with shared storage, which ends the single-host limitation ([ROADMAP.md](../ROADMAP.md)).

## Resolved questions

Decided on 2026-11-23.

1. **Proxmox control plane before CAPMOX failure domains:** ship `r1` unspread on Proxmox, reported by `validate` and `dciab status`; spread in `r2`. The first release shouldn't depend on an unreleased provider.
2. **Single-domain sites:** explicit in the config with a reason (`failureDomains.notHighlyAvailable.reason`). A warning alone is too easy to miss.
3. **Zone labels for Proxmox control plane nodes:** wait for upstream and revisit with `r2`. A DCiaB controller would duplicate work CAPMOX or Cluster API is likely to do.
4. **Two-host sites:** allowed but not HA, with the same acknowledgement as one host. Some sites have two servers; the docs say plainly what they get.
5. **CAPV cluster modules:** on for every vSphere site; sites without DRS turn them off explicitly. The safe setting should be the default.
6. **Where time settings live:** in `spec.time` now. Upstreams, who serves time and the pivot to the site's service belong together, and `v1alpha1` can still change.
7. **Storage network:** the optional schema and class support are in `r1`, with one or more extra NICs. iSCSI and NVMe-oF multipathing need independent paths, and adding it later would need a new class revision.
8. **Proxmox clustering with shared storage:** a Phase 2 gate, now in ROADMAP.md. It ends the single-host limitation.
9. **NTP and DNS on shared addresses:** run the spike before `dciab mgmt` takes over DNS; if it fails, NTP gets its own addresses. `time.servers` can already hold them.

## References

- CAPMOX `v0.9.2` components (zones as IP pools, `allowedNodes` and `additionalDevices` on `ProxmoxMachine`, clone `target` needs shared storage): https://github.com/ionos-cloud/cluster-api-provider-proxmox/releases/download/v0.9.2/infrastructure-components.yaml
- CAPMOX issue #717, "implement CAPI failure domain contract for v1alpha2 zones" (status as of 2026-11-22, unverified): https://github.com/ionos-cloud/cluster-api-provider-proxmox/issues/717
- CAPMOX PR #720, linked to #717 (status as of 2026-11-22, unverified): https://github.com/ionos-cloud/cluster-api-provider-proxmox/pull/720
- Cluster API failure domains (introduced in v1alpha3): https://kubernetes.io/blog/2020/04/21/cluster-api-v1alpha3-delivers-new-features-and-an-improved-user-experience/
- Cluster API metadata propagation (labels synced from Machines to Nodes): https://main.cluster-api.sigs.k8s.io/reference/api/metadata-propagation
- CAPM3 failure domains: https://book.metal3.io/capm3/failure_domain
- CAPM3 label synchronization: https://book.metal3.io/capm3/label_sync
- CAPM3 `v1.14.2` components (`failureDomains`, `bondMode`, `bondLinks`, `vlanID`, `mtu`): https://github.com/metal3-io/cluster-api-provider-metal3/releases/download/v1.14.2/infrastructure-components.yaml
- CAPV `v1.17.0` components (`VSphereFailureDomain`, `VSphereDeploymentZone`, `failureDomainSelector`, `NodeAntiAffinity` gate): https://github.com/kubernetes-sigs/cluster-api-provider-vsphere/releases/download/v1.17.0/infrastructure-components.yaml
- vSphere multi-AZ with CAPV (zone types, tag categories, `autoConfigure` removed, CSI topology; docs.vmware.com is being retired, so the CAPV CRDs above are the lasting source): https://docs.vmware.com/en/VMware-Tanzu-Kubernetes-Grid/2.5/using-tkg/workload-clusters-multi-az-vsphere.html
- Node anti-affinity with vSphere cluster modules: https://apps-cloudmgmt.techzone.vmware.com/blog/tkg-node-anti-affinity
- vSphere CPI with zones (`labels` region and zone): https://cloud-provider-vsphere.sigs.k8s.io/tutorials/deploying_cpi_with_multi_dc_vc_aka_zones
- Labels the kubelet may set on its own Node (includes `topology.kubernetes.io/zone`): https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kubelet/pkg/apis/well_known_labels.go
- Pod topology spread constraints: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints
- Longhorn replica scheduling (node, zone and disk anti-affinity; zone from `topology.kubernetes.io/zone`): https://longhorn.io/docs/1.13.0/nodes-and-volumes/nodes/scheduling
- CloudNativePG scheduling (anti-affinity defaults): https://cloudnative-pg.io/docs/1.28/scheduling
- OpenBao autopilot (no redundancy zones): https://openbao.org/docs/concepts/integrated-storage/autopilot/
- Vault redundancy zones (Enterprise only, for contrast): https://developer.hashicorp.com/vault/docs/v1.21.x/enterprise/redundancy-zones
- SeaweedFS replication (`xyz` placement, `-dataCenter` and `-rack`): https://github.com/seaweedfs/seaweedfs/wiki/Replication
- Proxmox CSI plugin (region and zone labels; zone is the PVE node; local versus shared storage): https://pkg.go.dev/github.com/sergelogvinov/proxmox-csi-plugin
