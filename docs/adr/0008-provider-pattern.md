# ADR 0008: Providers — one pattern for identity, storage, DNS, IPAM and secrets

- Status: proposed
- Date: 2026-11-22

## Context

DCiaB is written as if it owns every core service. Many sites already have some of them: an identity provider, a storage array, DNS, an IPAM, a secret store. Today the site config can't say so:

- `spec.storage` requires `longhorn` (`schema/dciab.schema.json:326`). A site with vSphere CSI, Ceph or a SAN must run Longhorn on top of it. On a replicated datastore that is replication on top of replication: Ceph 3× under Longhorn 2× keeps six copies, and every write pays for both.
- `spec.identity` has only `adminGroup` and `initialAdmins`. There is no way to name a site IdP, or to skip Authentik.
- The Authentik issuer for workload clusters is hard-coded in `lib/dciab_cluster/values.py:107-110` as `https://auth.<network.domain>/application/o/kubernetes/`.
- DNS is always DCiaB's (Day 0 CoreDNS, then PowerDNS on MGMT), IPAM is always the config's static ranges, and secrets are always OpenBao ([ADR 0005](0005-secrets.md)). No external-dns is planned.
- The object store (SeaweedFS) and COSI are proposed, but no consumer is wired to them, and nothing lets a site use its own S3 except Longhorn's `backupTarget`.

One part of the config already does this well. `spec.pki.mode` is `generate-root` or `import-intermediate`: DCiaB builds the thing, or takes the site's. The schema requires the fields each mode needs and forbids the others, and `dciab validate` checks what the schema can't (path length, expiry). This ADR copies that model to every core service.

[ADR 0004](0004-identity-and-sso.md) and [ADR 0005](0005-secrets.md) describe the built-in providers (Authentik, OpenBao). This ADR adds the choice between built-in and site-provided, and how each consumer wires to whichever is chosen. Backup and DR are in ADR 0009; failure domains are in ADR 0010.

## Decisions

### 1. One shape for every core service

Each core service is a block under `spec` with a `provider` key. Each provider has its own sub-block, named after it. The schema requires the sub-block of the chosen provider and forbids the others, as `spec.pki` does.

| Service | Built-in provider | Site-provided providers | Default |
| --- | --- | --- | --- |
| Identity (`spec.identity`) | `authentik` | `external` (OIDC, plus LDAP for hosts) | `authentik` |
| Block storage (`spec.storage.block`) | `longhorn`, `openebs` | `vsphere-csi`, `ceph-csi`, `vendor-csi`, `existing` | by platform (decision 4) |
| Object storage (`spec.storage.object`) | `seaweedfs` | `rook-ceph`, `external` | `seaweedfs` |
| DNS (`spec.dns`) | `powerdns` | `external` | `powerdns` |
| IPAM (`spec.ipam`) | `in-cluster` | `netbox` (DCiaB's or the site's) | `in-cluster` |
| Secrets (`spec.secrets`) | `openbao` | `external` | `openbao` |
| PKI (`spec.pki`, unchanged) | `generate-root` | `import-intermediate` | none; required |

`spec.pki` keeps its `mode` key; renaming it buys nothing. Every new block uses `provider`.

The built-in provider is always the default, so a config that names none gets today's behaviour.

### 2. Tiers can override, but only downwards

A site runs three kinds of cluster: MGMT, INFRA and workload clusters. Each service block takes an `overrides` map keyed by tier (`mgmt`, `infra`, `workload`). An override is merged over the block for that tier's clusters.

One rule limits overrides. The tiers are created in order: site → bootstrap → MGMT → INFRA → workload. **A tier may use providers that run on itself, on the site, or on an earlier tier. Its creation and its recovery may depend only on the site and earlier tiers.** A tier never depends on a later one. So:

- MGMT must not depend on anything that runs on INFRA: not SeaweedFS, not Authentik ([ADR 0004](0004-identity-and-sso.md), decision 4), not NetBox. Its block storage is Longhorn or a site array, and its backups go to the site target, never INFRA's SeaweedFS (ADR 0009).
- INFRA may use what it runs itself: its own block storage, and its own SeaweedFS for INFRA-local data and as the primary copy of its backups. That copy can't be the only one: restoring INFRA can't need INFRA, so the critical set is also copied to the off-site or site target (ADR 0009). OpenBao's snapshots skip SeaweedFS entirely and go to the site target, because restoring OpenBao comes before SeaweedFS can be trusted (ADR 0009, decision 4). INFRA can use MGMT's DNS, but not its own Authentik for its Kubernetes API (again ADR 0004).
- Workload clusters can use anything.

`dciab validate` rejects a config that breaks this rule (decision 10).

### 3. Identity: Authentik, or the site's IdP directly

**`provider: authentik`** (default) is [ADR 0004](0004-identity-and-sso.md) as written. A new optional `authentik.upstream` connects Authentik to the site's directory, so people log in with their site accounts and Authentik stays the one IdP every DCiaB service trusts:

- `type: ldap` (Active Directory or another LDAP directory): Authentik's LDAP source syncs users and groups. POSIX IDs pass through from the directory when it has them (ADR 0004, decision 2).
- `type: oidc` or `type: saml`: Authentik's federation source. Users and groups arrive at login.

**`provider: external`** skips Authentik. Services trust the site's OIDC issuer directly. The site has to supply:

- the issuer URL, the groups claim and the user name claim;
- one pre-created OIDC client per consumer, as a client ID and a secret reference. DCiaB can't create clients in an IdP it doesn't run;
- an LDAP endpoint (URL, bind DN and secret, user and group base DNs) for SSSD on hosts managed by OpenVox. Without it, hosts get only the break-glass user, and `validate` warns.

**Protocols.** OIDC is the one protocol DCiaB services consume. Every consumer in the matrix (decision 9) speaks OIDC, except hosts (SSSD reads LDAP) and vCenter (below). LDAP is served only for those. SAML is accepted only as an Authentik upstream, never as a consumer protocol: no DCiaB consumer needs it.

**The Kubernetes issuer comes from config.** `cluster-values` stops hard-coding the issuer. It reads one resolved value, `identity.kubernetes` (issuer URL, client ID, claims, CA bundle). With `authentik` it defaults to today's URL and client `kubernetes`, so nothing changes for existing configs. With `external` it uses the site's issuer and the client given for `kubernetes`.

**Clients are provisioned by blueprints first, then OpenTofu.** With `authentik`, the platform's own applications, providers and groups are Authentik blueprints. Authentik applies blueprint files from its blueprint directory when they appear or change, and rescans it on a schedule, each blueprint in one transaction. The platform step renders them from the site config into ConfigMaps mounted into the worker. They need nothing that isn't already running: not Terrakube, not Forgejo. That matters because those services need their own clients before they can start. Client secrets are generated into OpenBao and reach both Authentik and the consumer through ESO. After the platform step, the blueprints stay the source of truth for platform clients. Tenant applications and later additions go through the IaC path with the Authentik Terraform provider ([ADR 0006](0006-iac-path.md)), where OPA protects "Authentik core".

**Groups map to three platform roles.** ADR 0004 maps `adminGroup` to admin everywhere but leaves other groups to "added through Git". This ADR fixes the role set, so every consumer's mapping is rendered from one table instead of written by hand per service. `admin` is always `adminGroup`; the other two are optional:

```yaml
identity:
  roles:
    operator: dciab-operators  # optional
    viewer: dciab-viewers      # optional
```

| Consumer | `admin` | `operator` | `viewer` |
| --- | --- | --- | --- |
| Kubernetes (workload) | `cluster-admin` | `edit`, cluster-wide | `view` |
| Argo CD | `role:admin` | a project role with sync | `role:readonly` |
| Forgejo | site admin | organization owner of platform orgs | read on platform orgs |
| Grafana | Admin | Editor | Viewer |
| NetBox | superuser | a write group | a read group |
| OpenBao | admin policy | operator policy (platform KV write) | read policy |
| AWX | system administrator | organization admin | system auditor |
| Terrakube | admin team | a team with plan and apply | a read team |
| OpenSearch | `all_access` | a security-analyst role | `readall` |
| Proxmox VE | `Administrator` on `/` | `PVEVMAdmin` on DCiaB pools | `PVEAuditor` |
| vCenter | Administrator | a DCiaB operator role | Read-only |

Tenant groups are added on top of these, per tenant, through Git.

**vCenter is a gap in ADR 0004, and partly stays one.** vCenter's identity federation supports four providers: AD FS (vSphere 7.0 and later), Okta (8.0 Update 1), Microsoft Entra ID (8.0 Update 2) and PingFederate (8.0 Update 3). Authentik isn't one of them, and generic OIDC isn't documented. So:

- If the site's IdP is one of those four, vCenter federates to it directly. DCiaB documents it but doesn't configure it.
- Otherwise vCenter uses an LDAP identity source pointed at the Authentik LDAP outpost.
- vCenter usually belongs to the site, so DCiaB changes its identity source only when the config opts in (`identity.consumers` includes `vcenter`). CAPV and the vSphere CSI driver keep their own service accounts either way.

**Proxmox VE** has OpenID Connect realms. Syncing groups from a claim was proposed upstream in 2025 (`groups-claim`, `groups-autocreate`). The implementation checks which PVE version ships it; on older versions, role grants go to users, not groups. Like vCenter, Proxmox is configured only on opt-in.

### 4. Block storage: use the site's array when there is one

**The rule:** when the site provides replicated block storage (vSAN or any vSphere datastore, Ceph, a SAN), use it through its CSI driver. Don't run Longhorn on it. Run Longhorn (or OpenEBS) only on local disks. Never replicate twice.

The default provider follows from the platform:

| Platform and datastore | Default `block.provider` |
| --- | --- |
| vSphere (any datastore) | `vsphere-csi` |
| Proxmox VE with Ceph (RBD) storage | `ceph-csi`, against the Proxmox Ceph cluster |
| Proxmox VE with local storage (`local-lvm`, ZFS) | `longhorn`, on the worker data disk |
| Metal3 (bare metal) | `longhorn`, on the node's data disks |

The site can choose otherwise; the defaults just make the common case need no storage config.

**Providers:**

- **`longhorn`.** Today's provider. Keeps `replicaCount`, `backupTarget` and `backupCredentials`, and moves under `storage.block.longhorn`. Its data goes on the worker data disk (`dataDiskGiB`), not the root disk, which is a fix the review asked for and the implementation of this ADR must include. Replication across hosts depends on failure domains (ADR 0010).
- **`openebs`**, with `engine: mayastor | lvm`. OpenEBS is block storage: an alternative to Longhorn, not to SeaweedFS.
  - *Replicated PV Mayastor* replicates over NVMe-oF and is the fast option. It costs a lot per storage node: two CPU cores reserved for its I/O engine, at least 2 GiB of 2 MiB huge pages, the `nvme-tcp` kernel module, and at least three worker nodes. The node image and ClusterClass must reserve the huge pages, so it isn't usable until a class revision does.
  - *Local PV LVM* doesn't replicate. It carves volumes from an LVM volume group on each node's data disk, needs `lvm2` and `dm-snapshot` on the node, and supports snapshots. It suits services that replicate themselves (decision 5).
- **`vsphere-csi`.** The vSphere CSI driver, which provisions volumes through vCenter's Cloud Native Storage. Node VMs need hardware version 15 or later, VMware Tools, and `disk.EnableUUID` set. The driver is pinned in `bundle.lock` with the vSphere cloud provider (CPI) that CAPV clusters need anyway; the current release line supports Kubernetes 1.34 to 1.36. Only `vsphere`-class clusters can use it.
- **`ceph-csi`.** RBD (block) and optionally CephFS (shared files) against a site Ceph cluster, Proxmox's included: monitors, cluster ID, pool and a Ceph client key. DCiaB runs only the CSI driver; it doesn't run Ceph.
- **`vendor-csi`.** A SAN or NAS array through its vendor's CSI driver (NetApp Trident, Dell CSM, Pure, HPE and so on). iSCSI has no standard API for creating LUNs, so a generic iSCSI driver can only attach LUNs someone already made on the array. Dynamic provisioning, snapshots and resizing need the array's API, which only the vendor's driver has. democratic-csi covers ZFS-based appliances (TrueNAS, generic ZFS over SSH) and Synology, over iSCSI, NFS and SMB, not arbitrary arrays; it fits a site whose "SAN" is one of those. DCiaB installs the driver's Helm chart from the archive with the site's values. The chart and images must therefore be in `bundle.lock` (open question 3).
- **`existing`.** The cluster already has a CSI driver that DCiaB didn't install. DCiaB uses the named StorageClass and installs nothing.

Each block provider produces a **`replicated`** StorageClass, the cluster default. Stateful platform services also get a **`replicated-retain`** class (`reclaimPolicy: Retain`), so deleting a PVC by mistake doesn't delete the data.

### 5. A local class for services that replicate themselves

CloudNativePG, OpenSearch, OpenBao (Raft) and SeaweedFS volume servers keep their own copies. On a replicated class they double-replicate inside the cluster too. So each cluster may also have a **`local`** StorageClass:

- with `longhorn`: a Longhorn class with one replica and `dataLocality: strict-local`;
- with `openebs`: Local PV LVM;
- with an array (`vsphere-csi`, `ceph-csi`, `vendor-csi`): none by default. The array already replicates once, and that's the one copy beyond the application's that the rule allows. A site that wants local disks for these services adds OpenEBS Local PV LVM as a second provider (`storage.block.local`).

These services ask for `local` when it exists and fall back to `replicated-retain` when it doesn't. Spreading their replicas across failure domains is ADR 0010's job; without it, a local class puts all copies at risk on one host.

### 6. Object storage: SeaweedFS by default, the site's S3 when it has one

**`seaweedfs`** (default) stays as [COMPONENTS.md](../COMPONENTS.md) proposes, on INFRA. Apache 2.0, a small footprint, rack- and data-center-aware replication per volume, and rack-aware 10+4 erasure coding for warm volumes. Its volume servers use the `local` class (decision 5), never a replicated one. Replication and EC settings follow the failure domains (ADR 0010). It has a COSI driver.

**`rook-ceph`** is for a site that runs Rook-Ceph in its clusters, or wants one system for block and object. Ceph's RGW serves S3 and Rook ships a COSI driver. It is much heavier than SeaweedFS (monitors, managers, OSDs per disk, RGW), so DCiaB doesn't run Rook-Ceph just for object storage.

**`external`** is the site's S3. There is no generic COSI driver for an arbitrary S3 endpoint, so the site pre-creates one bucket per consumer and the config maps each consumer to its bucket and credentials. COSI is off.

**Not chosen:**

- **Garage** (AGPLv3) replicates in three zones and has no erasure coding, and the COSI project lists no driver for it. It is built for small, geo-distributed self-hosting. It would add a second object store without adding anything DCiaB needs.
- **MinIO.** The community repository was archived on 2026-04-25. Its README says it is no longer maintained, and that the community edition is distributed as source code only, with no new pre-built binaries. Its COSI driver is listed as deprecated. DCiaB doesn't ship it. A site that already runs MinIO (or its commercial successor) uses it as `external`.

**COSI** is used only with `seaweedfs` and `rook-ceph`. Every COSI driver is still `v1alpha1`, so the controller and driver are pinned together in `bundle.lock` and bucket requests are treated as alpha. Platform buckets on INFRA and workload clusters (CNPG backups, Terrakube state, OpenSearch snapshots, registry blobs) are each a `BucketClaim` against a platform `BucketClass`. OpenBao snapshots and everything of MGMT's are not: they go to the site target, with credentials applied directly from the config (ADR 0009). Which buckets must also leave the site is ADR 0009's decision.

**The site's S3 and ADR 0009's targets.** `storage.object.external` may be the same S3 service as ADR 0009's site target, with different buckets. INFRA's primary backup copy and the site target then share hardware, so the off-site copy is the one that protects the critical set.

### 7. DNS, IPAM and secrets

**DNS.**

- `powerdns` (default): PowerDNS on MGMT is authoritative for `network.domain`, and the site delegates that zone to it.
- `external`: the site's DNS stays authoritative, and DCiaB only writes records into it. Records are written by RFC 2136 dynamic updates with a TSIG key, or by an external-dns webhook provider for the site's DNS product (Infoblox, for example).

Either way, every cluster runs **external-dns** as a baseline add-on, with `txtOwnerId` set to the cluster name so clusters never fight over records. Gateway and LoadBalancer hostnames register themselves. PowerDNS and RFC 2136 are in-tree external-dns providers; Infoblox is a webhook provider that external-dns's maintainers haven't reviewed, so it's opt-in and pinned like any other component. Records external-dns can't create (each cluster's API VIP, the registry name before any cluster exists) are written by DCiaB itself at the step that creates them: Day 0 CoreDNS, then the DNS provider.

**IPAM.**

- `in-cluster` (default, built today): node and load balancer ranges come from the config and become CAPI in-cluster IPAM pools on MGMT.
- `netbox`: NetBox is the source of truth for the ranges, either DCiaB's NetBox on INFRA or a site NetBox (`netbox.url`). CAPI still allocates from in-cluster pools on MGMT. NetBox doesn't sit on MGMT's runtime path. The pools are written from NetBox's prefixes through the IaC path ([ADR 0006](0006-iac-path.md)), and allocations are recorded back in NetBox. A NetBox CAPI IPAM provider exists only as an early alpha, and a live call from MGMT to INFRA would break decision 2.

Day 0 always uses the config's ranges: NetBox doesn't exist yet. The config's ranges must sit inside the NetBox prefixes once NetBox takes over.

**Secrets.**

- `openbao` (default): [ADR 0005](0005-secrets.md) as written.
- `external`: a site **Vault-API-compatible** store (HashiCorp Vault or OpenBao) takes OpenBao's place. ADR 0005 depends on that API for more than reading secrets: Kubernetes and JWT auth mounts per cluster, the PKI engine for the cert-manager and SPIRE intermediates, and dynamic database credentials. The site gives the address, a CA bundle, a namespace or mount prefix DCiaB may own, and a token or auth role for the platform step. Bootstrap secrets move there instead (ADR 0005, decision 5). Unseal and recovery stay the site's.

Other secret managers that ESO supports (CyberArk Conjur and others) can be added as extra, read-only `SecretStore`s for tenants. They can't replace the platform store, because they don't provide the PKI and auth methods above.

### 8. The config shape

A sketch. Field names are settled in the implementation PR; the structure is the decision.

```yaml
spec:
  identity:
    provider: authentik                # authentik | external
    adminGroup: dciab-admins           # unchanged
    initialAdmins: [...]               # unchanged
    roles: {operator: dciab-operators, viewer: dciab-viewers}   # admin is always adminGroup
    consumers: [kubernetes, argocd, forgejo, grafana, netbox, openbao, awx, terrakube, opensearch, openvox]
                                       # add proxmox, vcenter to opt in
    authentik:
      upstream:                        # optional
        type: ldap                     # ldap | oidc | saml
        url: ldaps://ad.example.net
        bindDN: CN=dciab,OU=Service,DC=example,DC=net
        bindPassword: {file: ./secrets/ad-bind}
        userBaseDN: OU=People,DC=example,DC=net
        groupBaseDN: OU=Groups,DC=example,DC=net
        posixIDs: passthrough          # passthrough | generate
    # external:                        # instead of authentik
    #   issuer: https://sso.example.net/realms/dc
    #   claims: {username: preferred_username, groups: groups}
    #   caBundle: {file: ./secrets/sso-ca.pem}
    #   clients:
    #     kubernetes: {clientID: k8s}
    #     argocd: {clientID: argocd, clientSecret: {file: ./secrets/argocd-oidc}}
    #   ldap: {url: ldaps://..., bindDN: ..., bindPassword: {file: ...}, userBaseDN: ..., groupBaseDN: ...}

  storage:
    block:
      provider: vsphere-csi            # longhorn | openebs | vsphere-csi | ceph-csi | vendor-csi | existing
      vsphereCSI: {datastorePolicy: k8s-gold}
      overrides:
        workload: {provider: longhorn, longhorn: {replicaCount: 3, backupTarget: s3://lh@s3.example.net/dc1, backupCredentials: {file: ./secrets/lh-s3.env}}}
      local: {provider: openebs, openebs: {engine: lvm, volumeGroup: dciab-local}}   # optional second provider (decision 5)
    object:
      provider: seaweedfs              # seaweedfs | rook-ceph | external
      cosi: true                       # seaweedfs and rook-ceph only
      # external:
      #   endpoint: https://s3.example.net
      #   caBundle: {file: ./secrets/s3-ca.pem}
      #   buckets:
      #     cnpg: {bucket: dc1-cnpg, credentials: {file: ./secrets/s3-cnpg.env}}
      #     terrakube: {bucket: dc1-tfstate, credentials: {file: ./secrets/s3-tk.env}}

  dns:
    provider: powerdns                 # powerdns | external
    # external:
    #   method: rfc2136                # rfc2136 | webhook
    #   server: 10.0.0.53
    #   zone: dc1.example.net
    #   tsigKey: {file: ./secrets/tsig.key}

  ipam:
    provider: in-cluster               # in-cluster | netbox
    # netbox: {mode: platform}         # platform | site; site adds url, token, CA

  secrets:
    provider: openbao                  # openbao | external
    # external:
    #   address: https://vault.example.net:8200
    #   caBundle: {file: ./secrets/vault-ca.pem}
    #   mountPrefix: dciab/dc1
    #   bootstrapToken: {file: ./secrets/vault-bootstrap}

  pki: {mode: import-intermediate, ...}   # unchanged
```

**The old shape.** `spec.storage.longhorn` is accepted for one minor release as shorthand for `storage.block: {provider: longhorn, longhorn: {...}}`. `validate` warns and prints the new form. The examples move to the new form in the implementing PR.

### 9. What each consumer wires to

One row per consumer. The cells say what the consumer uses with the default providers. With a site provider, the same wiring points at the site's service instead. This matrix belongs in [COMPONENTS.md](../COMPONENTS.md) once this ADR is accepted.

| Consumer | Identity | Secrets via ESO | TLS issuer | Storage class / COSI bucket | DB on CNPG | Backup target | DNS record | Metrics |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Kubernetes API | Workload: OIDC (`identity.kubernetes`). MGMT and INFRA: certificates only (ADR 0004) | n/a (hosts ESO) | kubeadm cluster CA | n/a | n/a (etcd) | etcd snapshots to the site target (ADR 0009) | API VIP, written by DCiaB | API server metrics |
| Argo CD | OIDC | OIDC client secret, repo credentials | platform issuer | none (state is Git) | none | none needed (Git) | external-dns | ServiceMonitor |
| Forgejo | OIDC auth source | OIDC secret, DB, S3 | platform issuer | `replicated-retain` for repos; bucket for LFS, packages, attachments | yes | CNPG archive + repo volume (ADR 0009) | external-dns | ServiceMonitor |
| Grafana | Generic OAuth | OIDC secret, DB | platform issuer | none (dashboards from Git) | yes | CNPG archive | external-dns | ServiceMonitor |
| NetBox | OIDC | OIDC secret, DB, API tokens | platform issuer | bucket for media | yes | CNPG archive | external-dns | ServiceMonitor |
| OpenBao | OIDC auth method (people) | n/a (it is the store) | platform CA, issued at the platform step | `local`, else `replicated-retain` (Raft) | none (Raft) | Raft snapshots to the site target (ADR 0009, decision 4) | external-dns | telemetry endpoint |
| AWX | OIDC | OIDC secret, DB, tenant credentials | platform issuer | `replicated-retain` for projects | yes | CNPG archive | external-dns | ServiceMonitor |
| Terrakube | OIDC (Dex) | OIDC secret, DB, S3 | platform issuer | bucket for state and run outputs | yes | CNPG archive; versioned state bucket | external-dns | ServiceMonitor |
| OpenSearch | OIDC (Security plugin) | OIDC secret, internal users | platform issuer (HTTP and transport) | `local`, else `replicated` | none | snapshots to a bucket | external-dns | exporter |
| OpenVox | Hosts: LDAP outpost to SSSD (ADR 0004) | ENC and Hiera keys | platform issuer for web endpoints; OpenVox CA for agents | `replicated-retain` for code and the OpenVox CA | OpenVoxDB | CNPG archive + OpenVox CA | external-dns | exporter |
| Proxmox VE | OIDC realm, opt-in (decision 3) | CAPMOX token from OpenBao | site's, or platform CA by opt-in | provides VM disks; Ceph via `ceph-csi` | n/a | the site's (outside DCiaB) | static record | exporter |
| vCenter | LDAP identity source, or federation to the site IdP; opt-in (decision 3) | CAPV and CSI credentials from OpenBao | site's (VMCA) | provides volumes via `vsphere-csi` | n/a | the site's (outside DCiaB) | site DNS | exporter, optional |

"Platform issuer" is the cert-manager `ClusterIssuer` backed by OpenBao's PKI ([ADR 0005](0005-secrets.md), decision 4). Until OpenBao is up, it's a CA issuer from the platform CA. "CNPG archive" means WAL archiving and base backups to an object store bucket.

Gaps the matrix exposes, each owned by the implementation of this ADR:

- **vCenter** wasn't in ADR 0004's identity plan; decision 3 adds it, with the federation limit above.
- **Client provisioning** wasn't defined; decision 3 picks blueprints, then OpenTofu.
- **Group-to-role mapping** wasn't defined beyond admin; decision 3 adds the three roles.
- **UIs without OIDC** (Longhorn, Hubble, Alertmanager, Prometheus, the SeaweedFS UI) aren't in the matrix. They go behind an OIDC-aware proxy on the Gateway. That pattern needs its own small decision.

### 10. What `dciab validate` checks

The schema does what JSON Schema can: one `provider` per block, the chosen provider's sub-block required, the others forbidden, as in `spec.pki`. `validate` adds:

1. **Tier order** (decision 2). Error when an override gives MGMT an INFRA-hosted provider (`storage.object: seaweedfs`, `ipam: netbox` with `mode: platform`, `identity` for the MGMT API), when MGMT's or OpenBao's backup target isn't the site target, or when INFRA backs up to its own object provider with no off-site or site copy of the critical set (ADR 0009).
2. **No double replication.** Error when `longhorn` or `openebs: mayastor` with more than one replica sits on replicated storage. Offline, `validate` warns when the Proxmox storage name or the vSphere datastore is set, since it can't know its type. With `--live`, it reads the type (Proxmox `rbd` or `cephfs`, vSphere `vsan`) and errors. Today's check only hints at this (`check_resilience`).
3. **Platform fit.** `vsphere-csi` only for `vsphere`-class clusters; `openebs: mayastor` only with at least three workers per cluster that uses it, and only on a ClusterClass revision that reserves huge pages. `openebs: lvm`, and Longhorn on VMs, need `dataDiskGiB` on the worker pools.
4. **Required provider fields.** `ceph-csi`: monitors, cluster ID, pool, key. `vendor-csi`: a chart that is in `bundle.lock`. `existing`: a StorageClass name. Exactly one default StorageClass per cluster.
5. **COSI.** `cosi: true` only with `seaweedfs` or `rook-ceph`. With `external`, every enabled consumer needs a bucket entry.
6. **Identity.** `external.issuer` is `https`. Every name in `consumers` has a client entry with `external`. A secret is required for every client except the public `kubernetes` one. `openvox` in `consumers` with no `external.ldap` is a warning (hosts get break-glass only). `vcenter` in `consumers` needs LDAP (Authentik outpost or `external.ldap`), or the note that the site federates vCenter itself. `roles.operator` and `roles.viewer`, when set, differ from `adminGroup` and from each other.
7. **DNS.** `external.zone` is `network.domain` or a parent of it. `rfc2136` needs a TSIG key; `webhook` needs a provider pinned in `bundle.lock`.
8. **IPAM.** With `netbox: site`, a URL, a token and a CA. The config's ranges stay required either way: Day 0 needs them.
9. **Secrets.** `external.address` is `https`, and a CA bundle is given unless the platform CA covers it. With `--live`, `validate` checks the address answers as a Vault API (`/v1/sys/health`).
10. **Every referenced secret** follows the existing rules: `{file:}` or `{env:}`, present, not world-readable.

## Alternatives considered

- **Keep DCiaB as the only provider of everything.** Simplest to build and test. It rules out most enterprise sites, which have an IdP, an array and DNS they won't replace, and it forces double replication on every vSphere and Ceph site. Rejected.
- **A free-form `external` escape hatch per service** (a StorageClass name, an issuer URL, nothing more). Cheap, but `validate` can't check it, and the consumers still need to know what's behind it (COSI or not, LDAP or not, PKI or not). Kept only as `block.provider: existing`, where a StorageClass name really is all DCiaB needs.
- **One `providers:` map at the top of `spec`.** It reads well, but separates each service's choice from its settings. The `spec.pki` shape, where the choice and its fields sit together, is already understood by users and by `validate`. Rejected.
- **Authentik always, federating any site IdP.** One integration to test, and it keeps group mapping in one place. That stays the default. But some sites won't run a second IdP in front of their own, and Authentik on INFRA would then be a login dependency for nothing. Both are allowed.
- **Rook-Ceph as the one storage system** for block, file and object. It removes Longhorn and SeaweedFS. It also brings Ceph's operational weight to every site, including small ones and sites that already have an array. It stays an option (`rook-ceph` for object, `ceph-csi` for an existing Ceph), not the default.
- **Garage or MinIO instead of SeaweedFS.** See decision 6.
- **A generic iSCSI CSI driver for every SAN.** It can't create LUNs, snapshot or resize without the array's API. Rejected in favour of vendor drivers (decision 4).
- **A live NetBox CAPI IPAM provider.** Only an early alpha exists, and it would make MGMT depend on INFRA. Rejected for now (decision 7).

## Consequences

- The schema change is breaking in shape. The one-release `storage.longhorn` alias and a `validate` hint soften it. `cluster-values`, the Longhorn add-on and every example change in the implementing PR.
- `bundle.lock` grows: ESO, CNPG, SeaweedFS, the COSI controller and drivers, vSphere CPI and CSI, ceph-csi, external-dns, and the OpenEBS chart once it's in. Each is pinned and its images go in the archive like everything else. Vendor CSI drivers are the open part (open question 3).
- Test cost grows with the matrix. CI renders and validates every provider; the lab tests the defaults per platform (vSphere CSI, Ceph CSI on Proxmox, Longhorn on Proxmox local storage) before release. Site-provided providers other than those are documented as "supported by configuration, not lab-tested" until a site tests them.
- The order of work is: the schema and `validate` with the alias; `identity.kubernetes` replacing the hard-coded issuer; `vsphere-csi` and `ceph-csi`; the Longhorn data disk and `replicated-retain`; COSI with SeaweedFS; then `openebs`, `external` DNS and `netbox` IPAM.
- Authentik blueprints need a spike: how client secrets from OpenBao reach the blueprint, and whether a rerun of the platform step changes nothing.
- SeaweedFS's S3 bucket versioning must be checked before Terrakube's state bucket depends on it ([ADR 0006](0006-iac-path.md) requires versioning). Its README has described versioning as catching up with MinIO.
- With `identity: external`, DCiaB can't create clients, so a site's IdP team is on the install path. The docs need a "clients to create" list generated from `consumers`.
- With `secrets: external`, the site's Vault holds DCiaB's recovery-critical material. The site's backup and unseal procedures become part of DCiaB's DR plan (ADR 0009).

## Open questions for Trevor

1. **OpenEBS: which engine first, and in v1 at all?** Local PV LVM is cheap and fills the `local` class on array sites. Mayastor is fast but needs huge pages and reserved cores, so it needs a node image and class revision. The proposal is LVM first, Mayastor after a lab spike. Do you want Mayastor in v1?
2. **Default on Proxmox with Ceph.** The proposal is `ceph-csi` against the Proxmox Ceph cluster, which needs a Ceph client key from the hypervisors. The alternative is a Proxmox CSI driver, which makes VM disks on any Proxmox storage through the PVE API. Which do you prefer? (At the time of writing, `examples/proxmox.yaml` runs Longhorn ×2 on `ceph-vm`, which decision 4 forbids; re-check once the examples are rewritten.)
3. **Vendor CSI drivers in the archive.** Pin a fixed set (Trident, Dell CSM, Pure, HPE) in `bundle.lock`, or support a site-built overlay archive with extra charts and images? The overlay keeps product docs site-agnostic but needs its own signing and verification.
4. **External DNS for INFRA's names.** With `dns: external`, should the platform names (`auth.`, `git.`, `registry.`) go in the site zone, or should DCiaB always own a delegated subzone (`dciab.<site domain>`) even when the site's DNS is authoritative? The subzone is simpler to automate and harder to get from some DNS teams.
5. **`secrets: external` scope.** Is a Vault-API-only external store acceptable for v1 (decision 7), or do you need a mode where the site store holds only secrets and DCiaB still runs OpenBao for PKI and SPIRE?
6. **vCenter and Proxmox opt-in.** Should DCiaB ever change a site hypervisor's identity source, or only document it? The proposal is opt-in through `consumers`.
7. **Per-cluster overrides.** Tiers cover MGMT, INFRA and "all workload clusters". Do workload clusters need their own providers (one tenant on the array, another on Longhorn) in v1, or later?
8. **Kubernetes structured authentication.** The `r1` classes are still a draft. Should they switch from API server OIDC flags to an `AuthenticationConfiguration` file now, which allows more than one issuer (Authentik and a site IdP during a migration)?

## References

Primary sources unless marked otherwise.

- vCenter Server identity federation, supported providers and versions (VMware docs; Broadcom is moving these to techdocs.broadcom.com, and no equivalent URL was found there): https://docs.vmware.com/en/VMware-vSphere/8.0/vsphere-authentication/GUID-0A3A19E6-150A-493B-8B57-37E19AB420F2.html
- Broadcom KB 435677, external federation with Okta, Entra ID or PingFederate: https://knowledge.broadcom.com/external/article/435677/external-federation-okta-microsoft-entra.html
- vSphere Container Storage Plug-in requirements (VMware docs; same caveat as above): https://docs.vmware.com/en/VMware-vSphere/8.0/vsphere-storage/GUID-BA795112-AFC4-4FCB-B5A6-1ACDCAB79ED3.html
- vSphere CSI driver releases (Kubernetes 1.34 to 1.36 for v3.8.0): https://github.com/kubernetes-sigs/vsphere-csi-driver/releases
- OpenEBS releases (Replicated PV Mayastor, Local PV LVM, ZFS, Hostpath): https://github.com/openebs/openebs/releases
- OpenEBS prerequisites (Mayastor huge pages, cores, `nvme-tcp`, three nodes; LVM `dm-snapshot`). Secondary: openebs.io couldn't be read, so this is a Netlify build of the OpenEBS docs; check against openebs.io: https://gallant-pasteur-5e438c.netlify.app/docs/quickstart-guide/prerequisites
- Longhorn 1.11 release post (V2 data engine in technical preview): https://longhorn.io/blog/longhorn-v1.11.0/
- democratic-csi drivers (`freenas-*`, `zfs-generic-*`, `synology-iscsi`, `node-manual` for pre-created iSCSI volumes): https://github.com/democratic-csi/democratic-csi (the README was read through its ecosyste.ms index) and https://github.com/democratic-csi/charts
- COSI drivers and API versions: https://container-object-storage-interface.sigs.k8s.io/drivers
- Rook's CephCOSIDriver controller (source): https://pkg.go.dev/github.com/rook/rook@v1.14.3/pkg/operator/ceph/object/cosi, and the Rook COSI guide: https://rook.io/docs/rook/v1.18/Storage-Configuration/Object-Storage-RGW/cosi/
- SeaweedFS README (replication, 10+4 EC, license): https://raw.githubusercontent.com/chrislusf/seaweedfs/master/README.md
- Garage README (AGPLv3, small-to-medium scale): https://github.com/deuxfleurs-org/garage
- Garage site (three-zone replication): https://garagehq.deuxfleurs.fr/
- MinIO community repository (archived 2026-04-25; README on maintenance and source-only distribution): https://github.com/minio/minio
- ExternalDNS providers (in-tree and webhook): https://kubernetes-sigs.github.io/external-dns/latest
- NetBox CAPI IPAM provider (v0.1.0-alpha.1): https://beta.pkg.go.dev/github.com/erwin-kok/cluster-api-ipam-provider-netbox/internal/ipam
- Authentik blueprints: https://docs.goauthentik.io/customize/blueprints/
- Proxmox VE OpenID groups patch (pve-devel, March 2025): https://lists.proxmox.com/pipermail/pve-devel/2025-March/069397.html
