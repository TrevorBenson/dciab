# capi-clusterclasses

One ClusterClass per provider, installed once on the management cluster (KinD during `seed`, then MGMT after the pivot). Clusters reference a class by name with `topology.classRef`.

| Class | Provider | Infrastructure |
| --- | --- | --- |
| `dciab-vsphere-v2` | CAPV | VMs cloned from a vSphere template; node IPs from an `InClusterIPPool` named `<cluster>-nodes` |
| `dciab-proxmox-v2` | CAPMOX | VMs cloned from a Proxmox template; node IPs from the cluster's `ipv4Config` |
| `dciab-metal3-v2` | CAPM3 | BareMetalHosts picked by `hostSelector`; addresses from the `Metal3DataTemplate` named `<cluster>-<pool>` |

## Classes are frozen

Editing a ClusterClass rolls every cluster that uses it. So each version lives in its own folder, `classes/vN/`, and never changes once released; `classes/SHA256SUMS` makes CI fail if it does.

To change a class:

1. Copy `classes/vN/` to `classes/vN+1/` and rename the objects (`-vN` to `-vN+1`).
2. Edit the copy, add its sums to `SHA256SUMS`, and add the version to `values.yaml`.
3. Move clusters over one PR at a time by changing `classVersion` in their `capi-cluster` values: workload clusters first, then INFRA, then MGMT.
4. Drop the old version from `values.yaml` once no cluster uses it.

## Version history

| Version | Status | Change |
| --- | --- | --- |
| v2 | Current | Metal3 cluster template carries a placeholder `controlPlaneEndpoint`, which CAPM3's webhook requires. Found by the end-to-end test. |
| v1 | Retired, never installable | CAPM3's webhook rejected its Metal3 cluster template, so the chart could not install it. Kept frozen as released. |

## What every class sets up

- **API VIP**: a kube-vip static pod in ARP mode on the control plane nodes (`apiVIP` variable).
- **Registry mirror**: containerd `hosts.toml` for `docker.io`, `quay.io`, `ghcr.io` and `registry.k8s.io`, pointing at `https://<registryMirror>/v2/<upstream>/` (`override_path`). The node template's containerd must use `config_path = "/etc/containerd/certs.d"`.
- **Trust**: the platform CA (`trustBundle`) in the node trust store, applied before kubeadm runs.
- **Time**: NTP servers (`ntpServers`).
- **Break-glass user**: `dciab` with the given SSH keys (`sshAuthorizedKeys`).
- **OIDC** (optional): API server flags for Authentik when `oidc` is set.

Provider ID: vSphere nodes run with `cloud-provider: external`, so the vSphere CPI must be deployed (baseline add-ons). Proxmox uses CAPMOX's provider-ID injection. On Metal3, CAPM3 sets the provider ID on each Node itself (`cloudProviderEnabled: false`), so kubelet gets no `provider-id`. Neither Proxmox nor Metal3 needs a cloud controller.

kube-vip mounts `admin.conf` read-only. It still holds cluster-admin credentials, as in the upstream CAPI provider templates: a static pod has no ServiceAccount when the control plane is first built. A narrower kubeconfig is a candidate for a later class version. Set `vipInterface` when nodes have several NICs; otherwise kube-vip uses the interface that holds the default route.

## Not in v2 yet

- Separate data disks for Longhorn (Longhorn uses the root disk).
- MachineHealthChecks.

## Checking

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ python3 hack/check_manifests.py --crds "$crds" charts/capi-clusterclasses/classes/v2/*.yaml
```

The checker applies each class's patches in order to its templates and evaluates the CRDs' simple CEL rules, so it catches patches that JSON Patch or the API server would reject.
