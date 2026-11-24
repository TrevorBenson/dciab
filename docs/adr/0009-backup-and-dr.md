# ADR 0009: Backup and disaster recovery

- Status: Accepted (2026-11-23)
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

Storage, identity, DNS, IPAM and secrets each have a provider choice ([ADR 0008](0008-provider-pattern.md)): Longhorn, vSphere CSI, the Proxmox CSI driver (the usual choice on Proxmox), ceph-csi and a pinned set of vendor SAN drivers for block storage, SeaweedFS or the site's own S3 for objects. Where replicas and backup copies are placed across hosts and racks is [ADR 0010](0010-failure-domains.md). This ADR says what gets backed up, where to, how often, and how restores are proven.

## Decisions

### 1. Two kinds of target, and one rule

- **The site target** is the site's own S3 bucket or NFS export, outside DCiaB's clusters. It is required, as it is today.
- **The off-site target** is S3 or NFS at another location, or removable media the site carries away (`mode: removable`). It holds the copy that survives losing the site.
- **The object provider** is the S3 endpoint chosen under ADR 0008 (`storage.object`): SeaweedFS on INFRA by default, or the site's S3.

Some tools write only to object storage: OpenBao's snapshot agent uploads to S3, and CloudNativePG's Barman Cloud plugin writes to S3, Azure or GCS, never to a filesystem. On a site that provides only NFS, DCiaB puts an S3 endpoint in front of it (decision 4a).

The rule: every item in the critical set (below) has a copy that isn't on DCiaB's own hardware, and a copy off the site. A site that can't have an off-site copy records that in the config (decision 9), and `dciab validate` warns on every run.

| Tier | Primary copy | Off-site copy |
| --- | --- | --- |
| MGMT (etcd, CAPI exports, MGMT's volumes) | Always the site target. Never INFRA's SeaweedFS, and `dciab validate` rejects it | Copied from the site target (decision 8) |
| OpenBao | Always the site target (decision 4) | Copied from the site target |
| INFRA (etcd, databases, volumes, logs) | The object provider, or the site target | Required for the critical set. With no off-site target, `dciab validate` errors unless the config puts INFRA on the site target (`infra: {target: site}`) |
| Workload clusters | The object provider, or the site target. etcd snapshots are on by default; volume backups are opt-in per tenant | Recommended; the tenant decides |
| Recovery bundle (decision 10) | The site target | Required, plus an offline copy the site keeps |

**The critical set** is what a rebuild can't recreate from the archive, the config and Git: etcd snapshots of MGMT and INFRA, CAPI exports, OpenBao snapshots, CloudNativePG base backups and WAL, Forgejo repositories, Terrakube state, and the recovery bundle. Everything else (logs, metrics, workload data) is backed up on site, and off site where the site asks for it.

### 2. etcd snapshots on every cluster

DCiaB's clusters are kubeadm clusters under CAPI's KubeadmControlPlane, which has no backup of its own. Each cluster runs an **etcd snapshot CronJob**, a DCiaB add-on, not part of the ClusterClass:

- It runs on a control plane node (node selector and toleration for the control plane), with host network and read-only host mounts of `/etc/kubernetes/pki/etcd`. It runs `etcdctl snapshot save` against `https://127.0.0.1:2379` with kubeadm's etcd client certificate, then checks the file with `etcdutl snapshot status`.
- A snapshot holds every Secret in the cluster, so it's **age-encrypted before it leaves the node, to `spec.bootstrap.recipients` and to the site's backup key** (decision 10). The cluster has only the public keys, so a compromised cluster can't read its old snapshots. Whoever can read the backup key's private half from OpenBao can restore without a recipient; when OpenBao itself is lost, a recipient opens it from the recovery bundle.
- The upload goes to the tier's primary target. The job prunes by count; it never relies on bucket lifecycle rules, which a site S3 may not allow and NFS doesn't have.
- The etcd image comes from the same pin as the cluster's etcd, and the upload tool from `bundle.lock`, both through the site registry.

A CronJob avoids a new ClusterClass revision and a node roll on every cluster. It runs only while the cluster's API works, which is when snapshots matter: a snapshot from a cluster that has already failed isn't useful.

| Cluster | Schedule | Kept on the primary | Off site |
| --- | --- | --- | --- |
| MGMT | Every hour | 24 hourly and 14 daily | 30 daily |
| INFRA | Every hour | 24 hourly and 14 daily | 30 daily |
| Workload clusters | Every 6 hours, on by default | 7 days | Tenant's choice |

These are defaults; each schedule and retention can be changed in `spec.backup` (decision 9).

**Before any change to a control plane** (a Kubernetes upgrade, a ClusterClass revision change, a provider upgrade on MGMT), the step that makes the change takes a snapshot first and refuses to continue if the upload fails.

Restore follows the Kubernetes procedure: stop every API server, restore every member from the same snapshot with `etcdutl snapshot restore`, then restart the API servers, scheduler, controller manager and kubelets. DCiaB documents it as a runbook and drills it (decision 11). For MGMT, an etcd restore is the first choice when MGMT's VMs still exist.

### 3. CAPI state is exported, before the pivot and every day after

MGMT's etcd snapshot covers CAPI's objects only while MGMT's VMs can come back. To rebuild MGMT from nothing, DCiaB also exports CAPI's objects with `clusterctl move --to-directory`:

- **Before the pivot,** the `move` step exports the bootstrap cluster to `<state>/capi-backup/<timestamp>/` and copies it to the site target. If the bootstrap host dies between `cluster` and `move`, MGMT's VMs aren't orphaned.
- **After the pivot,** the `adopt` step exports MGMT once.
- **Every day,** a CronJob on MGMT exports again. 14 are kept on the site target, 30 off site.
- **Before any upgrade** of CAPI, its providers or MGMT's Kubernetes, the upgrade step exports first.

An export holds the clusters' CA and kubeconfig Secrets, so it's age-encrypted to the recipients and the backup key, like an etcd snapshot. After each export the job checks that no Cluster was left paused.

Cluster API documents `--to-directory` as the replacement for the old `clusterctl backup`, and also says `move` wasn't designed as a backup tool: it expects the clusters to be stable, and it never restores `status`. So the export is the fallback, used when MGMT must be rebuilt. The path, written as a runbook and drilled: rebuild MGMT from the archive and the config, install the same providers, then import the newest export with `clusterctl move --from-directory`. Exports are taken only when no Cluster is upgrading, scaling or remediating; the job skips and retries otherwise.

### 4. OpenBao: raft snapshots to the site target, with credentials outside OpenBao

OpenBao documents scheduled raft snapshots as a CronJob from its Helm chart (the `openbao-snapshot-agent`), authenticated with the Kubernetes auth method and a role whose policy only reads `sys/storage/raft/snapshot`. DCiaB uses that:

- **Every hour**, kept 14 days on the site target and 90 days off site. A snapshot is encrypted by OpenBao's barrier, so it isn't age-encrypted again; it's useless without the unseal or recovery keys ([ADR 0005](0005-secrets.md), decision 6).
- **The target is the site target, not SeaweedFS.** Restoring OpenBao is the first step in recovering INFRA, before SeaweedFS can be trusted. This amends ADR 0005, decision 1 (see [Amendment to ADR 0005](#amendment-to-adr-0005-2026-11-23)).
- **The target's credentials never live only in OpenBao.** They come from the config's `{file: ...}` or `{env: ...}` reference, and DCiaB applies them as a Kubernetes Secret directly, as `addons/longhorn` does today, not through External Secrets Operator. This is an exception to ADR 0005's rule that secrets reach clusters through ESO (same amendment). A copy is in the recovery bundle. A restore therefore needs only the archive, the config, the recovery bundle and the key holders.
- **Restore** is `bao operator raft snapshot restore` onto a new cluster, with `-force` because the new cluster's keys differ, then unseal with the original keys (or the original auto-unseal key, which the site backs up under its own procedures).

### 4a. Sites that provide only NFS: an S3 shim on MGMT

The standing decision stays: a site provides S3 or NFS. Longhorn, the etcd snapshot job, the CAPI export and the copier can all write to NFS. OpenBao's snapshot agent and CloudNativePG's Barman Cloud plugin can't: they need S3.

So when the site target is NFS, DCiaB runs an **S3 shim on MGMT**: a single-instance SeaweedFS (`weed server` with its filer), already in the stack and pinned, with all its data and metadata on the site's NFS export. OpenBao and CloudNativePG write to it as their site target. MGMT comes before INFRA, so ADR 0008's tier order holds, and the primary copy stays off INFRA.

- The shim's credentials are applied like the site target's (decision 4) and kept in the recovery bundle.
- Its data and metadata are all on the export, so a new shim, on a rebuilt MGMT or on a bootstrap host from the archive, serves the same objects during a restore.
- S3 throughput is limited by the NFS export, and the shim relies on the NFS server's `fsync` and locking behaving. The release-gate drills (decision 11) include an NFS-only site.
- The shim runs only when the site target is NFS. `dciab validate` reports which mode a site gets.

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
- **Recurring jobs on ADR 0008's four provider-neutral StorageClasses**, which Longhorn implements:
  - `replicated`, the default: `reclaimPolicy: Delete`. On MGMT and INFRA, volumes with no job of their own fall into Longhorn's `default` group: a snapshot every hour (24 kept) and a backup every day (14 kept). On workload clusters the `default` group has no jobs: a tenant opts a volume in to the `backup` group (the same schedule) with Longhorn's per-PVC job labels.
  - `replicated-retain`: `reclaimPolicy: Retain`, for platform stateful services that have no backup of their own (Forgejo repositories, AWX projects, the OpenVox CA and code). Its `recurringJobSelector` names a `platform` group: a snapshot every hour (24 kept) and a backup every day (14 kept).
  - `local-retain` (one replica, `strict-local`, `reclaimPolicy: Retain`), for platform data that keeps its own copies and backs itself up (CloudNativePG, OpenBao, OpenSearch, SeaweedFS volume servers): a `local` group with a snapshot every hour (24 kept) and no Longhorn backup, so the data isn't backed up twice. When a cluster has no local class, these services fall back to `replicated-retain` (ADR 0008) and get the same snapshot-only jobs through per-PVC job labels.
  - `local` (one replica, `strict-local`, `reclaimPolicy: Delete`), for scratch data. No jobs.
- **Argo CD never prunes PVCs** of platform applications (`Prune=false` on PVCs), so removing an application from Git doesn't delete its data.

The other r1 block providers under ADR 0008 (vSphere CSI, the Proxmox CSI driver, ceph-csi and the pinned vendor SAN drivers) have no Longhorn-style backup to a target. On those, the platform's stateful services rely on their application-level backups (decisions 4, 5 and 7), the four classes keep the same names and roles (ADR 0008 says what backs each one), and backing up raw volumes is the site's array's job. Backing up tenant volumes on those providers waits for the tenant backup service (Velero), which comes after r1.

### 7. The other stateful services

| Service | What is backed up | How | Schedule and retention |
| --- | --- | --- | --- |
| SeaweedFS | Its buckets aren't backed up as a whole: SeaweedFS is a backup store. The buckets in the critical set (Terrakube state, CloudNativePG, etcd and other INFRA backups) are copied off site (decision 8). Its filer metadata is backed up | Replication `010` (a copy in another rack) when ADR 0010 gives at least two failure domains, `001` (another server) otherwise. Erasure coding (Reed-Solomon 10+4) for warm buckets of backups only, and only with at least four volume servers, so losing one server costs at most four of the fourteen shards. Filer metadata on CloudNativePG (decision 5) | Replication is continuous; erasure coding converts full, quiet volumes |
| OpenSearch | Indices | A snapshot repository on the object provider, and a Snapshot Management policy | Every day, 14 kept. Audit and security indices also off site, for as long as the site's retention policy says |
| Prometheus | Nothing by default. Metrics are rebuilt as clusters run | Local retention sized for 15 days. Long-term storage is a later decision | — |
| Forgejo | Its database (decision 5) and its repository and LFS volume | The volume on `replicated-retain`, backed up every day. A weekly `forgejo dump` to the object provider gives a portable copy. Restores are checked with `git fsck`. When the site has its own Git service (`spec.backup.gitMirror`), platform repositories are also push-mirrored there: a copy that needs no restore, and the first step if the site later takes Git over | Volume: 14 daily. Dump: 4 weekly, off site. Mirror: on every push |
| Terrakube | Its database (decision 5) and its state bucket | The bucket is versioned ([ADR 0006](0006-iac-path.md)) and copied off site | Bucket versions: 30 days |
| Zot and Artifact Keeper | Only what the site pushed: tenant and site-built images. Everything DCiaB ships is in the archive | Storage on the object provider. Those repositories' buckets are copied off site when the site asks | 30 days |

**The archive is the cold-start source for images.** After a full site loss, nothing needs INFRA's registry to come back: a bootstrap host serves the archive's images from its read-only Zot, as `dciab seed` does on Day 0. So the site keeps the archive of its current bundle version (the USB stick and one copy), and replaces it with every new bundle it imports.

### 8. Off-site copies are made by one copier per tier

Producers write once, to their primary target. A **copier** CronJob per tier copies the critical prefixes from the primary target to the off-site target every hour, with **rclone**, pinned and checksummed in `bundle.lock` (it handles both S3 and NFS):

- It copies; it never syncs deletions. Its off-site credentials can write but not delete. Off-site retention is enforced by the off-site store (lifecycle rules, and object lock or versioning where it has them). One store applies one retention, so it keeps everything for `offsite.retentionDays`, which must be at least the largest off-site number in this ADR (90 days, OpenBao's). The shorter per-item numbers are what restores are planned against.
- Its credentials, like the site target's, come from the config and are applied directly, never only from OpenBao, and are in the recovery bundle (read credentials too, so a restore can read the off-site copy).
- The off-site copy lags its source by up to an hour. For CloudNativePG this is the off-site recovery point; on site it is the WAL archive's.
- A site whose own S3 already replicates off site says so (`mode: site-replicated`, decision 9) and runs no copier.
- **A site with no second location uses removable media** (`mode: removable`). `dciab backup export <path>` writes the newest copy of the critical set and the recovery bundle to a disk the site carries away, as one archive age-encrypted to the recipients and the backup key. The config sets how often (`everyDays`, 7 by default). The last export's time is recorded on the site target, and `dciab status` warns when it's older than that. The off-site recovery point is then the last export, not the last hour.

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
        mode: copy                            # copy | site-replicated | removable | none
        # everyDays: 7                        # removable: how often to export
        url: s3://dciab-dr@dr.example.net/site-a
        credentials: {file: secrets/backup-offsite.env} # can write, can't delete
        readCredentials: {file: secrets/backup-offsite-read.env}
        retentionDays: 90                     # what the off-site store keeps; at least the largest off-site number above
        # reason: "no second location"        # required with mode: none
    tiers:
      mgmt: {target: site}                    # fixed; anything else is an error
      infra: {target: objectStore}            # objectStore (ADR 0008) or site
      workloads: {target: objectStore, volumeBackups: false}   # tenants opt volumes in
    key: {file: secrets/backup-key.pub}      # the backup key's public half (decision 10); generated if unset
    gitMirror:                               # optional: the site's own Git service
      url: https://git.example.net/dciab/
      credentials: {file: secrets/git-mirror.token}
    schedules:                                # DCiaB's defaults; every schedule and retention may be changed
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
- the site and off-site target credentials (and the S3 shim's), age-encrypted to the recipients;
- the backup key's private half, age-encrypted to the recipients;
- the newest CAPI export's location, the bundle version, and a manifest with every file's sha256;
- a canary file per recipient, each encrypted to that recipient alone.

The bundle holds only encrypted files; no plaintext ever leaves the bootstrap host. `dciab` writes it to the site target after `seed`, after `move` and at `finalize`, and again whenever its contents change, and the copier takes it off site. The site also keeps an offline copy with the archive.

**The backup key** is an age key pair that DCiaB generates at `seed` (unless the config gives its public half). Everything this ADR encrypts (etcd snapshots, CAPI exports, removable exports) is encrypted to it and to every recipient. Its private half is sealed to the recipients at once and goes into the recovery bundle; at the platform step it is also written to OpenBao's platform KV mount, readable only by the platform admin group and the restore runbooks' roles. So on-call staff restore with OpenBao's help, and recipients are needed only when OpenBao itself is lost. Replacing it is a new key pair: new backups use the new key, and the old private half stays in OpenBao and the bundle until the last backup encrypted to it expires.

**Custody of the recipients' identities** is the one thing DCiaB can't back up for the site, and it decides whether any of this can be restored:

- An age identity is never in the bundle, the config, Git or a cluster. Each recipient keeps theirs offline: on a hardware token through an age plugin, or printed and kept in a safe.
- At least two recipients keep their identities away from the site, in different places, so losing the site doesn't lose every identity.
- The site keeps a custody record (who holds which recipient, and where) outside DCiaB.
- Once a year, and whenever a recipient changes, each recipient decrypts their canary. When a recipient leaves, everything encrypted to all recipients is encrypted again to the new list (one current identity is enough), OpenBao's Shamir shares are rekeyed, and the backup key is replaced.

### 11. Restores are drilled, not assumed

A backup counts only once a restore from it has passed.

**In CI (kind),** with SeaweedFS in the kind cluster as the S3 target:

| Drill | Passes when | When |
| --- | --- | --- |
| etcd snapshot job | The snapshot decrypts with a test recipient identity and with the test backup key, restores with `etcdutl` into a standalone etcd, and a marker key reads back | Every pull request that touches backup code |
| CAPI export and import | The e2e management cluster's objects export with `--to-directory`, import into a second kind cluster with `--from-directory`, and `hack/e2e/verify.py` passes there | Every pull request that touches backup code |
| CloudNativePG | A base backup and WAL go to the `ObjectStore`, a new Cluster recovers to a target time, and the rows written before it are there and those after aren't | Nightly |
| OpenBao | A raft snapshot from the snapshot agent restores into a new OpenBao, which unseals with the original keys and reads a known secret | Nightly |
| Recovery bundle | The bundle's manifest checks out and each canary decrypts with its test identity | Every pull request that touches backup code |

**As release-gate drills on the reference platforms** (vSphere and Proxmox), because they need real disks, hypervisors or quorum holders:

| Drill | When |
| --- | --- |
| Longhorn: back up a volume, restore it to a new PVC, compare checksums | With the first Longhorn RecurringJobs, then before each release |
| MGMT etcd restore on all members | Before each release; a person approves, as it stops MGMT |
| MGMT rebuilt from the archive, the config, the recovery bundle and the newest CAPI export | Before each release, and in the `r1` gate |
| OpenBao restore with a Shamir quorum | With the platform step, then before each release |
| An NFS-only site target: OpenBao and CloudNativePG back up through the S3 shim, and restore from a new shim on the same export | Before each release |
| Full-site cold start: every host powered off, then brought back in the documented order | The `r1` gate |

**At a site,** an in-cluster restore canary restores the newest CloudNativePG backup and one Longhorn backup to a scratch namespace every week, checks them and deletes them. A failed canary is an alert. People drill an OpenBao restore every quarter and a cold start every year.

Every backup job and the copier export the time of their last success. Alertmanager alerts when that's older than twice the schedule ([ADR 0007](0007-detection-to-response.md), decision 4), and `dciab status` shows the age of the newest backup of each kind.

## Alternatives considered

- **MGMT backs up to INFRA's SeaweedFS.** One S3 for everything, but restoring either cluster then needs the other. Rejected: MGMT uses the site target, always.
- **Velero for everything** (cluster objects, and volumes through CSI snapshots or file copy). It's one tool, but its volume backups of databases are crash-consistent with no point-in-time recovery, restoring CAPI objects needs a careful order and loses `status`, and it adds a stateful component to every cluster. Rejected as the platform's mechanism. Velero becomes a DCiaB service for tenant workload backups after r1.
- **etcd snapshots from a systemd timer on each control plane node,** set by the ClusterClass. It runs even when the API doesn't, but needs a new class revision and a node roll on every cluster, and a snapshot from a failing cluster isn't useful. Rejected for the CronJob.
- **Only etcd snapshots for MGMT, or only CAPI exports.** An etcd restore needs MGMT's VMs and addresses; an export alone loses `status` and depends on `move` for something it wasn't designed for. Both are kept, for different failures.
- **CloudNativePG's in-tree `barmanObjectStore`.** Simpler today, but deprecated since 1.26 and removed in 1.32. Rejected; DCiaB starts on the plugin.
- **Longhorn volume backups for databases and OpenBao.** Uniform, but crash-consistent, with no point-in-time recovery. Rejected for services with their own backups.
- **Automated raft snapshots inside OpenBao.** Vault's automated snapshots (`sys/storage/raft/snapshot-auto`) are a Vault Enterprise feature; OpenBao documents the CronJob instead.
- **Each producer writes to both targets.** No copier, but every tool needs a second target and credentials, and some (Longhorn's recurring jobs, CloudNativePG's WAL archiver) take only one. Rejected for one copier per tier.
- **Require S3 from sites for OpenBao and CloudNativePG.** Nothing new runs, but it takes back "S3 or NFS" for every site, since every site runs OpenBao. Rejected for the shim (decision 4a).
- **On NFS-only sites, write to INFRA's SeaweedFS and copy files to NFS.** Nothing new on MGMT, but the primary copy is back on INFRA's hardware, every recovery point gains the copy's delay, and a CloudNativePG restore still needs an S3 endpoint. Rejected for the shim.
- **Encrypt backups to the recipients only.** Simplest, but every restore then needs a recipient, even a routine one. Rejected for the backup key (decision 10).
- **Off-site copies optional.** Simpler for sites without a second location, but the default would be no disaster recovery. Rejected: off site is required. A site with no second location uses removable media, and a site that has neither says so in the config (`mode: none` with a reason).

## Consequences

- A site needs S3 or NFS for the site target (as today) and, unless it records otherwise, an off-site target with write-only credentials or a removable-media routine.
- `dciab` gains backup jobs on every cluster, a copier per tier, the recovery bundle, and pre-change snapshots in every step that changes a control plane. Each job's tool (rclone included) is pinned and checksummed in `bundle.lock` and is in the archive.
- ADR 0005 is amended (below): OpenBao snapshots go to the site target, not SeaweedFS, and backup-target credentials reach clusters without ESO.
- On a site whose target is NFS, MGMT runs one more stateful service, the S3 shim (decision 4a).
- Restoring an etcd snapshot or a CAPI export needs the backup key from OpenBao, or a recipient's identity when OpenBao is lost. Opening the recovery bundle always needs a recipient. Custody of those identities becomes part of operating a site, with a yearly check.
- Tenants get etcd snapshots of their clusters by default, but their volumes are backed up only when they opt in, until the tenant backup service arrives after r1.
- Retain classes leave volumes behind after their PVCs go. Removing platform data becomes a deliberate, manual step, and capacity planning must allow for released volumes until someone deletes them.
- Off-site recovery points lag by up to the copier's interval (one hour by default).
- Restore drills join CI (kind), the release-gate drills, and the `r1` gate.

## Amendment to ADR 0005 (2026-11-23)

**Context.** [ADR 0005](0005-secrets.md), decision 1 keeps OpenBao's Raft snapshots on "S3 on SeaweedFS, plus a copy that leaves the site". SeaweedFS runs on INFRA, and its credentials come from OpenBao through ESO, so restoring OpenBao would need a service that itself depends on OpenBao. ADR 0005's Consequences also say that nothing in Git or Helm values holds a secret, and that everything is an `ExternalSecret` reference or an OpenBao path.

**Decisions.**
- In ADR 0005, decision 1, the words "kept off the cluster (S3 on SeaweedFS, plus a copy that leaves the site)" now read: kept off the cluster on the **site target** (the site's S3, or the S3 shim in front of its NFS export, decision 4a), plus the off-site copy (decision 8). They never go to INFRA's SeaweedFS. Schedule and retention are in decision 4.
- An exception to ADR 0005, decision 3 and its Consequences: the credentials of the site and off-site targets (and of the shim) don't come from OpenBao through ESO. They come from the config's `{file: ...}` or `{env: ...}` references and DCiaB applies them as Kubernetes Secrets directly, as `addons/longhorn` does today. A copy is in the recovery bundle (decision 10). Every other secret still follows ADR 0005.

**Consequences.** Restoring OpenBao needs only the site target, the recovery bundle and the key holders. The backup-target credentials are the one set of platform secrets that isn't rotated through OpenBao; the site rotates them in its own storage and updates the config.

## Resolved questions

Trevor decided these on 2026-11-23; the decisions above include them.

1. **Sites with no second location:** `offsite: removable` is allowed, with a schedule and a `dciab status` reminder (decision 8), because carried-away media is still a copy off the site.
2. **Retention:** 14 days on site and 30 to 90 days off site stay the defaults, configurable in `spec.backup` (decision 9), because sites' compliance needs differ.
3. **Who can restore:** backups are encrypted to the recipients and to a separate backup key whose private half is in OpenBao and the recovery bundle (decision 10), so routine restores don't need a recipient.
4. **Workload clusters:** etcd snapshots are on by default and volume backups are opt-in per tenant (decisions 2 and 6), because a cluster's state is cheap to keep and tenant data is the tenant's choice.
5. **Forgejo:** platform repositories are push-mirrored to the site's own Git service when one is configured (decision 7), which gives a copy that needs no restore and eases pivoting Git to the site.
6. **Velero for tenants:** a DCiaB service later, not r1, because the platform's own backups come first.
7. **The copier's tool:** rclone, pinned and checksummed in `bundle.lock` (decision 8), because one tool covers S3 and NFS.
8. **NFS-only sites:** an S3 shim (SeaweedFS) on MGMT backed by the site's NFS export (decision 4a), because it keeps "S3 or NFS" and keeps the primary copy off INFRA.
9. **Storage classes:** `replicated`, `replicated-retain`, `local` and `local-retain` (decision 6); platform data that keeps its own copies uses `local-retain`, and scratch uses `local`, so no platform data is lost with its PVC.

## References

- CloudNativePG, Barman Cloud plugin: [migrating from the in-tree support](https://cloudnative-pg.io/plugin-barman-cloud/docs/migration/) (deprecated in 1.26, removed in 1.32; `ObjectStore`, `isWALArchiver`, `ScheduledBackup` `method: plugin`, retention on the `ObjectStore`) and [introduction](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/).
- Longhorn: [setting a backup target](https://longhorn.io/docs/snapshots-and-backups/backup-and-restore/set-backup-target/) (multiple targets since 1.8, the `default` target, `defaultBackupStore`); [multiple backup stores test plan, v1.8.0](https://longhorn.github.io/longhorn-tests/manual/release-specific/v1.8.0/test-multiple-backup-stores) (`backupTargetName` on StorageClasses and volumes); [recurring snapshots and backups](https://longhorn.io/docs/snapshots-and-backups/scheduling-backups-and-snapshots/) (RecurringJob tasks, `retain`, groups, the `default` group, `recurringJobSelector`).
- OpenBao: [snapshot CronJob](https://openbao.org/docs/platform/k8s/helm/examples/snapshot-cronjob/) (`openbao-snapshot-agent`, Kubernetes auth, read on `sys/storage/raft/snapshot`, S3 upload); [raft storage](https://openbao.org/docs/configuration/storage/raft/).
- Vault: [automated raft snapshots API](https://developer.hashicorp.com/vault/api-docs/system/storage/raftautosnapshots) (Vault Enterprise).
- Cluster API: [`clusterctl move`](https://cluster-api.sigs.k8s.io/clusterctl/commands/move) (`--to-directory` and `--from-directory` replace `clusterctl backup` and `restore`; not designed as a backup tool; `status` isn't restored).
- Kubernetes: [operating etcd clusters](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/) (`etcdctl snapshot save`, `etcdutl snapshot restore`, restoring every member, encrypting snapshot files).
- SeaweedFS: [replication](https://github.com/seaweedfs/seaweedfs/wiki/Replication) (the `xyz` replication string, `defaultReplication`); [erasure coding](https://deepwiki.com/seaweedfs/seaweedfs/4.4-erasure-coding) (Reed-Solomon 10+4, up to four lost shards, volumes read-only after encoding).
