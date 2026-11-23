# kubelet-csr-approver

The ClusterClasses set `serverTLSBootstrap: true` on every kubelet, so each node's serving certificate (the one the API server, metrics and `kubectl logs` talk to) is issued by the cluster CA and rotated, instead of being self-signed. A kubelet asks for it with a `kubernetes.io/kubelet-serving` CSR. Kubernetes never approves those by itself, and until one is approved, `kubectl logs`, `exec` and metrics on that node fail.

[kubelet-csr-approver](https://github.com/postfinance/kubelet-csr-approver) approves a serving CSR only when:

- the requester is the node the certificate names (`system:node:<name>`);
- the node name matches `providerRegex`;
- every IP address in it is in the cluster's node IP ranges (`providerIpPrefixes`: each `nodeIPRange` as CIDRs). Not the whole subnet: the API VIP, the load balancer pool and the DNS service addresses sit there too, and a node must never get a cluster-CA certificate for one of them;
- it names at most `allowedDnsNames` DNS names.

Node names aren't resolved (`bypassDnsResolution`), because the platform's DNS doesn't exist yet when a cluster is built.

`install.sh` installs or upgrades it in `kube-system`:

- **Version and chart source:** `bundle.lock` (`charts.kubelet-csr-approver`, `chartRepositories.kubelet-csr-approver`). The chart's sha256 is checked against `chartDigests.kubelet-csr-approver`.
- **DCiaB defaults:** `values.yaml`, the same on every cluster.
- **Per-cluster values:** `providerIpPrefixes`, the node IP ranges from the cluster's `capi-cluster` values.

```console
$ addons/kubelet-csr-approver/install.sh <capi-cluster values> <kubeconfig>     # install or upgrade
$ addons/kubelet-csr-approver/install.sh <capi-cluster values> --print-values   # show the per-cluster values
$ addons/kubelet-csr-approver/install.sh <capi-cluster values> --template       # render the manifests; needs no cluster
```

## Where it runs

- **MGMT:** `dciab mgmt`'s `cilium` step, last: after Cilium (it needs a pod network) and, on vSphere, after the CPI has initialized every node, so nodes have their addresses and no uninitialized taint.
- **INFRA and workload clusters:** the same way as Cilium, when those steps exist.
