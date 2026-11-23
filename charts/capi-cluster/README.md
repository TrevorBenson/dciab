# capi-cluster

One cluster per release, installed into the cluster's namespace on the management cluster.

Values come from the site config: `dciab cluster-values <config> <cluster>` prints them for MGMT, INFRA or a workload cluster (`--list` names them). Don't write them by hand.

The chart renders:

- a `Cluster` whose topology uses `dciab-<class>-<classRevision>` from [capi-clusterclasses](../capi-clusterclasses),
- for `vsphere`: an `InClusterIPPool` named `<cluster>-nodes`, and, when the site has vSphere failure domains, a `VSphereFailureDomain` and a `VSphereDeploymentZone` per domain, named `<cluster>-<domain>` and labelled `dciab.io/cluster=<cluster>` (both kinds are cluster-scoped, so each cluster has its own),
- for `vsphere` and `proxmox` clusters with `storageNetworks`: an `InClusterIPPool` `<cluster>-storage-<name>` per storage network (no gateway),
- for `metal3`: an `IPPool` and a `Metal3DataTemplate` per role (`<cluster>-controlplane`, `<cluster>-<pool>`); a pool split over failure domains gets a data template per domain and keeps one `IPPool` (`addressPool`),
- the Secret `<cluster>-encryption-config`, which holds the cluster's Secret encryption key (below).

Proxmox clusters need no pool object for node addresses: CAPMOX allocates them from the cluster's `ipv4Config`.

Worker pools with a `failureDomain` (`dciab cluster-values` splits each pool into one per domain, `<pool>-<domain>`) are placed there: on vSphere and Metal3 through the MachineDeployment's `failureDomain`, on Proxmox through the class's `failureDomain` variable.

Values are validated by `values.schema.json`: every site-specific value is required and nothing has a default. [ci/](ci) has a complete values file for each class.

## The Secret encryption key

Every class encrypts Secrets at rest in etcd. The API server reads its `EncryptionConfiguration` from the Secret `<cluster>-encryption-config` in the cluster's namespace on the management cluster; Cluster API's bootstrap provider writes it to each control plane node (mode 600).

- **Where it comes from.** The chart makes it on the first install: one random 32-byte `secretbox` key from Helm (`randBytes`). An upgrade looks the Secret up and keeps its key, because a new key would leave every stored Secret unreadable. Nothing about the key is in Git, the values or the class.
- **Where it exists.** Like the kubeadm CA keys, which take the same path, the key is in:
  - the Secret itself, in the cluster's namespace on the management cluster (the kind bootstrap cluster first, then MGMT after `clusterctl move`). It carries `helm.sh/resource-policy: keep` and `clusterctl.cluster.x-k8s.io/move`, so uninstalling never deletes it and the move takes it along;
  - every Helm release record of the release (`sh.helm.release.v1.<release>.vN` Secrets, up to the history limit), on the bootstrap cluster and on MGMT;
  - the bootstrap cluster's etcd, which isn't encrypted (it lives on the bootstrap host's work disk until the bootstrap cluster is deleted);
  - each control plane machine's bootstrap-data Secret, which Cluster API's kubeadm bootstrap provider writes in the same namespace;
  - the hypervisor's copy of that cloud-init data (CAPMOX's cloud-init ISO on the Proxmox storage, vSphere `guestinfo`), and on each control plane node at `/etc/kubernetes/encryption/config.yaml`, mode 600.

  On MGMT, MGMT's etcd encrypts the Secrets that hold it (its own key included, read from its nodes' files).
- **Recovery.** Losing it loses every Secret in that cluster, and an etcd snapshot can't be restored without it. Nothing puts it in ADR 0009's recovery bundle yet: that is a follow-up, with the bundle itself.
- **Renderers without the cluster.** `helm template` and Argo CD can't look the Secret up and would render a new key. Set `encryption.createSecret: false` there; the Secret must then already exist.
- **Rotation** is by hand for now, in three rolls of the control plane, because during each roll old and new nodes must both read every key in use:
  1. Add the new key as the second `secretbox` key in the Secret, and roll the control plane. Editing the Secret doesn't roll it by itself (the class only references the Secret): set `rollout.after` on the KubeadmControlPlane, or run `clusterctl alpha rollout restart`.
  2. Move the new key first, and roll again. New writes now use it.
  3. Rewrite every Secret (`kubectl get secrets -A -o json | kubectl replace -f -`), drop the old key, and roll a third time.

## Kept on uninstall

Every object the chart renders carries `helm.sh/resource-policy: keep`. Deleting the `Cluster` deletes its machines, and deleting a pool leaves the cluster without node addresses. So `helm uninstall`, or an upgrade that stops rendering an object, leaves it in place.

They also carry `argocd.argoproj.io/sync-options: Prune=false,Delete=false`. Argo CD ignores Helm's annotation; this one does the same for Argo CD once it adopts these objects. It does nothing without Argo CD.

To really delete a cluster, delete the `Cluster` and wait: CAPI deletes its machines first. Then uninstall the release and delete the pools:

```console
$ kubectl -n <namespace> delete cluster <cluster> --wait
$ helm uninstall <release> -n <namespace>
$ kubectl -n <namespace> delete inclusterippools.ipam.cluster.x-k8s.io -l cluster.x-k8s.io/cluster-name=<cluster>          # vSphere
$ kubectl -n <namespace> delete ippools.ipam.metal3.io,metal3datatemplates -l cluster.x-k8s.io/cluster-name=<cluster>     # Metal3
```

A self-managed cluster (MGMT) runs the controllers that delete it. `clusterctl move` it to a bootstrap cluster first, or expect leftovers.

Kept objects keep Helm's `meta.helm.sh/release-*` annotations. Installing again with the same release name and namespace adopts them; another release name fails with "invalid ownership metadata".

## Expected around it

- A Secret `<cluster>-vsphere-credentials` (vSphere) or `<cluster>-proxmox-credentials` (Proxmox) in the cluster namespace. DCiaB's bootstrap creates it (for MGMT, `dciab mgmt`); keeping it in sync afterwards with External Secrets is planned.
- For Metal3: BareMetalHosts labelled to match each `hostSelector`, each annotated `dciab.io/boot-mac: <boot NIC MAC>`.

## What the chart does not check

Address-plan problems (ranges that overlap each other, the API VIP or the gateway; pod and service CIDRs that overlap; a range too small for its replicas) and a Kubernetes version that differs from `bundle.lock` are caught by `dciab validate`, which checks the site config these values are generated from. Clusters added later through Git need the same checks in the gitops repo's CI (planned with the platform repos).
