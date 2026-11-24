# Array CSI drivers

When a site's array keeps its data safe, DCiaB uses it through the array's CSI driver instead of running Longhorn on top of it, so nothing is replicated twice ([ADR 0008](../../docs/adr/0008-provider-pattern.md), decision 4). This add-on installs the two drivers DCiaB ships:

| `spec.storage.block.provider` | Driver | Platform | Default on |
| --- | --- | --- | --- |
| `proxmox-csi` | The [Proxmox CSI plugin](https://github.com/sergelogvinov/proxmox-csi-plugin) | Proxmox VE | Proxmox |
| `vsphere-csi` | The [vSphere CSI driver](https://github.com/kubernetes-sigs/vsphere-csi-driver) | vSphere | vSphere |

`install.sh` installs or upgrades the driver of one cluster, waits for its controller and node plugin, then applies the cluster's four StorageClasses ([`lib/steps/storage-classes.sh`](../../lib/steps/storage-classes.sh)): `replicated` (the default), `replicated-retain`, `local` and `local-retain`. On an array all four use the array, as the same backend, and differ only in reclaim policy: the array keeps the copies (ADR 0008, decision 5). They bind a volume once its pod is placed, so the volume is made where the pod runs.

- **Version and source:** the upstream release manifest at the tag in `bundle.lock` (`csiDrivers`), checked against `bundle.lock` `checksums`. `hack/fetch_crds.py` fetches it, and the archive carries it in `payload/providers`; with `DCIAB_PAYLOAD` set, it comes from there.
- **DCiaB's changes** (`csi.py`): a privileged namespace for the node plugin; no Windows node plugin; on vSphere, no snapshot sidecar (DCiaB installs no VolumeSnapshot CRDs yet), one controller per control plane node (at most 3), and topology-aware provisioning when the site has vSphere failure domains.
- **Credentials:** a Secret from the config's references, piped into `kubectl apply --server-side`, so they never land on disk, in a process's arguments, or in a `last-applied-configuration` annotation.
- **Images:** the manifests' images, through the site registry's mirrors; the archive's image inventory reads them from `payload/providers`.

```console
$ addons/csi/install.sh <config> <cluster> <kubeconfig>   # install or upgrade, wait, apply the StorageClasses
$ addons/csi/install.sh <config> <cluster> --template     # render; no cluster, no network (secrets redacted)
```

## Proxmox CSI plugin

Volumes are VM disks on one Proxmox storage (`proxmoxCSI.storage`): Ceph RBD, LVM on a shared iSCSI or FC LUN, NFS, or local LVM and ZFS. `proxmoxCSI.shared` (required) says whether that storage is shared in Proxmox; `dciab validate --live` checks it against the storage's `shared` flag. The plugin attaches each one to the node VM that uses it, through the Proxmox API.

- **Token only.** It needs only an API token of its own, with the least privileges in [docs/PROXMOX.md](../../docs/PROXMOX.md#the-proxmox-csi-plugins-token): no Ceph key, no SSH, no access to the storage network. `dciab validate --live` checks the token's privileges and that the storage exists on the nodes.
- **TLS.** The plugin trusts only the Proxmox API certificate whose fingerprint is `spec.platform.proxmox.tlsFingerprint`: `install.sh` reads it from the API, checks the fingerprint, and puts it in the Secret as the plugin's `ca_file`, its only trusted certificate.
- **Topology.** Each node's `topology.kubernetes.io/region` is the site name and `topology.kubernetes.io/zone` its PVE node name (ADR 0010; the ClusterClass sets both). The plugin finds node VMs by UUID; it needs the zone only for volumes on node-local storage (`shared: false`), which are tied to their PVE node. In `r1` a Proxmox control plane isn't spread, so its nodes get a zone only on a one-domain site; with node-local storage on a site with several domains, volumes need worker nodes, and `dciab validate` refuses a worker-less cluster there.
- **Limits.** A volume is a disk of one VM at a time: `ReadWriteOnce` (and `ReadWriteOncePod`) only, no `ReadWriteMany`. On local storage a volume stays on the PVE node it was made on, and its pod can only run there; moving it to another node is a manual step (the plugin's `pvecsictl`). On shared storage it follows the pod. No snapshots yet.
- **VM IDs.** CAPMOX's provider IDs name the VMs (`features.provider: capmox`).

## vSphere CSI driver

Volumes are First Class Disks through vCenter's Cloud Native Storage, on the datastore the storage policy (`vsphereCSI.storagePolicy`) or URL (`vsphereCSI.datastoreURL`) selects, or one every node can reach when neither is set. It pairs with the [vSphere CPI](../vsphere-cpi/README.md): the CPI sets the nodes' provider IDs and zones, and both read the same vCenter credentials from the config.

- **Credentials.** `csi-vsphere.conf` in the Secret `vsphere-config-secret`, with the vCenter thumbprint pinned (`spec.platform.vsphere.thumbprint`). The vCenter user needs the CNS privileges VMware documents for the driver.
- **Nodes.** VMs need hardware version 15 or later and `disk.EnableUUID`, which CAPV sets.
- **Topology.** With vSphere failure domains, the driver reads the same `k8s-region` and `k8s-zone` tag categories as the CPI, and provisions in the pod's zone.
- **Limits.** `ReadWriteOnce` block volumes only: `ReadWriteMany` needs vSAN File Services, which DCiaB doesn't configure. No snapshots yet.

## Where it runs

- **MGMT:** `dciab mgmt`'s `storage` step, when MGMT's block provider is `proxmox-csi` or `vsphere-csi`.
- **INFRA and workload clusters:** not yet.
