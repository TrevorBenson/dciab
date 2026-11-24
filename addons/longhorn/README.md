# Longhorn

Longhorn is DCiaB's own block storage (CSI), the `longhorn` provider of `spec.storage.block` ([ADR 0008](../../docs/adr/0008-provider-pattern.md)), for clusters on local disks. A site with an array that replicates (vSAN, Ceph, a SAN) uses the array's CSI driver instead ([addons/csi](../csi/README.md)), so data isn't replicated twice. Volumes are replicated across nodes. Longhorn's default backup target is the cluster's backup target from `spec.backup` (MGMT: the site target), and recurring jobs snapshot and back up volumes by StorageClass ([docs/BACKUP.md](../../docs/BACKUP.md#longhorn)).

`install.sh` installs or upgrades Longhorn on one cluster:
- **Version and chart source:** `bundle.lock` (`charts.longhorn`, `chartRepositories.longhorn`). The chart's sha256 is checked against `chartDigests.longhorn`; a mismatch stops the install.
- **Chart, offline:** `--chart <tgz>`, else `$DCIAB_PAYLOAD/charts/longhorn-<version>.tgz` when `DCIAB_PAYLOAD` is set (the archive vendors the chart), else the pinned repository.
- **DCiaB defaults:** `values.yaml`: replica data lives in `/var/lib/longhorn` (image-mode nodes have read-only `/usr` and `/opt`), the chart's own `longhorn` StorageClass isn't the default, and there is no ingress.
- **Per-site values:** from the cluster's Longhorn settings (`spec.storage.block.longhorn`, with the tier's override; for workload clusters only `overrides.workload`, as they don't inherit `block`; `longhorn_values.py`): `replicaCount` sets Longhorn's default replica count. The default backup target is the cluster's target from `spec.backup` (MGMT: `targets.site`); a config without `spec.backup` still uses the Longhorn settings' `backupTarget`. The old `spec.storage.longhorn` still works for one minor release; `dciab validate` warns and prints the new form.
- **StorageClasses:** after the chart, [`lib/steps/storage-classes.sh`](../../lib/steps/storage-classes.sh) applies DCiaB's four classes, the same names on every cluster whatever backs them:

  | Class | Replicas | Data locality | Reclaim | For |
  | --- | --- | --- | --- | --- |
  | `replicated` (the default) | `replicaCount` | any node | Delete | ordinary volumes |
  | `replicated-retain` | `replicaCount` | any node | Retain | platform data with no copies of its own (Forgejo, AWX projects) |
  | `local` | 1 | `strict-local`, bound when the pod is placed | Delete | scratch |
  | `local-retain` | 1 | `strict-local`, bound when the pod is placed | Retain | platform data that keeps its own copies (CloudNativePG, OpenBao, OpenSearch, SeaweedFS) |

  Every class but `replicated` names a recurring-job group in its `recurringJobSelector` (`platform`, `local`, `scratch`); `replicated`'s volumes are in Longhorn's `default` group.

  A StorageClass's parameters can't change in place, so a class whose parameters changed is deleted and created again; volumes made from it keep working. Any other class marked default stops being one.
- **Recurring jobs:** then `install.sh` applies the `RecurringJob`s `dciab-snapshot` and `dciab-backup` server-side, from `spec.backup.schedules.longhorn` (`python3 -m dciab_backup longhorn-jobs`). Which groups each covers is in [docs/BACKUP.md](../../docs/BACKUP.md#longhorn).
- **Data disk:** on vSphere, Longhorn needs `dataDiskGiB` on the nodes it runs on (the worker pools, or the control plane when there are no workers). The class mounts that disk at `/var/lib/longhorn`, so replicas never share the root disk with etcd, and a full Longhorn disk can't fill `/var`. CAPMOX can't add a second disk, so on Proxmox Longhorn shares the root disk, and `dciab validate` warns: use the Proxmox CSI driver there.
- **Control plane nodes:** when MGMT has no worker nodes, its control plane nodes run workloads: they carry no `node-role.kubernetes.io/control-plane` taint ([capi-cluster](../../charts/capi-cluster/README.md#clusters-without-workers)), and Longhorn also tolerates that taint there. Give the control plane `dataDiskGiB` then (on vSphere). With 3 control plane nodes, a `replicaCount` of 3 puts one replica on each; a volume needs as many nodes as replicas, so it stays degraded while one node is down or being replaced.
- **UI and API:** the UI and the manager's API are ClusterIP Services without authentication. Restrict them with network policy, and front the UI with the platform's gateway and SSO, when those exist.

```console
$ addons/longhorn/install.sh <config> <kubeconfig> [--cluster NAME]   # install or upgrade (default: mgmt), wait for the manager, apply the StorageClasses and recurring jobs
$ addons/longhorn/install.sh <config> --print-values   # show the per-site values
$ addons/longhorn/install.sh <config> --template       # render the manifests; needs no cluster
$ addons/longhorn/install.sh <config> --images         # every image the release runs (for the archive inventory)
```

## Backup targets

- **NFS** (`nfs://<server>:/<export>`): passed to Longhorn as is. No credentials.
The target is `spec.backup.targets.site` on MGMT ([docs/BACKUP.md](../../docs/BACKUP.md)); its `credentials` and `caBundle` fill the Secret below. Longhorn writes under `backupstore/` at the target's root.

- **S3** (`s3://<bucket>@<endpoint>[/<path>]`): Longhorn's own form is `s3://<bucket>@<region>/<path>`, with the endpoint in the credentials Secret, so `install.sh` sets the target to `s3://<bucket>@<region>/<path>`: the region of an AWS endpoint (`s3.<region>.amazonaws.com`), else `us-east-1`, which S3-compatible stores ignore, and the Secret's `AWS_ENDPOINTS` to `https://<endpoint>`. The credentials (`{file: ...}` or `{env: ...}`) hold `KEY=VALUE` lines:

  ```sh
  AWS_ACCESS_KEY_ID=...
  AWS_SECRET_ACCESS_KEY=...
  # optional: AWS_ENDPOINTS=http://s3.example.net:8333 overrides the https:// endpoint
  ```

  A `caBundle` becomes the Secret's `AWS_CERT`, which Longhorn trusts for the endpoint. The Secret `longhorn-backup-credentials` (namespace `longhorn-system`) is piped from the config's reference into `kubectl apply --server-side -f -`, so the credentials never land on disk, in a process's arguments, or in a `last-applied-configuration` annotation. An annotation an older install left is removed. Moving to an NFS target deletes the Secret.

## Images

The chart keeps upstream image names; nodes pull them through the site registry's mirrors. Longhorn's manager starts most of its images itself (engine, instance manager, CSI sidecars, share manager), so they aren't `image:` fields in the rendered manifests. `--images` lists them from the chart's values, and the archive's image inventory takes both.

## Where it runs

- **MGMT:** `dciab mgmt`'s `storage` step, after the move, when MGMT's block provider is `longhorn`.
- **INFRA and workload clusters:** not yet.

## Node requirements

Longhorn's v1 data engine needs `iscsiadm` (open-iscsi) on every node, and NFS backup targets need the NFS client. The bootc node image (`images/node-bootc`) carries both and enables `iscsid`.
