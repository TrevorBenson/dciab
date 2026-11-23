# capi-clusterclasses

One ClusterClass per provider, installed once on the management cluster (KinD during `seed`, then MGMT after the pivot). Clusters reference a class by name with `topology.classRef`.

| Class | Provider | Infrastructure |
| --- | --- | --- |
| `dciab-vsphere-r1` | CAPV | VMs cloned from a vSphere template; node IPs from an `InClusterIPPool` named `<cluster>-nodes` |
| `dciab-proxmox-r1` | CAPMOX | VMs cloned from a Proxmox template; node IPs from the cluster's `ipv4Config` |
| `dciab-metal3-r1` | CAPM3 | BareMetalHosts picked by `hostSelector`; addresses from the `Metal3DataTemplate` named `<cluster>-<pool>` |

## Revisions: released classes are frozen, drafts are not

Editing a ClusterClass rolls every cluster that uses it, so a class never changes once a DCiaB release contains it. Changes go into the next revision (`classes/rN/`, objects named `-rN`), and clusters move over one at a time by changing `classRevision` in their `capi-cluster` values. Until a release contains it, a revision is a draft and changes in place; there is at most one draft. `-rN` is a DCiaB revision, not a Kubernetes API version.

[docs/VERSIONING.md](../../docs/VERSIONING.md) has the full lifecycle and the steps to start a new revision. CI enforces it with `hack/check_frozen_classes.sh`.

There has been no release yet, so `r1` is a draft.

## Kept on uninstall

Every ClusterClass and template carries `helm.sh/resource-policy: keep` and `argocd.argoproj.io/sync-options: Prune=false,Delete=false`, set in the class files. Clusters that use a deleted class stop reconciling, so `helm uninstall`, dropping a revision from `revisions`, or an Argo CD prune leaves its objects in place. Argo CD ignores Helm's annotation, hence the second one; it does nothing without Argo CD. Because the chart serves class files as they are, both annotations are part of a revision and freeze with it.

To really delete a revision:

1. Check that no `Cluster` still uses it:

   ```console
   $ kubectl get clusters -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.topology.classRef.name}{"\n"}{end}'
   ```

2. Drop it from `revisions` and upgrade the release.
3. Delete its ClusterClasses (`dciab-<provider>-rN`) first: CAPI refuses to delete a class a Cluster still references, which catches a missed one.
4. Then delete its templates (every object named `dciab-<provider>-rN*`) in the class namespace.

## What every class sets up

- **No kube-proxy**: kubeadm skips its kube-proxy addon. Cilium replaces it and is installed after the control plane is up ([addons/cilium](../../addons/cilium/README.md)); nodes stay `NotReady` until then.
- **API VIP**: a kube-vip static pod in ARP mode on the control plane nodes (`apiVIP` variable).
- **Registry mirror**: for every upstream registry in `mirroredRegistries` (from `bundle.lock`), nodes pull through `https://<registryMirror>/v2/<upstream>/`. The list is a variable, so a new upstream needs no new class revision; changing it rolls the nodes. The classes write the configuration for both runtimes, so either kind of node image works:
  - **containerd:** `hosts.toml` files with `override_path`. The template's containerd must use `config_path = "/etc/containerd/certs.d"`.
  - **CRI-O:** `/etc/containers/registries.conf.d/50-dciab-mirror.conf`, with mirror location `<registryMirror>/<upstream>`.
- **Trust**: the platform CA (`trustBundle`) in the node trust store, applied before kubeadm runs.
- **Time**: NTP servers (`ntpServers`).
- **Break-glass user**: `dciab` with the given SSH keys (`sshAuthorizedKeys`).
- **OIDC** (optional): API server flags for Authentik when `oidc` is set.
- **Pod Security admission**: `baseline` enforced, `restricted` as warnings and audit annotations, in every namespace but `podSecurityExemptNamespaces` (kube-system and the privileged CNI and CSI namespaces). A namespace's own `pod-security.kubernetes.io` labels still override the defaults.
- **Secrets encrypted at rest**: the API server's `EncryptionConfiguration` comes from the Secret `<cluster>-encryption-config` on the management cluster, which [capi-cluster](../capi-cluster/README.md) creates. No key is in a class.
- **Kubelet serving certificates**: `serverTLSBootstrap: true` (a kubeadm patch in `/etc/kubernetes/patches`), so serving certificates come from the cluster CA and rotate. [kubelet-csr-approver](../../addons/kubelet-csr-approver/README.md) approves their CSRs.
- **Health checks**: a MachineHealthCheck for the control plane and each worker pool. A machine whose Node is `NotReady` or `Unknown` for 5 minutes, or that never gets a Node (20 minutes on VMs, 60 on bare metal), is replaced. The control plane replaces one machine at a time, and never so that etcd loses quorum; a worker pool stops replacing once more than 40% of it is unhealthy.
- **Deleting machines**: draining a node stops after 30 minutes and waiting for its volumes to detach after 10, so a stuck pod can't block an upgrade or a replacement forever.
- **Certificate expiry**: control plane machines are replaced 60 days before their kubeadm certificates expire, so a cluster that is never upgraded never runs on expired certificates.
- **API audit log**: an audit policy at `/etc/kubernetes/audit/policy.yaml` on the control plane nodes (Metadata by default; full bodies for RBAC changes; never Secret, ConfigMap or token review bodies; no health checks, events or system leases). The API server writes `/var/log/kubernetes/audit/audit.log`, rotated at 100 MB; a rotated file is deleted after 30 days, or sooner once there are more than 10.

Provider ID: vSphere nodes run with `cloud-provider: external`, so the vSphere CPI must be deployed (baseline add-ons; nothing installs it yet). Proxmox uses CAPMOX's provider-ID injection. On Metal3, CAPM3 sets the provider ID on each Node itself (`cloudProviderEnabled: false`), so kubelet gets no `provider-id`. Neither Proxmox nor Metal3 needs a cloud controller.

kube-vip mounts `admin.conf` read-only. It still holds cluster-admin credentials, as in the upstream CAPI provider templates: a static pod has no ServiceAccount when the control plane is first built. A narrower kubeconfig is a candidate for a later class revision. Set `vipInterface` when nodes have several NICs; otherwise kube-vip uses the interface that holds the default route.

## Not in v1 yet

- Separate data disks for Longhorn (Longhorn uses the root disk).

## Checking

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ python3 hack/check_manifests.py --crds "$crds" charts/capi-clusterclasses/classes/r1/*.yaml
```

The checker applies each class's patches in order to its templates and evaluates the CRDs' simple CEL rules, so it catches patches that JSON Patch or the API server would reject.
