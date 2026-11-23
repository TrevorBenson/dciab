# capi-cluster

One cluster per release, installed into the cluster's namespace on the management cluster.

Values come from the site config: `dciab cluster-values <config> <cluster>` prints them for MGMT, INFRA or a workload cluster (`--list` names them). Don't write them by hand.

The chart renders:

- a `Cluster` whose topology uses `dciab-<class>-<classRevision>` from [capi-clusterclasses](../capi-clusterclasses),
- for `vsphere`: an `InClusterIPPool` named `<cluster>-nodes`,
- for `metal3`: an `IPPool` and a `Metal3DataTemplate` per role (`<cluster>-controlplane`, `<cluster>-<pool>`),
- the Secret `<cluster>-encryption-config`, which holds the cluster's Secret encryption key (below).

Proxmox clusters need no pool object: CAPMOX allocates node addresses from the cluster's `ipv4Config`.

Values are validated by `values.schema.json`: every site-specific value is required and nothing has a default. [ci/](ci) has a complete values file for each class.

## The Secret encryption key

Every class encrypts Secrets at rest in etcd. The API server reads its `EncryptionConfiguration` from the Secret `<cluster>-encryption-config` in the cluster's namespace on the management cluster; Cluster API's bootstrap provider writes it to each control plane node (mode 600).

- **Where it comes from.** The chart makes it on the first install: one random 32-byte `secretbox` key from Helm (`randBytes`). An upgrade looks the Secret up and keeps its key, because a new key would leave every stored Secret unreadable. Nothing about the key is in Git, the values or the class.
- **How it is protected.** It exists only in that Secret (on the management cluster, whose own etcd encrypts it in turn on MGMT) and on the cluster's control plane nodes, readable by root only. It carries `helm.sh/resource-policy: keep` and `clusterctl.cluster.x-k8s.io/move`, so uninstalling never deletes it and `clusterctl move` takes it along with the cluster. Losing it loses every Secret in that cluster, so it belongs in the recovery bundle (ADR 0009).
- **Renderers without the cluster.** `helm template` and Argo CD can't look the Secret up and would render a new key. Set `encryption.createSecret: false` there; the Secret must then already exist.
- **Rotation** is by hand for now: add a second key first, roll the control plane, rewrite all Secrets, then drop the old key.

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
