# Cilium

Every DCiaB cluster runs Cilium as its CNI. Cilium also **replaces kube-proxy**: the ClusterClasses skip kubeadm's kube-proxy addon, because Cilium's Gateway API and load-balancer features need kube-proxy replacement. A new cluster's nodes stay `NotReady` until Cilium is installed.

`install.sh` installs or upgrades Cilium on one cluster:
- **Version and chart source:** `bundle.lock` (`charts.cilium`, `chartRepositories.cilium`). The chart's sha256 is checked against `chartDigests.cilium`; a mismatch stops the install.
- **DCiaB defaults:** `values.yaml`, the same on every cluster.
- **Per-cluster values:** taken from the cluster's `capi-cluster` values. `k8sServiceHost` is the API VIP, because without kube-proxy Cilium reaches the API server directly. `cluster.name` is the cluster name.

```console
$ addons/cilium/install.sh <capi-cluster values> <kubeconfig>     # install or upgrade, then wait for every node to be Ready
$ addons/cilium/install.sh <capi-cluster values> --print-values   # show the per-cluster values
$ addons/cilium/install.sh <capi-cluster values> --template       # render the manifests; needs no cluster
```

The kubeconfig of a cluster built by CAPI is stored on the management cluster as the Secret `<cluster>-kubeconfig`, in the cluster's namespace (key `value`).

kube-proxy stays absent through upgrades. CAPI replaces control plane nodes instead of running `kubeadm upgrade`, and the control plane controller only updates a kube-proxy DaemonSet that already exists.

If an install is interrupted and Helm leaves the release pending, roll it back (`helm rollback cilium -n kube-system`) before running again.

Pod addresses use Kubernetes IPAM: each node's `podCIDR` comes from the Cluster's pod range (`podCIDR` in the cluster values).

## Where it runs

- **MGMT:** `dciab mgmt`'s `cilium` step, once the first control plane node is up.
- **INFRA:** `dciab infra`, the same way (not implemented yet).
- **Workload clusters:** installed by the baseline add-ons (first sync wave) once INFRA's Argo CD exists, using the same values.

## Not here yet

Load-balancer IP pools, BGP or L2 announcement, and Gateway API come with the MGMT services step. They depend on the site's network settings in the config.
