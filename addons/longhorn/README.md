# Longhorn

Longhorn is DCiaB's block storage (CSI): the default StorageClass `longhorn` on the clusters that run it, starting with MGMT. Volumes are replicated across nodes. The site's backup target is Longhorn's default target; no backup schedule is set yet, so a volume is backed up only when someone asks for a backup.

`install.sh` installs or upgrades Longhorn on one cluster:
- **Version and chart source:** `bundle.lock` (`charts.longhorn`, `chartRepositories.longhorn`). The chart's sha256 is checked against `chartDigests.longhorn`; a mismatch stops the install.
- **Chart, offline:** `--chart <tgz>`, else `$DCIAB_PAYLOAD/charts/longhorn-<version>.tgz` when `DCIAB_PAYLOAD` is set (the archive vendors the chart), else the pinned repository.
- **DCiaB defaults:** `values.yaml`: Longhorn is the default StorageClass, replica data lives in `/var/lib/longhorn` (image-mode nodes have read-only `/usr` and `/opt`), and there is no ingress.
- **Per-site values:** from the config's `spec.storage.longhorn` (`longhorn_values.py`): `replicaCount` sets the StorageClass's and Longhorn's default replica count, and `backupTarget` the default backup target.
- **Control plane nodes:** when MGMT has no worker nodes, its control plane nodes run workloads: they carry no `node-role.kubernetes.io/control-plane` taint ([capi-cluster](../../charts/capi-cluster/README.md#clusters-without-workers)), and Longhorn also tolerates that taint there. Its replicas then share the control plane nodes' root disks with etcd. With 3 control plane nodes, a `replicaCount` of 3 puts one replica on each; a volume needs as many nodes as replicas, so it stays degraded while one node is down or being replaced.
- **UI and API:** the UI and the manager's API are ClusterIP Services without authentication. Restrict them with network policy, and front the UI with the platform's gateway and SSO, when those exist.

```console
$ addons/longhorn/install.sh <config> <kubeconfig>     # install or upgrade, then wait for the manager and the StorageClass
$ addons/longhorn/install.sh <config> --print-values   # show the per-site values
$ addons/longhorn/install.sh <config> --template       # render the manifests; needs no cluster
$ addons/longhorn/install.sh <config> --images         # every image the release runs (for the archive inventory)
```

## Backup targets

- **NFS** (`nfs://<server>:/<export>`): passed to Longhorn as is. No credentials.
- **S3** (`s3://<bucket>@<endpoint>[/<path>]`): Longhorn's own form is `s3://<bucket>@<region>/<path>`, with the endpoint in the credentials Secret, so `install.sh` sets the target to `s3://<bucket>@<region>/<path>`: the region of an AWS endpoint (`s3.<region>.amazonaws.com`), else `us-east-1`, which S3-compatible stores ignore, and the Secret's `AWS_ENDPOINTS` to `https://<endpoint>`. `backupCredentials` (`{file: ...}` or `{env: ...}`) holds `KEY=VALUE` lines:

  ```sh
  AWS_ACCESS_KEY_ID=...
  AWS_SECRET_ACCESS_KEY=...
  # optional: AWS_ENDPOINTS=http://s3.example.net:8333 overrides the https:// endpoint
  ```

  The Secret `longhorn-backup-credentials` (namespace `longhorn-system`) is piped from the config's reference into `kubectl apply -f -`, so the credentials never land on disk or in a process's arguments. Moving to an NFS target deletes it.

## Images

The chart keeps upstream image names; nodes pull them through the site registry's mirrors. Longhorn's manager starts most of its images itself (engine, instance manager, CSI sidecars, share manager), so they aren't `image:` fields in the rendered manifests. `--images` lists them from the chart's values, and the archive's image inventory takes both.

## Where it runs

- **MGMT:** `dciab mgmt`'s `longhorn` step, after the move.
- **INFRA and workload clusters:** not yet.

## Node requirements

Longhorn's v1 data engine needs `iscsiadm` (open-iscsi) on every node, and NFS backup targets need the NFS client. The bootc node image (`images/node-bootc`) carries both and enables `iscsid`.
