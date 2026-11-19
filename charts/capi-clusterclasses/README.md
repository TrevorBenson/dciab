# capi-clusterclasses

One ClusterClass per provider, installed once on the management cluster (KinD during `seed`, then MGMT after the pivot). Clusters reference a class by name with `topology.classRef`.

| Class | Provider | Infrastructure |
| --- | --- | --- |
| `dciab-vsphere-v1` | CAPV | VMs cloned from a vSphere template; node IPs from an `InClusterIPPool` named `<cluster>-nodes` |
| `dciab-proxmox-v1` | CAPMOX | VMs cloned from a Proxmox template; node IPs from the cluster's `ipv4Config` |
| `dciab-metal3-v1` | CAPM3 | BareMetalHosts picked by `hostSelector`; addresses from the `Metal3DataTemplate` named `<cluster>-<pool>` |

## Released classes are frozen; drafts are not

Editing a ClusterClass rolls every cluster that uses it, and most changes replace nodes. So once clusters can depend on a class version, it never changes; changes go into the next version, and clusters move over one at a time.

Clusters can only depend on what has shipped, so a version freezes when a **DCiaB release** (a `vX.Y.Z` tag matching `VERSION`) first contains it. Until then it is a **draft**: fix it in place, as often as needed. CI enforces this with `hack/check_frozen_classes.sh`, which fails if a class file from the latest release tag was changed, renamed or deleted.

The lifecycle:

1. **Draft.** `classes/vN/` changes freely until a release tag includes it.
2. **Released.** The tag freezes it.
3. **Next change.** The first change to a class after a release starts the next draft: copy `classes/vN/` to `classes/vN+1/`, rename the objects (`-vN` to `-vN+1`), edit the copy, and add the version to `values.yaml`. Further changes before the next release go into that same draft.
4. **Move clusters** one PR at a time by changing `classVersion` in their `capi-cluster` values: workload clusters first, then INFRA, then MGMT.
5. **Drop** an old version from `values.yaml` once no cluster uses it. Its files stay.

So between two releases there is at most one new version, however many commits it takes.

There has been no release yet, so `v1` is a draft.

## What every class sets up

- **API VIP**: a kube-vip static pod in ARP mode on the control plane nodes (`apiVIP` variable).
- **Registry mirror**: containerd `hosts.toml` for `docker.io`, `quay.io`, `ghcr.io` and `registry.k8s.io`, pointing at `https://<registryMirror>/v2/<upstream>/` (`override_path`). The node template's containerd must use `config_path = "/etc/containerd/certs.d"`.
- **Trust**: the platform CA (`trustBundle`) in the node trust store, applied before kubeadm runs.
- **Time**: NTP servers (`ntpServers`).
- **Break-glass user**: `dciab` with the given SSH keys (`sshAuthorizedKeys`).
- **OIDC** (optional): API server flags for Authentik when `oidc` is set.

Provider ID: vSphere nodes run with `cloud-provider: external`, so the vSphere CPI must be deployed (baseline add-ons). Proxmox uses CAPMOX's provider-ID injection. On Metal3, CAPM3 sets the provider ID on each Node itself (`cloudProviderEnabled: false`), so kubelet gets no `provider-id`. Neither Proxmox nor Metal3 needs a cloud controller.

kube-vip mounts `admin.conf` read-only. It still holds cluster-admin credentials, as in the upstream CAPI provider templates: a static pod has no ServiceAccount when the control plane is first built. A narrower kubeconfig is a candidate for a later class version. Set `vipInterface` when nodes have several NICs; otherwise kube-vip uses the interface that holds the default route.

## Not in v1 yet

- Separate data disks for Longhorn (Longhorn uses the root disk).
- MachineHealthChecks.

## Checking

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ python3 hack/check_manifests.py --crds "$crds" charts/capi-clusterclasses/classes/v1/*.yaml
```

The checker applies each class's patches in order to its templates and evaluates the CRDs' simple CEL rules, so it catches patches that JSON Patch or the API server would reject.
