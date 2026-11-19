# capi-clusterclasses

One ClusterClass per provider, installed once on the management cluster (KinD during `seed`, then MGMT after the pivot). Clusters reference a class by name with `topology.classRef`.

| Class | Provider | Infrastructure |
| --- | --- | --- |
| `dciab-vsphere-v1` | CAPV | VMs cloned from a vSphere template; node IPs from an `InClusterIPPool` named `<cluster>-nodes` |
| `dciab-proxmox-v1` | CAPMOX | VMs cloned from a Proxmox template; node IPs from the cluster's `ipv4Config` |
| `dciab-metal3-v1` | CAPM3 | BareMetalHosts picked by `hostSelector`; addresses from the `Metal3DataTemplate` named `<cluster>-<pool>` |

## Classes are frozen

Editing a ClusterClass rolls every cluster that uses it. So each version lives in its own folder, `classes/vN/`, and never changes once released; `classes/SHA256SUMS` makes CI fail if it does.

To change a class:

1. Copy `classes/vN/` to `classes/vN+1/` and rename the objects (`-vN` to `-vN+1`).
2. Edit the copy, add its sums to `SHA256SUMS`, and add the version to `values.yaml`.
3. Move clusters over one PR at a time by changing `classVersion` in their `capi-cluster` values: workload clusters first, then INFRA, then MGMT.
4. Drop the old version from `values.yaml` once no cluster uses it.

## What every class sets up

- **API VIP**: a kube-vip static pod in ARP mode on the control plane nodes (`apiVIP` variable).
- **Registry mirror**: containerd `hosts.toml` for `docker.io`, `quay.io`, `ghcr.io` and `registry.k8s.io`, pointing at `https://<registryMirror>/v2/<upstream>/` (`override_path`). The node template's containerd must use `config_path = "/etc/containerd/certs.d"`.
- **Trust**: the platform CA (`trustBundle`) in the node trust store, applied before kubeadm runs.
- **Time**: NTP servers (`ntpServers`).
- **Break-glass user**: `dciab` with the given SSH keys (`sshAuthorizedKeys`).
- **OIDC** (optional): API server flags for Authentik when `oidc` is set.

Provider ID: vSphere nodes run with `cloud-provider: external`, so the vSphere CPI must be deployed (baseline add-ons). Proxmox uses CAPMOX's provider-ID injection; Metal3 sets `metal3://` IDs from metadata. Neither needs a cloud controller.

## Not in v1

- Separate data disks for Longhorn (Longhorn uses the root disk).
- MachineHealthChecks.

## Checking

```console
$ python3 hack/fetch_crds.py
$ python3 hack/check_manifests.py --crds .cache/crds charts/capi-clusterclasses/classes/v1/*.yaml
```
