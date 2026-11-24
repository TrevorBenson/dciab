# Backups

What DCiaB backs up, where to, how often, and how to restore it. The design and its reasons are in [ADR 0009](adr/0009-backup-and-dr.md); this page says what is built and how to use it.

## The config: `spec.backup`

```yaml
spec:
  backup:
    targets:
      site:                                   # required
        url: s3://dciab-backup@s3.example.net/site-a   # or nfs://server:/export
        credentials: {file: secrets/backup-site.env}    # S3 only
        caBundle: {file: secrets/backup-site-ca.pem}    # S3 only, optional: the endpoint's private CA
      offsite:                                # required
        mode: copy                            # copy | site-replicated | removable | none
        url: s3://dciab-dr@dr.example.net/site-a
        credentials: {file: secrets/backup-offsite.env} # can write, can't delete
        retentionDays: 90
    tiers:                                    # optional; these are the defaults
      mgmt: {target: site}                    # always site
      infra: {target: objectStore}            # objectStore or site
      workloads: {target: objectStore, etcdSnapshots: true, volumeBackups: false}
    key: {publicKey: age1...}                 # optional: the backup key's public half
    schedules: {}                             # optional: see Schedules and retention
```

### The two targets

- **The site target** is the site's own S3 bucket or NFS export, outside DCiaB's clusters. MGMT always backs up there. S3 credentials are `KEY=VALUE` lines with at least `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (`AWS_ENDPOINTS` overrides the `https://` endpoint). They reach clusters as Kubernetes Secrets applied server-side from the config's reference, never through OpenBao, so a restore needs only the config and the site target (ADR 0009, amendment to ADR 0005).
- **The off-site target** holds the copy that survives losing the site:

  | `mode` | What it is | Fields |
  | --- | --- | --- |
  | `copy` | S3 or NFS at another location | `url`; for S3 `credentials` (write, not delete) and optionally `readCredentials`; `retentionDays` (default 90) |
  | `site-replicated` | The site target already replicates off site | none |
  | `removable` | An encrypted export to a disk the site carries away | `everyDays` (default 7), `retentionDays` |
  | `none` | No off-site copy | `reason`, required |

  The off-site store keeps everything for `retentionDays`; `validate` warns below 90 days, OpenBao's off-site number.

### Where each tier backs up

| Tier | Target | Rule |
| --- | --- | --- |
| MGMT | The site target | Fixed. Restoring INFRA needs MGMT, so MGMT never backs up to INFRA's object store. `validate` rejects anything else |
| INFRA | `objectStore` (SeaweedFS on INFRA, the default) or `site` | With `offsite.mode: none`, `validate` requires `site`: the critical set needs a copy off INFRA's hardware |
| Workload clusters | `objectStore` (the default) or `site` | etcd snapshots on by default (`etcdSnapshots`); volume backups opt-in (`volumeBackups`) |

### What `dciab validate` checks

Errors: a missing site target; an S3 target without credentials, or an NFS target with any; MGMT's tier not `site`; `offsite.mode: none` without a `reason`; INFRA on its own object store with no off-site copy; an off-site target equal to the site target; a backup key that is one of `spec.bootstrap.recipients`. Warnings, on every run: `offsite.mode: none`; an NFS off-site target (it can't be made write-once); `retentionDays` below 90; `workloads.etcdSnapshots: false`. A note says when the site target is NFS (OpenBao and CloudNativePG will need the S3 shim of ADR 0009, decision 4a).

### The old form

A config without `spec.backup` still works for one minor release: the site target is MGMT's Longhorn `backupTarget` and `backupCredentials`, and there is no off-site copy. `validate` warns and prints the new form. With `spec.backup` set, a Longhorn `backupTarget` is ignored (a warning); on MGMT, one that differs from the site target is an error.

## Layout on the site target

DCiaB writes only under `dciab/`, so the target can hold other data. Longhorn keeps its own layout at the target's root.

```text
backupstore/...                 Longhorn's volume backups, from a cluster that runs Longhorn
dciab/<cluster>/etcd/...        etcd snapshots, age-encrypted
dciab/mgmt/capi/...             CAPI exports, age-encrypted
dciab/recovery/...              the backup key's private half, sealed to the recipients
```

## Schedules and retention

Every schedule and retention below is a default; `spec.backup.schedules` changes any of them. Crons are in UTC.

```yaml
schedules:
  etcd:
    mgmt:      {cron: "0 * * * *",   keep: {hourly: 24, daily: 14}}
    infra:     {cron: "0 * * * *",   keep: {hourly: 24, daily: 14}}
    workloads: {cron: "0 */6 * * *", keep: {days: 7}}
  capiExport:  {cron: "30 2 * * *",  keep: {daily: 14}}
  longhorn:
    snapshot:  {cron: "0 * * * *",   retain: 24}
    backup:    {cron: "0 1 * * *",   retain: 14}
```

`keep` rules: `hourly: N` keeps the newest N; `daily: N` keeps the newest of each day for N days; `days: N` keeps everything from the last N days. A copy any rule keeps is kept. Jobs prune by these rules themselves and never rely on bucket lifecycle rules, which a site's S3 may not allow and NFS doesn't have.

## Volumes

### Longhorn

Longhorn's jobs follow the cluster that runs Longhorn, from that cluster's block storage settings; DCiaB never assumes a cluster runs it. When a cluster's block storage is Longhorn, its default backup target is its tier's target (MGMT's: the site target), and `addons/longhorn/install.sh --cluster <name>` applies two `RecurringJob`s server-side after the StorageClasses. Each StorageClass puts its volumes in a group through its `recurringJobSelector`:

| Class | Group | MGMT and INFRA | Workload clusters |
| --- | --- | --- | --- |
| `replicated` | Longhorn's `default` (no selector) | snapshot hourly (24), backup daily (14) | nothing, unless `volumeBackups: true` |
| `replicated-retain` | `platform` | snapshot hourly, backup daily | snapshot hourly, backup daily |
| `local-retain` | `local` | snapshot hourly only: these services back themselves up | snapshot hourly only |
| `local` | `scratch` | nothing | nothing |

On a workload cluster a tenant opts a volume in to the `backup` group (the same schedule) with Longhorn's PVC labels: `recurring-job.longhorn.io/source: enabled` and `recurring-job-group.longhorn.io/backup: enabled`.

A volume created before its class had a selector has no group, so it is in the `default` group. On MGMT and INFRA that group is backed up; label a `local` or `local-retain` volume made earlier if it shouldn't be.

### The site's array

On vSphere CSI, the Proxmox CSI driver and the other array providers, DCiaB backs up no raw volumes: there is no Longhorn-style backup to a target. The platform's stateful services rely on their own backups (OpenBao snapshots, CloudNativePG base backups and WAL), and backing up raw volumes is the array's job, with its own snapshots and replication. Tenant volumes on an array wait for the tenant backup service (Velero), after v1 (ADR 0009, decision 6).

## Workload clusters

`spec.backup.tiers.workloads` is in the schema and checked by `validate`. DCiaB doesn't build workload clusters yet, so nothing installs their backups; when it does, each gets the etcd snapshot job (unless `etcdSnapshots: false`) and, on Longhorn, the recurring jobs above.

## Not built yet

- The off-site copier (rclone, ADR 0009, decision 8): until it exists, nothing is copied off site; `validate` says so.
- `dciab backup export` for `mode: removable`, and the `dciab status` reminder when the last export is old.
- The S3 shim for NFS-only sites (decision 4a), which comes with OpenBao and CloudNativePG.
- `validate --live` checks that both targets answer.
- Backups of INFRA's services, Forgejo's push mirror (`gitMirror`) and the recovery bundle.
