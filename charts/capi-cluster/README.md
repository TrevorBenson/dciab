# capi-cluster

One cluster per release, installed into the cluster's namespace on the management cluster.

Values come from the site config: `dciab cluster-values <config> <cluster>` prints them for MGMT, INFRA or a workload cluster (`--list` names them). Don't write them by hand.

The chart renders:

- a `Cluster` whose topology uses `dciab-<class>-<classRevision>` from [capi-clusterclasses](../capi-clusterclasses),
- for `vsphere`: an `InClusterIPPool` named `<cluster>-nodes`,
- for `metal3`: an `IPPool` and a `Metal3DataTemplate` per role (`<cluster>-controlplane`, `<cluster>-<pool>`).

Proxmox clusters need no pool object: CAPMOX allocates node addresses from the cluster's `ipv4Config`.

Values are validated by `values.schema.json`: every site-specific value is required and nothing has a default. [ci/](ci) has a complete values file for each class.

## Kept on uninstall

Every object the chart renders carries `helm.sh/resource-policy: keep`. Deleting the `Cluster` deletes its machines, and deleting a pool leaves the cluster without node addresses. So `helm uninstall`, or an upgrade that stops rendering an object, leaves it in place.

To really delete a cluster, delete the `Cluster` itself, then the pools once its machines are gone:

```console
$ kubectl -n <namespace> delete cluster <cluster>
$ helm uninstall <release> -n <namespace>
$ kubectl -n <namespace> delete inclusterippools,ippools,metal3datatemplates -l cluster.x-k8s.io/cluster-name=<cluster>
```

When Argo CD adopts these objects, also set `argocd.argoproj.io/sync-options: Prune=false,Delete=false` on them. Argo CD does not read Helm's annotation.

## Expected around it

- A Secret `<cluster>-vsphere-credentials` (vSphere) or `<cluster>-proxmox-credentials` (Proxmox) in the cluster namespace. DCiaB's bootstrap creates it; External Secrets keeps it in sync afterwards.
- For Metal3: BareMetalHosts labelled to match each `hostSelector`, each annotated `dciab.io/boot-mac: <boot NIC MAC>`.

## What the chart does not check

Address-plan problems (ranges that overlap each other, the API VIP or the gateway; pod and service CIDRs that overlap; a range too small for its replicas) and a Kubernetes version that differs from `bundle.lock` are caught by `dciab validate`, which checks the site config these values are generated from. Clusters added later through Git need the same checks in the gitops repo's CI (planned with the platform repos).
