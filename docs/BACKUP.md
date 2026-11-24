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
  An NFS export is mounted by the backup jobs' pods (and by `dciab mgmt` on the bootstrap host, as root, for the exports around the pivot). The jobs write as root, as Longhorn does, so with `root_squash` the export's folder must be writable by the squashed user.
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

A config without `spec.backup` still works for one minor release: the site target is the Longhorn `backupTarget` and `backupCredentials` (MGMT's when `overrides.mgmt` gives it Longhorn, otherwise `spec.storage.block`'s or the old `spec.storage.longhorn`'s), and there is no off-site copy. `validate` warns and prints the new form. With `spec.backup` set, a Longhorn `backupTarget` is ignored (a warning); on MGMT, one that differs from the site target is an error.

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

Longhorn's jobs follow the cluster that runs Longhorn, from that cluster's block storage settings; DCiaB never assumes a cluster runs it. MGMT runs it only when `spec.storage.block.overrides.mgmt` asks (MGMT keeps no volumes, ADR 0008 decision 4a), so by default MGMT has no Longhorn jobs, and its etcd snapshots and CAPI exports are its backups. When a cluster's block storage is Longhorn, its default backup target is its tier's target (MGMT's: the site target), and `addons/longhorn/install.sh --cluster <name>` applies two `RecurringJob`s server-side after the StorageClasses. Each StorageClass puts its volumes in a group through its `recurringJobSelector`:

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

## MGMT's etcd and Cluster API state

`dciab mgmt` sets these up; every one is age-encrypted to `spec.bootstrap.recipients` **and** to the backup key before it leaves the machine that made it. A cluster holds only public keys, so a compromised cluster can't read its old backups.

| What | When | Where | Kept (default) |
| --- | --- | --- | --- |
| CAPI export of the bootstrap cluster | The `move` step, before `clusterctl move`. The move doesn't start until the export is on the site target | `dciab/mgmt/capi/`, and `<state>/capi-backup/` | `capiExport.keep` |
| CAPI export of MGMT | The `adopt` step, once MGMT is moved; a later run retries a failed one (`<state>/capi-backup/LAST` records it) | the same | the same |
| etcd snapshot of MGMT | CronJob `dciab-backup/etcd-snapshot`, every hour | `dciab/mgmt/etcd/` | 24 hourly, 14 daily |
| CAPI export of MGMT | CronJob `dciab-backup/capi-export`, every day at 02:30 UTC | `dciab/mgmt/capi/` | 14 daily |

The `backups` step (the last one) first renders the jobs, so an unpinned image stops it before anything is applied. Then it applies the CronJobs, a `ConfigMap` of the public keys, and, for an S3 target, the Secret `dciab-backup-target`, piped from the config's reference into `kubectl apply --server-side`. The jobs run DCiaB's backup image (`images/backup`: age, rclone, kubectl, clusterctl and `lib/dciab_backup/runner.py`), which CI builds and publishes and `bundle.lock` pins as `images.backup`; the step refuses to run until it is pinned.

**etcd snapshots.** A CronJob on a control plane node, as ADR 0009 decision 2 chose over a systemd timer: it needs no new ClusterClass revision and no node roll, and it runs whenever the API works, which is when a snapshot is worth having. With host networking it runs `etcdctl snapshot save` against `https://127.0.0.1:2379`, using the cluster's own etcd image (the `registry.k8s.io/etcd` pin in `bundle.lock`). It authenticates with kubeadm's etcd client certificate. Only `ca.crt` and `healthcheck-client.{crt,key}` are mounted, read-only; etcd's CA key and its server and peer keys never are. The upload container checks the file with `etcdutl snapshot status` (the same etcd version, built into the backup image), then encrypts it, uploads it and prunes. The snapshot is staged on the node's own disk (an `emptyDir`) and deleted when the upload container ends, whether it succeeded or not. If that container never starts (its image can't be pulled, for example), the plaintext snapshot stays in the failed pod's folder on the node, readable by root only. It is removed with the Job: the CronJob keeps one failed Job, deleted after 6 hours. The job never needs a StorageClass: by default MGMT runs no block storage at all.

**CAPI exports.** `clusterctl move --to-directory` of every namespace that holds a Cluster, as one tar. The job waits up to 30 minutes for every Machine to be `Running` and none to be deleting, because `move` expects stable clusters. An export pauses each Cluster and its ClusterClass while it reads, and clusterctl resumes them only on success. Any Cluster or ClusterClass the export left paused is resumed, and the job fails so that someone checks it. A paused ClusterClass would otherwise stop topology reconciliation, and fail every later export. `move` never restores `status`, so an export is the fallback, for when MGMT's etcd can't be restored.

**The backup key.** With `spec.backup.key.publicKey` set, that key is used. Without it, the `move` step generates an age key pair the first time it needs one. The private half is never written in plaintext: it is sealed to the recipients at once, and kept as `<state>/backup/backup-key.age` on the bootstrap host and as `dciab/recovery/backup-key-<public key>.age` on the site target. Every `dciab mgmt` run checks that the sealed copy is on the target, and uploads it (with retries) if it isn't. Nothing is encrypted to the key, and nothing moves, until it is there. `dciab mgmt` prints the public half; put it in the config, so a new bootstrap host keeps using it. **Until OpenBao exists, those two sealed files are the only copies of the private half**, so every restore needs a recipient's identity. With OpenBao, the private half also goes to its platform KV mount, and on-call staff restore without a recipient (ADR 0009, decision 10).

**Retention and failures.** Each job prunes its own folder by its `keep` rules after a successful upload, and only then. A CronJob keeps the last three successful Jobs and one failed Job, each for 6 hours, so `kubectl -n dciab-backup logs job/<name>` shows what happened.

## Where backups stand

`dciab status` has a `backups` line. It shows when each MGMT CronJob last succeeded, Longhorn's newest backup, and the CAPI export `dciab mgmt` took around the pivot. It reports `down` when a CronJob's last scheduled run is more than an hour old and no success came after it. For more detail:

```console
$ kubectl -n dciab-backup get cronjobs     # LAST SCHEDULE; .status.lastSuccessfulTime
$ kubectl -n dciab-backup get jobs
```

## Restore

Every restore starts with a backup and a key that opens it: a recipient's identity, or the backup key, which you open first with a recipient's identity. Keep plaintext in memory (`/dev/shm`) or on the machine that needs it, never on shared storage.

```console
$ age -d -i <recipient identity> -o /dev/shm/backup-key.txt backup-key-<public key>.age   # only to use the backup key
$ age -d -i /dev/shm/backup-key.txt -o /dev/shm/etcd.db etcd-mgmt-<time>.db.age      # or -i <recipient identity>
```

Fetch a backup with the site's own tools: `rclone copy` (or any S3 client) for S3, or mount the NFS export. The newest file sorts last in `dciab/mgmt/etcd/` and `dciab/mgmt/capi/`.

### MGMT's etcd, from a snapshot

The first choice whenever MGMT's VMs still exist. It stops MGMT: Cluster API, and every cluster MGMT manages, are unmanaged until it ends. It follows the Kubernetes procedure: every member is restored from the same snapshot.

1. **Choose and check the snapshot.** Decrypt it, then `etcdutl snapshot status /dev/shm/etcd.db --write-out=table`. Anything created in MGMT after it was taken is lost: Machines created later are VMs that nothing manages any more. Note them, so you can delete them afterwards.
2. **Stop the control plane on every control plane node.** Move the static pod manifests aside and wait for the containers to stop:
   ```console
   # mkdir -p /root/held && mv /etc/kubernetes/manifests/{kube-apiserver,kube-controller-manager,kube-scheduler,etcd}.yaml /root/held/
   # crictl ps | grep -E 'etcd|kube-apiserver'      # until empty
   ```
3. **Restore every member.** Copy the snapshot to every control plane node. On each node, read `--name` and `--initial-advertise-peer-urls` from `/root/held/etcd.yaml`, then:
   ```console
   # mv /var/lib/etcd /var/lib/etcd.before-restore
   # etcdutl snapshot restore /dev/shm/etcd.db --data-dir /var/lib/etcd \
       --name <this node's --name> \
       --initial-advertise-peer-urls https://<this node's IP>:2380 \
       --initial-cluster <node1>=https://<ip1>:2380,<node2>=https://<ip2>:2380,<node3>=https://<ip3>:2380 \
       --initial-cluster-token mgmt-restore-<date>
   # restorecon -R /var/lib/etcd          # SELinux-enforcing nodes
   ```
   `etcdutl` is in the etcd image `bundle.lock` pins. Nodes don't carry it, so copy it out of the image on the bootstrap host (`podman create` the image, then `podman cp <container>:/usr/local/bin/etcdutl .`) and onto each node.
4. **Start etcd, then the rest.** Move `etcd.yaml` back on every node and wait for `etcdctl endpoint health --cluster` (with the client certificate in `/etc/kubernetes/pki/etcd`) to report every member healthy. Then move the other three manifests back, and `systemctl restart kubelet` on every node.
5. **Check.** `kubectl get nodes`; `kubectl -n cluster-mgmt get cluster,machines`; `clusterctl describe cluster mgmt -n cluster-mgmt`. Delete the unmanaged VMs from step 1. Once all is well, remove `/var/lib/etcd.before-restore` and `/dev/shm/etcd.db`.

### Cluster API state, from an export

`clusterctl move --from-directory` recreates every object of an export in the cluster `--to-kubeconfig` names, then resumes the Clusters. It ignores `--kubeconfig`, and without `--to-kubeconfig` it uses `$KUBECONFIG` or `~/.kube/config`, which may be another cluster: always pass `--to-kubeconfig`. It doesn't restore `status`, so controllers rebuild it.

**The bootstrap host was lost before or during the move.** MGMT's VMs run, but the kind cluster that managed them is gone. Use the export taken before the move. Import it paused, check it, and only then resume it: until then, nothing acts on MGMT's VMs.

1. Bring up a new bootstrap host with the same config and run `dciab seed`. It makes a new kind cluster with Cluster API, the providers and the ClusterClasses.
2. Decrypt and unpack the newest `capi-mgmt-<time>.tar.age` (`age -d ... | tar -x`). This gives `capi/cluster-mgmt/`.
3. Annotate every object in it with Cluster API's pause annotation:
   ```console
   $ PYTHONPATH=lib python3 -m dciab_backup pause-export capi/cluster-mgmt
   ```
   clusterctl resumes the Clusters' `spec.paused` and the ClusterClasses after an import, but it leaves objects' annotations alone. Cluster API, the kubeadm providers and the infrastructure providers skip every annotated object.
4. Import it into the new bootstrap cluster:
   ```console
   $ clusterctl move --from-directory capi/cluster-mgmt --to-kubeconfig <state>/bootstrap.kubeconfig
   ```
5. Check: `kubectl -n cluster-mgmt get cluster mgmt,machines` lists every MGMT machine, and every object in `cluster-mgmt` still carries `cluster.x-k8s.io/paused`. This lists any that don't, and must print nothing:
   ```console
   $ kinds="$(kubectl api-resources --namespaced -o name | grep -E '\.(x-k8s\.io)$' | paste -sd,)"
   $ kubectl -n cluster-mgmt get "$kinds" -o json | python3 -c 'import json,sys; [print(o["kind"], o["metadata"]["name"]) for o in json.load(sys.stdin)["items"] if "cluster.x-k8s.io/paused" not in (o["metadata"].get("annotations") or {})]'
   ```
6. Resume, ClusterClass first, then every other object, then the Cluster last:
   ```console
   $ export KUBECONFIG=<state>/bootstrap.kubeconfig
   $ kubectl -n dciab-clusterclasses annotate clusterclass --all cluster.x-k8s.io/paused-
   $ kinds="$(kubectl api-resources --namespaced -o name | grep -E '\.(x-k8s\.io)$' | grep -v '^clusters\.' | paste -sd,)"
   $ kubectl -n cluster-mgmt annotate "$kinds" --all cluster.x-k8s.io/paused-
   $ kubectl -n cluster-mgmt annotate cluster mgmt cluster.x-k8s.io/paused-
   ```
   Watch that no Machine is replaced (`kubectl -n cluster-mgmt get machines -w`). Then re-run `dciab mgmt`: it finds Cluster mgmt on the bootstrap cluster, unpaused, and moves it to MGMT.

**MGMT was lost, with its VMs.** Rebuild MGMT from the archive and the config (`dciab seed`, then `dciab mgmt`). Once MGMT manages INFRA and workload clusters, import the newest export's namespaces for those clusters into the new MGMT the same way: paused (step 3), imported with `--to-kubeconfig` set to MGMT's kubeconfig, checked, then resumed. Don't import `cluster-mgmt`: the rebuilt MGMT has its own.

Never import an export into a management cluster while another one still manages the same Clusters: both would act on the same VMs.

## Workload clusters

`spec.backup.tiers.workloads` is in the schema and checked by `validate`: etcd snapshots are on by default (every 6 hours, 7 days kept), and volume backups are opt-in. DCiaB doesn't build workload clusters yet, so nothing installs their backups. When it does, each gets the etcd snapshot job (unless `etcdSnapshots: false`) and, on Longhorn, the recurring jobs above.

## Not built yet

What ADR 0009 decides that DCiaB doesn't do yet:

- The off-site copier (rclone, ADR 0009, decision 8): until it exists, nothing is copied off site; `validate` says so.
- `dciab backup export` for `mode: removable`, and the `dciab status` reminder when the last export is old.
- The S3 shim for NFS-only sites (decision 4a), which comes with OpenBao and CloudNativePG.
- `validate --live` checks that both targets answer.
- INFRA's backups (etcd, its services), Forgejo's push mirror (`gitMirror`) and the recovery bundle.
- Workload clusters' etcd snapshot jobs (the schema and `validate` are ready).
- A snapshot before every control plane change (an upgrade, a class revision change), and the restore drills in CI and at release.
