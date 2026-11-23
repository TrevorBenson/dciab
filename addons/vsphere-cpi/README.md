# vSphere cloud provider (CPI)

The vSphere ClusterClass runs kubelet with `cloud-provider: external`. Each node then starts with the taint `node.cloudprovider.kubernetes.io/uninitialized`, and only a cloud provider removes it. The vSphere CPI (cloud controller manager) does that and sets each node's provider ID (`vsphere://<VM UUID>`), which Cluster API uses to match the node to its Machine. Without it, ordinary pods never schedule and Cluster API never sees the nodes.

`install.sh` installs or upgrades it on one vSphere cluster:
- **Version and chart source:** `bundle.lock` (`charts.vsphere-cpi`, `chartRepositories.vsphere-cpi`). The chart's sha256 is checked against `chartDigests.vsphere-cpi`; a mismatch stops the install. CPI releases follow Kubernetes minor versions, so the pin moves with `kubernetes.version`.
- **Chart, offline:** `--chart <tgz>`, else `$DCIAB_PAYLOAD/charts/vsphere-cpi-<version>.tgz` when `DCIAB_PAYLOAD` is set (the archive vendors the chart), else the pinned repository.
- **Per-site values:** from the config's `spec.platform.vsphere` (`cpi_values.py`): `server`, `datacenter` and `thumbprint`.
- **Credentials:** the Secret `vsphere-cloud-secret` (namespace `kube-system`) is piped from `spec.platform.vsphere.credentials` into `kubectl apply -f -`, so the password never lands on disk or in a process's arguments. The chart doesn't create it.
- **Zones and regions:** not set yet, so the CPI doesn't read vSphere tags.

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
