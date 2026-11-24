# vSphere cloud provider (CPI)

The vSphere ClusterClass runs kubelet with `cloud-provider: external`. Each node then starts with the taint `node.cloudprovider.kubernetes.io/uninitialized`, and only a cloud provider removes it. The vSphere CPI (cloud controller manager) does that and sets each node's provider ID (`vsphere://<VM UUID>`), which Cluster API uses to match the node to its Machine. Without it, ordinary pods never schedule and Cluster API never sees the nodes.

`install.sh` installs or upgrades it on one vSphere cluster:
- **Version and chart source:** `bundle.lock` (`charts.vsphere-cpi`, `chartRepositories.vsphere-cpi`). The chart's sha256 is checked against `chartDigests.vsphere-cpi`; a mismatch stops the install. CPI releases follow Kubernetes minor versions, so the pin moves with `kubernetes.version`.
- **Chart, offline:** `--chart <tgz>`, else `$DCIAB_PAYLOAD/charts/vsphere-cpi-<version>.tgz` when `DCIAB_PAYLOAD` is set (the archive vendors the chart), else the pinned repository.
- **Per-site values:** from the config's `spec.platform.vsphere` (`cpi_values.py`): `server`, `datacenter` and `thumbprint`.
- **Credentials:** the Secret `vsphere-cloud-secret` (namespace `kube-system`) is piped from `spec.platform.vsphere.credentials` into `kubectl apply --server-side -f -`, so the password never lands on disk, in a process's arguments or in a `last-applied-configuration` annotation. The chart doesn't create it.
- **Thumbprint:** passed to the CPI as is. The schema takes a SHA-256 thumbprint, as CAPV does; that the CPI accepts SHA-256 (and not only SHA-1) is not yet confirmed on real vCenter. If it rejects it, the CPI logs a certificate error and nodes stay uninitialized.
- **Zones and regions:** when the site declares vSphere failure domains (`spec.failureDomains`, ADR 0010), the CPI reads the `k8s-region` and `k8s-zone` tag categories and labels each Node `topology.kubernetes.io/region` and `zone`. Otherwise they stay empty and the CPI reads no tags.

The CPI reaches the API server at its Service address, which works only once Cilium runs. So it is installed before Cilium and checked after it:

```console
$ addons/vsphere-cpi/install.sh <config> <kubeconfig>          # install or upgrade; doesn't wait
$ addons/vsphere-cpi/install.sh <config> --wait <kubeconfig>   # wait until every node is initialized
$ addons/vsphere-cpi/install.sh <config> --print-values        # show the per-site values
$ addons/vsphere-cpi/install.sh <config> --template            # render the manifests (for the archive inventory)
```

## Where it runs

- **MGMT:** `dciab mgmt`'s `cpi` step, before Cilium; the `cilium` step then waits until every node is initialized. Other platforms skip the step.
- **INFRA and workload clusters on vSphere:** not yet.
