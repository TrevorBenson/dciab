# ADR 0009: Backup and disaster recovery

- Status: proposed
- Date: 2026-11-22

## Context

What exists today:

- Longhorn runs on MGMT with a backup target from `spec.storage.longhorn.backupTarget` (the site's S3 or NFS), but nothing creates a backup. There are no RecurringJobs (`addons/longhorn/`), so no volume is ever backed up.
- The default StorageClass `longhorn` has `reclaimPolicy: Delete` (`addons/longhorn/values.yaml`). Deleting a PVC, or a namespace, or pruning an application deletes its data at once.
- No cluster takes etcd snapshots. Nothing exports the CAPI objects, either before the pivot (when they live in the bootstrap host's kind cluster) or after it, when MGMT holds every cluster's machines, IP claims, kubeconfigs and CAs.
- ADRs [0005](0005-secrets.md) and [0006](0006-iac-path.md) say OpenBao snapshots and Terrakube state must also leave the site. The config has no field for a target that does.
- The bootstrap host keeps the age-encrypted platform CA key (`<state>/pki/ca.key.age`), the sealed hypervisor root passwords and the MGMT kubeconfig on its work disk. Nothing copies them anywhere.

Two constraints shape the design:

- **MGMT comes first.** MGMT needs a backup target from the moment it exists, long before INFRA's SeaweedFS does. If MGMT backed up to SeaweedFS, restoring INFRA would need MGMT, and restoring MGMT would need INFRA. A standing project decision already covers this: sites provide S3 or NFS for backups.
- **A copy on the same hardware isn't disaster recovery.** SeaweedFS on INFRA protects against a deleted volume or a bad upgrade. It doesn't protect against losing INFRA, or the site.

Storage, identity, DNS, IPAM and secrets each have a provider choice ([ADR 0008](0008-provider-pattern.md)): Longhorn, vSphere CSI, a SAN over iSCSI, OpenEBS and others for block storage, SeaweedFS or the site's own S3 for objects. Where replicas and backup copies are placed across hosts and racks is [ADR 0010](0010-failure-domains.md). This ADR says what gets backed up, where to, how often, and how restores are proven.

## Decisions

### 1. Two kinds of target, and one rule

- **The site target** is the site's own S3 bucket or NFS export, outside DCiaB's clusters. It is required, as it is today.
- **The off-site target** is S3 or NFS at another location. It holds the copy that survives losing the site.
- **The object provider** is the S3 endpoint chosen under ADR 0008 (`storage.object`): SeaweedFS on INFRA by default, or the site's S3.

Some tools write only to object storage: OpenBao's snapshot agent uploads to S3, and CloudNativePG's Barman Cloud plugin writes to S3, Azure or GCS, never to a filesystem. A site that provides only NFS needs an S3 endpoint in front of it (decision 4a).

The rule: every item in the critical set (below) has a copy that isn't on DCiaB's own hardware, and a copy off the site. A site that can't have an off-site copy records that in the config (decision 9), and `dciab validate` warns on every run.

| Tier | Primary copy | Off-site copy |
| --- | --- | --- |
| MGMT (etcd, CAPI exports, MGMT's volumes) | Always the site target. Never INFRA's SeaweedFS, and `dciab validate` rejects it | Copied from the site target (decision 8) |
| OpenBao | Always the site target (decision 4) | Copied from the site target |
| INFRA (etcd, databases, volumes, logs) | The object provider, or the site target | Required for the critical set. With no off-site target, `dciab validate` errors unless the config puts INFRA on the site target (`infra: {target: site}`) |
| Workload clusters | The object provider, or the site target | Recommended; the tenant decides |
| Recovery bundle (decision 10) | The site target | Required, plus an offline copy the site keeps |

**The critical set** is what a rebuild can't recreate from the archive, the config and Git: etcd snapshots of MGMT and INFRA, CAPI exports, OpenBao snapshots, CloudNativePG base backups and WAL, Forgejo repositories, Terrakube state, and the recovery bundle. Everything else (logs, metrics, workload data) is backed up on site, and off site where the site asks for it.

### 2. etcd snapshots on every cluster

DCiaB's clusters are kubeadm clusters under CAPI's KubeadmControlPlane, which has no backup of its own. Each cluster runs an **etcd snapshot CronJob**, a DCiaB add-on, not part of the ClusterClass:

- It runs on a control plane node (node selector and toleration for the control plane), with host network and read-only host mounts of `/etc/kubernetes/pki/etcd`. It runs `etcdctl snapshot save` against `https://127.0.0.1:2379` with kubeadm's etcd client certificate, then checks the file with `etcdutl snapshot status`.
- A snapshot holds every Secret in the cluster, so it's **age-encrypted to `spec.bootstrap.recipients` before it leaves the node**. The cluster has only the public keys, so a compromised cluster can't read its old snapshots. Restoring needs one recipient's identity, which matches who is allowed to do a restore.
- The upload goes to the tier's primary target. The job prunes by count; it never relies on bucket lifecycle rules, which a site S3 may not allow and NFS doesn't have.
- The etcd image comes from the same pin as the cluster's etcd, and the upload tool from `bundle.lock`, both through the site registry.

A CronJob avoids a new ClusterClass revision and a node roll on every cluster. It runs only while the cluster's API works, which is when snapshots matter: a snapshot from a cluster that has already failed isn't useful.

| Cluster | Schedule | Kept on the primary | Off site |
| --- | --- | --- | --- |
| MGMT | Every hour | 24 hourly and 14 daily | 30 daily |
| INFRA | Every hour | 24 hourly and 14 daily | 30 daily |
| Workload clusters | Every 6 hours | 7 days | Tenant's choice |

**Before any change to a control plane** (a Kubernetes upgrade, a ClusterClass revision change, a provider upgrade on MGMT), the step that makes the change takes a snapshot first and refuses to continue if the upload fails.

Restore follows the Kubernetes procedure: stop every API server, restore every member from the same snapshot with `etcdutl snapshot restore`, then restart the API servers, scheduler, controller manager and kubelets. DCiaB documents it as a runbook and drills it (decision 11). For MGMT, an etcd restore is the first choice when MGMT's VMs still exist.

### 3. CAPI state is exported, before the pivot and every day after

MGMT's etcd snapshot covers CAPI's objects only while MGMT's VMs can come back. To rebuild MGMT from nothing, DCiaB also exports CAPI's objects with `clusterctl move --to-directory`:

- **Before the pivot,** the `move` step exports the bootstrap cluster to `<state>/capi-backup/<timestamp>/` and copies it to the site target. If the bootstrap host dies between `cluster` and `move`, MGMT's VMs aren't orphaned.
- **After the pivot,** the `adopt` step exports MGMT once.
- **Every day,** a CronJob on MGMT exports again. 14 are kept on the site target, 30 off site.
- **Before any upgrade** of CAPI, its providers or MGMT's Kubernetes, the upgrade step exports first.

An export holds the clusters' CA and kubeconfig Secrets, so it's age-encrypted to the recipients like an etcd snapshot. After each export the job checks that no Cluster was left paused.

Cluster API documents `--to-directory` as the replacement for the old `clusterctl backup`, and also says `move` wasn't designed as a backup tool: it expects the clusters to be stable, and it never restores `status`. So the export is the fallback, used when MGMT must be rebuilt. The path, written as a runbook and drilled: rebuild MGMT from the archive and the config, install the same providers, then import the newest export with `clusterctl move --from-directory`. Exports are taken only when no Cluster is upgrading, scaling or remediating; the job skips and retries otherwise.

### 4. OpenBao: raft snapshots to the site target, with credentials outside OpenBao

OpenBao documents scheduled raft snapshots as a CronJob from its Helm chart (the `openbao-snapshot-agent`), authenticated with the Kubernetes auth method and a role whose policy only reads `sys/storage/raft/snapshot`. DCiaB uses that:

- **Every hour**, kept 14 days on the site target and 90 days off site. A snapshot is encrypted by OpenBao's barrier, so it isn't age-encrypted again; it's useless without the unseal or recovery keys ([ADR 0005](0005-secrets.md), decision 6).
- **The target is the site target, not SeaweedFS.** Restoring OpenBao is the first step in recovering INFRA, before SeaweedFS can be trusted. This amends ADR 0005, decision 1 (see [Amendment to ADR 0005](#amendment-to-adr-0005-2026-11-23)).
- **The target's credentials never live only in OpenBao.** They come from the config's `{file: ...}` or `{env: ...}` reference, and DCiaB applies them as a Kubernetes Secret directly, as `addons/longhorn` does today, not through External Secrets Operator. This is an exception to ADR 0005's rule that secrets reach clusters through ESO (same amendment). A copy is in the recovery bundle. A restore therefore needs only the archive, the config, the recovery bundle and the key holders.
- **Restore** is `bao operator raft snapshot restore` onto a new cluster, with `-force` because the new cluster's keys differ, then unseal with the original keys (or the original auto-unseal key, which the site backs up under its own procedures).

### 4a. Sites that provide only NFS

The standing decision stays: a site provides S3 or NFS. Longhorn, the etcd snapshot job, the CAPI export and the copier can all write to NFS. OpenBao's snapshot agent and CloudNativePG's Barman Cloud plugin can't: they need S3. So on an NFS-only site, "OpenBao always to the site target" and `infra: {target: site}` have nothing to write to.

The options:

- **(a) Require S3 for them.** `dciab validate` errors when the site target is NFS and OpenBao, or INFRA's databases, would use it. Simplest, and nothing new runs. But it takes back "S3 or NFS" for every site that runs OpenBao, which is every site.
- **(b) An S3 shim on MGMT, backed by the NFS export.** DCiaB runs a small single-instance S3 server on MGMT, with all its data and metadata on the site's NFS export, and points OpenBao and CloudNativePG at it. SeaweedFS (`weed server` with its filer) is already in the stack and pinned; Garage is the alternative. MGMT comes before INFRA, so the tier order of ADR 0008 holds.
- **(c) Write to the object provider, then copy files to NFS.** OpenBao and CloudNativePG write to INFRA's SeaweedFS, and a file-level copy job moves the objects to the NFS export. Nothing new runs on MGMT. But it puts the primary copy back on INFRA's hardware, adds the copy's delay to every recovery point, and reopens the restore order: CloudNativePG can only recover from an S3 endpoint, so a restore still needs one, and SeaweedFS's credentials come from OpenBao.

**Recommendation: (b), with SeaweedFS as the shim.** It keeps "S3 or NFS" for sites, keeps OpenBao's and the databases' primary copy off INFRA, and adds no new tool to the archive. Its costs: one more stateful service on MGMT, with its own credentials (applied like the site target's, decision 4, and kept in the recovery bundle); S3 throughput limited by the NFS export; and the shim depends on the NFS server's `fsync` and locking behaving. Because its data and metadata are all on the export, a new shim, on a rebuilt MGMT or on a bootstrap host from the archive, can serve the same objects during a restore. The shim runs only when the site target is NFS. `dciab validate` reports which mode a site gets. This is open question 8.

### 5. CloudNativePG: the Barman Cloud plugin, WAL archiving and point-in-time recovery

CloudNativePG deprecated its in-tree Barman Cloud support (`.spec.backup.barmanObjectStore`) in 1.26 and removes it in 1.32. DCiaB starts with the **Barman Cloud plugin**:

- One `ObjectStore` per database cluster, on the tier's primary target, with its own path (`serverName`). The plugin is listed in the Cluster's `plugins` with `isWALArchiver: true`, so WAL is archived continuously.
- A `ScheduledBackup` with `method: plugin` takes a base backup every day.
- The retention policy is on the `ObjectStore`: 14 days, which is also the point-in-time recovery window. Off site, 30 days of base backups and WAL.
- **Restores go to a new Cluster**, bootstrapped from the `ObjectStore` (with a target time for point-in-time recovery). A restore never overwrites the cluster it came from, and the new cluster archives to a new `serverName`, so it can't overwrite the old cluster's WAL.

This covers every CloudNativePG database: Authentik, Forgejo, NetBox, Artifact Keeper, OpenVoxDB, Terrakube, AWX, and SeaweedFS's filer if it uses one. Postgres volumes get no Longhorn backups: a volume backup of a running database is only crash-consistent, and it has no point-in-time recovery.

### 6. Block storage: Longhorn jobs, and retain classes for platform data

Longhorn 1.8 and later model targets as `BackupTarget` objects (the one from Helm's `defaultBackupStore` is named `default`), and each volume names its target (`backupTargetName`, from its StorageClass). Backups run only from RecurringJobs. DCiaB ships:

- **The `default` target** set to the tier's primary target.
- **Recurring jobs on ADR 0008's provider-neutral StorageClasses**, which Longhorn implements:
  - `replicated`, the default: `reclaimPolicy: Delete`, for workloads. Volumes with no job of their own fall into Longhorn's `default` group: a snapshot every hour (24 kept) and a backup every day (14 kept).
  - `replicated-retain`: `reclaimPolicy: Retain`, for platform stateful services that have no backup of their own (Forgejo repositories, AWX projects, the OpenVox CA and code). Its `recurringJobSelector` names a `platform` group: a snapshot every hour (24 kept) and a backup every day (14 kept).
  - `local` (one replica, `strict-local`), for services that replicate and back up themselves (CloudNativePG, OpenBao, OpenSearch, SeaweedFS volume servers): a `local` group with a snapshot every hour (24 kept) and no Longhorn backup, so their data isn't backed up twice. When a cluster has no `local` class, these services fall back to `replicated-retain` (ADR 0008, decision 5) and get the same snapshot-only jobs through Longhorn's per-PVC job labels.
  - Platform data on `local` must also survive a deleted PVC. This ADR needs `local` to be `reclaimPolicy: Retain` for platform services, or a `local-retain` class next to it; ADR 0008 defines which.
- **Argo CD never prunes PVCs** of platform applications (`Prune=false` on PVCs), so removing an application from Git doesn't delete its data.

Other block providers under ADR 0008 (vSphere CSI, a SAN over iSCSI, OpenEBS) have no Longhorn-style backup to a target. On those, the platform's stateful services rely on their application-level backups (decisions 4, 5 and 7), the classes keep the same names and roles (`replicated`, `replicated-retain`, `local`), and backing up raw volumes is the site's array's job.

### 7. The other stateful services

| Service | What is backed up | How | Schedule and retention |
| --- | --- | --- | --- |
| SeaweedFS | Its buckets aren't backed up as a whole: SeaweedFS is a backup store. The buckets in the critical set (Terrakube state, CloudNativePG, etcd and other INFRA backups) are copied off site (decision 8). Its filer metadata is backed up | Replication `010` (a copy in another rack) when ADR 0010 gives at least two failure domains, `001` (another server) otherwise. Erasure coding (Reed-Solomon 10+4) for warm buckets of backups only, and only with at least four volume servers, so losing one server costs at most four of the fourteen shards. Filer metadata on CloudNativePG (decision 5) | Replication is continuous; erasure coding converts full, quiet volumes |
| OpenSearch | Indices | A snapshot repository on the object provider, and a Snapshot Management policy | Every day, 14 kept. Audit and security indices also off site, for as long as the site's retention policy says |
| Prometheus | Nothing by default. Metrics are rebuilt as clusters run | Local retention sized for 15 days. Long-term storage is a later decision | — |
| Forgejo | Its database (decision 5) and its repository and LFS volume | The volume on `replicated-retain`, backed up every day. A weekly `forgejo dump` to the object provider gives a portable copy. Restores are checked with `git fsck` | Volume: 14 daily. Dump: 4 weekly, off site |
| Terrakube | Its database (decision 5) and its state bucket | The bucket is versioned ([ADR 0006](0006-iac-path.md)) and copied off site | Bucket versions: 30 days |
| Zot and Artifact Keeper | Only what the site pushed: tenant and site-built images. Everything DCiaB ships is in the archive | Storage on the object provider. Those repositories' buckets are copied off site when the site asks | 30 days |

**The archive is the cold-start source for images.** After a full site loss, nothing needs INFRA's registry to come back: a bootstrap host serves the archive's images from its read-only Zot, as `dciab seed` does on Day 0. So the site keeps the archive of its current bundle version (the USB stick and one copy), and replaces it with every new bundle it imports.

### 8. Off-site copies are made by one copier per tier

Producers write once, to their primary target. A **copier** CronJob per tier copies the critical prefixes from the primary target to the off-site target every hour, with a tool pinned in `bundle.lock`:

- It copies; it never syncs deletions. Its off-site credentials can write but not delete. Off-site retention is enforced by the off-site store (lifecycle rules, and object lock or versioning where it has them). One store applies one retention, so it keeps everything for `offsite.retentionDays`, which must be at least the largest off-site number in this ADR (90 days, OpenBao's). The shorter per-item numbers are what restores are planned against.
- Its credentials, like the site target's, come from the config and are applied directly, never only from OpenBao, and are in the recovery bundle (read credentials too, so a restore can read the off-site copy).
- The off-site copy lags its source by up to an hour. For CloudNativePG this is the off-site recovery point; on site it is the WAL archive's.
- A site whose own S3 already replicates off site says so (`mode: site-replicated`, decision 9) and runs no copier.

### 9. `spec.backup`: one place for targets, schedules and retention

A sketch. It follows ADR 0008's provider pattern: targets are named, and the object provider is referred to, not configured again. Field names follow ADR 0008 where they differ. The schema change comes with the code, not with this ADR.

```yaml
spec:
  backup:
    targets:
      site:                                   # required
        url: s3://dciab-backup@s3.example.net/site-a   # or nfs://server:/export
        credentials: {file: secrets/backup-site.env}
        caBundle: {file: secrets/backup-site-ca.pem}    # optional: a private CA
      offsite:                                # required, or mode: none with a reason
        mode: copy                            # copy | site-replicated | none
        url: s3://dciab-dr@dr.example.net/site-a
        credentials: {file: secrets/backup-offsite.env} # can write, can't delete
        readCredentials: {file: secrets/backup-offsite-read.env}
        retentionDays: 90                     # what the off-site store keeps; at least the largest off-site number above
        # reason: "no second location"        # required with mode: none
    tiers:
      mgmt: {target: site}                    # fixed; anything else is an error
      infra: {target: objectStore}            # objectStore (ADR 0008) or site
      workloads: {target: objectStore}
    schedules:                                # DCiaB's defaults; each may be overridden
      etcd:
        mgmt:      {cron: "0 * * * *",   keep: {hourly: 24, daily: 14}}
        infra:     {cron: "0 * * * *",   keep: {hourly: 24, daily: 14}}
        workloads: {cron: "0 */6 * * *", keep: {days: 7}}
      capiExport:  {cron: "30 2 * * *",  keep: {daily: 14}}
      openbao:     {cron: "15 * * * *",  keep: {days: 14}}
      postgres:    {baseBackup: "0 3 * * *", retention: 14d}
      longhorn:
        snapshot:  {cron: "0 * * * *",   retain: 24}
        backup:    {cron: "0 1 * * *",   retain: 14}
      opensearch:  {cron: "0 4 * * *",   retain: 14}
      copier:      {cron: "45 * * * *"}
```

- `spec.storage.longhorn.backupTarget` and `backupCredentials` move to `targets.site`. While both exist, `dciab validate` reads the old fields and warns.
- `dciab validate` errors when MGMT's tier isn't `site`, when `offsite.mode` is `none` without a `reason`, and when `infra` uses the object provider and off site is `none` (the critical set then goes to the site target, and the config must say `infra: {target: site}`). It reports when the site target is NFS and the S3 shim (decision 4a) will run. It warns on every run when off site is `none`, and when an off-site NFS target can't be made immutable. `validate --live` checks that both targets answer from the node network.

### 10. The recovery bundle: bootstrap state, and who can open it

The bootstrap host isn't a backup location. Its state directory can be lost, and the host is retired after `finalize`. Everything a rebuild needs that isn't in the archive, the config or Git goes into a **recovery bundle**:

- the age-encrypted platform CA key (`generate-root`), the sealed hypervisor root passwords, and the age-encrypted break-glass kubeconfigs;
- OpenBao's unseal or recovery shares, each still encrypted to its one holder ([ADR 0005](0005-secrets.md), decision 6);
- the site and off-site target credentials, age-encrypted to the recipients;
- the newest CAPI export's location, the bundle version, and a manifest with every file's sha256;
- a canary file per recipient, each encrypted to that recipient alone.

The bundle holds only encrypted files; no plaintext ever leaves the bootstrap host. `dciab` writes it to the site target after `seed`, after `move` and at `finalize`, and again whenever its contents change, and the copier takes it off site. The site also keeps an offline copy with the archive.

**Custody of the recipients' identities** is the one thing DCiaB can't back up for the site, and it decides whether any of this can be restored:

- An age identity is never in the bundle, the config, Git or a cluster. Each recipient keeps theirs offline: on a hardware token through an age plugin, or printed and kept in a safe.
- At least two recipients keep their identities away from the site, in different places, so losing the site doesn't lose every identity.
- The site keeps a custody record (who holds which recipient, and where) outside DCiaB.
- Once a year, and whenever a recipient changes, each recipient decrypts their canary. When a recipient leaves, everything encrypted to all recipients is encrypted again to the new list (one current identity is enough), and OpenBao's Shamir shares are rekeyed.

### 11. Restores are drilled, not assumed

A backup counts only once a restore from it has passed.

**In CI (kind),** with SeaweedFS in the kind cluster as the S3 target:

| Drill | Passes when | When |
| --- | --- | --- |
| etcd snapshot job | The snapshot decrypts with a test identity, restores with `etcdutl` into a standalone etcd, and a marker key reads back | Every pull request that touches backup code |
| CAPI export and import | The e2e management cluster's objects export with `--to-directory`, import into a second kind cluster with `--from-directory`, and `hack/e2e/verify.py` passes there | Every pull request that touches backup code |
| CloudNativePG | A base backup and WAL go to the `ObjectStore`, a new Cluster recovers to a target time, and the rows written before it are there and those after aren't | Nightly |
| OpenBao | A raft snapshot from the snapshot agent restores into a new OpenBao, which unseals with the original keys and reads a known secret | Nightly |
| Recovery bundle | The bundle's manifest checks out and each canary decrypts with its test identity | Every pull request that touches backup code |

**As release-gate drills on the reference platforms** (vSphere and Proxmox), because they need real disks, hypervisors or quorum holders:

| Drill | When |
| --- | --- |
| Longhorn: back up a volume, restore it to a new PVC, compare checksums | With the first Longhorn RecurringJobs, then before each release |
| MGMT etcd restore on all members | Before each release; a person approves, as it stops MGMT |
| MGMT rebuilt from the archive, the config, the recovery bundle and the newest CAPI export | Before each release, and in the Phase 5 gate |
| OpenBao restore with a Shamir quorum | With the platform step, then before each release |
| Full-site cold start: every host powered off, then brought back in the documented order | The Phase 5 gate |

**At a site,** an in-cluster restore canary restores the newest CloudNativePG backup and one Longhorn backup to a scratch namespace every week, checks them and deletes them. A failed canary is an alert. People drill an OpenBao restore every quarter and a cold start every year.

Every backup job and the copier export the time of their last success. Alertmanager alerts when that's older than twice the schedule ([ADR 0007](0007-detection-to-response.md), decision 4), and `dciab status` shows the age of the newest backup of each kind.

## Alternatives considered

- **MGMT backs up to INFRA's SeaweedFS.** One S3 for everything, but restoring either cluster then needs the other. Rejected: MGMT uses the site target, always.
- **Velero for everything** (cluster objects, and volumes through CSI snapshots or file copy). It's one tool, but its volume backups of databases are crash-consistent with no point-in-time recovery, restoring CAPI objects needs a careful order and loses `status`, and it adds a stateful component to every cluster. Rejected as the platform's mechanism; it stays possible as a tenant service later.
- **etcd snapshots from a systemd timer on each control plane node,** set by the ClusterClass. It runs even when the API doesn't, but needs a new class revision and a node roll on every cluster, and a snapshot from a failing cluster isn't useful. Rejected for the CronJob.
- **Only etcd snapshots for MGMT, or only CAPI exports.** An etcd restore needs MGMT's VMs and addresses; an export alone loses `status` and depends on `move` for something it wasn't designed for. Both are kept, for different failures.
- **CloudNativePG's in-tree `barmanObjectStore`.** Simpler today, but deprecated since 1.26 and removed in 1.32. Rejected; DCiaB starts on the plugin.
- **Longhorn volume backups for databases and OpenBao.** Uniform, but crash-consistent, with no point-in-time recovery. Rejected for services with their own backups.
- **Automated raft snapshots inside OpenBao.** Vault's automated snapshots (`sys/storage/raft/snapshot-auto`) are a Vault Enterprise feature; OpenBao documents the CronJob instead.
- **Each producer writes to both targets.** No copier, but every tool needs a second target and credentials, and some (Longhorn's recurring jobs, CloudNativePG's WAL archiver) take only one. Rejected for one copier per tier.
- **Off-site copies optional.** Simpler for sites without a second location, but the default would be no disaster recovery. Rejected: off site is required, and a site without it says so in the config.

## Consequences

- A site needs S3 or NFS for the site target (as today) and, unless it records otherwise, an off-site target with write-only credentials.
- `dciab` gains backup jobs on every cluster, a copier per tier, the recovery bundle, and pre-change snapshots in every step that changes a control plane. Each job's tool is pinned in `bundle.lock` and in the archive.
- ADR 0005 is amended (below): OpenBao snapshots go to the site target, not SeaweedFS, and backup-target credentials reach clusters without ESO.
- On a site whose target is NFS, MGMT runs one more stateful service, the S3 shim (decision 4a, if open question 8 settles on it).
- Restoring an etcd snapshot, a CAPI export or the recovery bundle needs one recipient's age identity. Custody of those identities becomes part of operating a site, with a yearly check.
- Retain classes leave volumes behind after their PVCs go. Removing platform data becomes a deliberate, manual step, and capacity planning must allow for released volumes until someone deletes them.
- Off-site recovery points lag by up to the copier's interval (one hour by default).
- Restore drills join CI (kind), the release-gate drills, and the Phase 5 gate.

## Amendment to ADR 0005 (2026-11-23)

**Context.** [ADR 0005](0005-secrets.md), decision 1 keeps OpenBao's Raft snapshots on "S3 on SeaweedFS, plus a copy that leaves the site". SeaweedFS runs on INFRA, and its credentials come from OpenBao through ESO, so restoring OpenBao would need a service that itself depends on OpenBao. ADR 0005's Consequences also say that nothing in Git or Helm values holds a secret, and that everything is an `ExternalSecret` reference or an OpenBao path.

**Decisions.**
- In ADR 0005, decision 1, the words "kept off the cluster (S3 on SeaweedFS, plus a copy that leaves the site)" now read: kept off the cluster on the **site target** (the site's S3, or the S3 shim in front of its NFS export, decision 4a), plus the off-site copy (decision 8). They never go to INFRA's SeaweedFS. Schedule and retention are in decision 4.
- An exception to ADR 0005, decision 3 and its Consequences: the credentials of the site and off-site targets (and of the shim) don't come from OpenBao through ESO. They come from the config's `{file: ...}` or `{env: ...}` references and DCiaB applies them as Kubernetes Secrets directly, as `addons/longhorn` does today. A copy is in the recovery bundle (decision 10). Every other secret still follows ADR 0005.

**Consequences.** Restoring OpenBao needs only the site target, the recovery bundle and the key holders. The backup-target credentials are the one set of platform secrets that isn't rotated through OpenBao; the site rotates them in its own storage and updates the config.

## Open questions for Trevor

1. **Sites with no second location.** Is an encrypted export to removable media (the critical set and the recovery bundle, written to a disk the site carries away) an acceptable `offsite` mode, or is `mode: none` with a reason the only option there?
2. **Retention defaults.** Are 14 days on site and 30 to 90 days off site right, or should the defaults follow a stated compliance baseline (audit logs for a year, for example)?
3. **Who can restore.** Encrypting etcd snapshots and CAPI exports to the bootstrap recipients means every restore needs one of them. Is that intended, or should there be a separate backup key pair, its private key in OpenBao and in the recovery bundle, so on-call staff can restore without a recipient?
4. **Workload clusters.** Should etcd snapshots and the `default` Longhorn jobs be on by default for tenant clusters, or opt-in per tenant?
5. **Forgejo.** Should platform repositories also be push-mirrored to the site's own Git service, as a copy that needs no restore?
6. **Velero for tenants.** Is backup of tenant workloads a service DCiaB offers (Velero or similar), or left to tenants?
7. **The copier's tool.** rclone handles both S3 and NFS; is it acceptable as another pinned tool, or should the copier be limited to S3 and use the object stores' own replication?
8. **NFS-only sites** (decision 4a). OpenBao's snapshot agent and CloudNativePG's Barman Cloud plugin need S3. The recommendation is an S3 shim (SeaweedFS) on MGMT backed by the site's NFS export. The alternatives are to require S3 from every site that runs OpenBao, or to write to INFRA's SeaweedFS and copy files to NFS. Which one?

## References

- CloudNativePG, Barman Cloud plugin: [migrating from the in-tree support](https://cloudnative-pg.io/plugin-barman-cloud/docs/migration/) (deprecated in 1.26, removed in 1.32; `ObjectStore`, `isWALArchiver`, `ScheduledBackup` `method: plugin`, retention on the `ObjectStore`) and [introduction](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/).
- Longhorn: [setting a backup target](https://longhorn.io/docs/snapshots-and-backups/backup-and-restore/set-backup-target/) (multiple targets since 1.8, the `default` target, `defaultBackupStore`); [multiple backup stores test plan, v1.8.0](https://longhorn.github.io/longhorn-tests/manual/release-specific/v1.8.0/test-multiple-backup-stores) (`backupTargetName` on StorageClasses and volumes); [recurring snapshots and backups](https://longhorn.io/docs/snapshots-and-backups/scheduling-backups-and-snapshots/) (RecurringJob tasks, `retain`, groups, the `default` group, `recurringJobSelector`).
- OpenBao: [snapshot CronJob](https://openbao.org/docs/platform/k8s/helm/examples/snapshot-cronjob/) (`openbao-snapshot-agent`, Kubernetes auth, read on `sys/storage/raft/snapshot`, S3 upload); [raft storage](https://openbao.org/docs/configuration/storage/raft/).
- Vault: [automated raft snapshots API](https://developer.hashicorp.com/vault/api-docs/system/storage/raftautosnapshots) (Vault Enterprise).
- Cluster API: [`clusterctl move`](https://cluster-api.sigs.k8s.io/clusterctl/commands/move) (`--to-directory` and `--from-directory` replace `clusterctl backup` and `restore`; not designed as a backup tool; `status` isn't restored).
- Kubernetes: [operating etcd clusters](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/) (`etcdctl snapshot save`, `etcdutl snapshot restore`, restoring every member, encrypting snapshot files).
- SeaweedFS: [replication](https://github.com/seaweedfs/seaweedfs/wiki/Replication) (the `xyz` replication string, `defaultReplication`); [erasure coding](https://deepwiki.com/seaweedfs/seaweedfs/4.4-erasure-coding) (Reed-Solomon 10+4, up to four lost shards, volumes read-only after encoding).
