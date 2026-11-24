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
- **Authentication** (optional): structured authentication. The `authentication` variable lists JWT issuers (URL, client ID, claims, CA), written to an `AuthenticationConfiguration` file the API server reads (`--authentication-config`). Several issuers at once, such as Authentik and the site's IdP during a move. Each issuer has a `name`, and its users and groups get the prefix `<name>:` (or `usernamePrefix` and `groupsPrefix`), so two issuers can never name the same user or group; the variable rejects duplicate URLs, names and prefixes. This changes user names from the old `--oidc-*` form (the issuer URL and `#`, then the claim) to `authentik:alice`; nothing binds users by name yet. MGMT and INFRA set none.
- **Failure domains** ([ADR 0010](../../docs/adr/0010-failure-domains.md)):
  - **Proxmox**: a worker pool with `failureDomain` (a MachineDeployment override) runs only on that PVE node (`allowedNodes`). Every kubelet labels its Node `topology.kubernetes.io/region` from `topology.region`, and `topology.kubernetes.io/zone` from the pool's `failureDomain`, or from `topology.zone` on a one-domain site. The control plane isn't spread in `r1`: CAPMOX v0.9 has no failure domains.
  - **vSphere**: with `zones`, the control plane spreads over the `VSphereDeploymentZone`s labelled `dciab.io/cluster=<cluster>` ([capi-cluster](../capi-cluster/README.md) renders them), and each MachineDeployment names one. CAPV's cluster modules (DRS anti-affinity) keep each group's VMs on separate hosts; `disableClusterModule` turns them off on a site without DRS.
  - **Metal3**: `failureDomains` become the `Metal3Cluster`'s failure domains, all for the control plane. The template carries `metal3.io/metal3-label-sync-prefixes: topology.kubernetes.io`, so CAPM3 copies the hosts' topology labels to their Nodes.
- **Storage networks** (Proxmox and vSphere): one extra NIC per `storageNetworks` entry (bridge and VLAN, or port group; optional MTU), with an address from the pool `<cluster>-storage-<name>` and no gateway. When there is one, nodes load `nvme-tcp` and `dm_multipath` and start `multipathd` if the image has it. `mtu` sets the node NIC's MTU.
- **Pod Security admission**: `baseline` enforced, `restricted` as warnings and audit annotations, in every namespace but `podSecurityExemptNamespaces` (kube-system and the privileged CNI and CSI namespaces). A namespace's own `pod-security.kubernetes.io` labels still override the defaults; components that need more than baseline (Ironic, node-exporter, host-network DNS or DHCP) label their own namespaces. The list is in a file on the control plane nodes, so changing it rolls the control plane.
- **Schedulable control plane** (optional): with `controlPlaneSchedulable: true`, control plane nodes register without kubeadm's `node-role.kubernetes.io/control-plane:NoSchedule` taint (`nodeRegistration.taints: []` for init and join), so ordinary pods run there. For a cluster without worker nodes; [capi-cluster](../capi-cluster/README.md) sets it for one. Changing it rolls the control plane.
- **Secrets encrypted at rest**: the API server's `EncryptionConfiguration` comes from the Secret `<cluster>-encryption-config` on the management cluster, which [capi-cluster](../capi-cluster/README.md) creates. No key is in a class.
- **Kubelet serving certificates**: `serverTLSBootstrap: true` (a kubeadm patch in `/etc/kubernetes/patches`), so serving certificates come from the cluster CA and rotate. [kubelet-csr-approver](../../addons/kubelet-csr-approver/README.md) approves their CSRs.
- **Health checks**: a MachineHealthCheck for the control plane and each worker pool. A machine whose Node is `NotReady` or `Unknown` for 5 minutes, or that never gets a Node (20 minutes on VMs, 60 on bare metal), is replaced. Only one unhealthy machine at a time is replaced, in the control plane and in each worker pool (an absolute limit, so pools of one or two are repaired too); with two or more unhealthy at once nothing is replaced. The control plane never remediates so that etcd loses quorum, and retries a failing machine at most 3 times, 10 minutes apart. On Proxmox a replacement control plane VM may land on the same host as a survivor until CAPMOX spreads the control plane (ADR 0010).
- **Deleting machines**: draining a node stops after 30 minutes and waiting for its volumes to detach after 10, so a stuck pod can't block an upgrade or a replacement forever. Longhorn's drain policy, `block-for-eviction-if-contains-last-replica` ([addons/longhorn](../../addons/longhorn/values.yaml)), moves a volume's last replica off the node during the drain, so the timeout doesn't delete it.
- **Data disk for Longhorn** (vSphere): a machine whose `machine` variable has `dataDiskGiB` gets a second disk of that size (`additionalDisksGiB`). Before kubeadm runs, `/etc/dciab/data-disk.sh` formats the blank disk of exactly that size once (XFS, label `dciab-data`) and mounts it at `/var/lib/longhorn` through `/etc/fstab`, so Longhorn never writes to the root disk that etcd uses. With no such disk, the bootstrap fails. The size must differ from `diskGiB`. CAPMOX has no second disk, so the Proxmox class has none.
- **Certificate expiry**: control plane machines are replaced 60 days before their kubeadm certificates expire, so a cluster that is never upgraded never runs on expired certificates.
- **API audit log**: an audit policy at `/etc/kubernetes/audit/policy.yaml` on the control plane nodes (Metadata by default; full bodies for RBAC changes; never Secret, ConfigMap or token review bodies; no health checks, events or system leases). The API server writes `/var/log/kubernetes/audit/audit.log`, rotated at 100 MB; a rotated file is deleted after 30 days, or sooner once there are more than 10.

Provider ID: vSphere nodes run with `cloud-provider: external`, so the vSphere CPI must be deployed (baseline add-ons; nothing installs it yet). Proxmox uses CAPMOX's provider-ID injection. On Metal3, CAPM3 sets the provider ID on each Node itself (`cloudProviderEnabled: false`), so kubelet gets no `provider-id`. Neither Proxmox nor Metal3 needs a cloud controller.

kube-vip mounts `admin.conf` read-only. It still holds cluster-admin credentials, as in the upstream CAPI provider templates: a static pod has no ServiceAccount when the control plane is first built. A narrower kubeconfig is a candidate for a later class revision. Set `vipInterface` when nodes have several NICs; otherwise kube-vip uses the interface that holds the default route.

## Not in v1 yet

- Separate data disks for Longhorn on Proxmox: CAPMOX can't add a second disk to a machine yet. On Proxmox, use the Proxmox CSI driver instead of Longhorn.

## Checking

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ python3 hack/check_manifests.py --crds "$crds" charts/capi-clusterclasses/classes/r1/*.yaml
```

The checker applies each class's patches in order to its templates and evaluates the CRDs' simple CEL rules, so it catches patches that JSON Patch or the API server would reject.
