# dciab seed

`dciab seed` builds the three things every later step needs, using only the archive's payload:

- **Seed DNS.** `registry.<network.domain>` resolving to `spec.bootstrap.address` on `network.dnsServiceIPs`, because cluster nodes reach the registry by that name. For a site without `spec.hypervisors`, seed runs `dciab services` in DNS-only mode: CoreDNS alone (the same zones and forwarding as the full services), with its image loaded from the payload. For a site with `spec.hypervisors`, `dciab services` already runs, and seed only checks that it answers. Either way `spec.bootstrap.interface` must already carry `spec.bootstrap.address`; the DNS service IPs are added to it.

- **The seed registry.** Zot, on the bootstrap host at `spec.bootstrap.address:443`, serving every image in the payload under its upstream name (`registry.k8s.io/pause`, `docker.io/kindest/node`, ...), by tag and by digest. It is read-only: nobody can push to it. Images get in only through `dciab seed` writing Zot's storage directly from the payload's OCI layout, with oras ([ADR 0003](adr/0003-archive-images-with-oras.md)).
- **The bootstrap cluster.** kind (`dciab-bootstrap`, on rootful podman). Its containerd pulls from every upstream registry through the seed registry, and trusts the platform CA. It runs cert-manager, Cluster API, the providers and the ClusterClasses, installed from the payload's manifests.

```console
# ./dciab seed dciab.yaml [--payload DIR] [--state DIR] [--age-identity FILE] [--force-new-ca] [--until STEP]
```

It runs as root. Steps: `preflight`, `pki`, `dns`, `registry`, `kind`, `providers`. Each one is safe to rerun, and a finished run changes nothing: the `classes` Helm release, for example, is upgraded only when the chart renders something other than what is deployed. Each step starts with `== seed: <step>` and ends with `== seed: <step> done in <N>s`; `dciab services` prints the same lines for its steps. Then run `dciab mgmt` with the same `--state`: it picks up `<state>/bootstrap.kubeconfig` and `<state>/pki/trust-bundle.pem` by itself, and takes Cilium's chart and the provider manifests from the payload. `--payload` defaults to the archive's own `payload/`.

## The platform CA

| `spec.pki.mode` | What seed does |
| --- | --- |
| `generate-root` | Creates the platform root CA (ECDSA P-384, 10 years). The private key exists in plain text only in a private temporary folder (tmpfs when the host has `/dev/shm`) during that run. It is stored only encrypted with age to every key in `spec.bootstrap.recipients` (`<state>/pki/ca.key.age`). |
| `import-intermediate` | Uses the certificate, key and chain from the config's secret references. The key is never copied into the state. |

Seed then issues the registry's TLS certificate (ECDSA P-256, 30 days). It names `registry.<network.domain>` and `spec.bootstrap.address`, so both work. A rerun reissues it only when it is missing, no longer chains to the CA, names something else, or expires within a week. With `generate-root`, reissuing needs the CA key, so pass `--age-identity` with the identity of one of the recipients.

`<state>/pki/trust-bundle.pem` is what nodes and the bootstrap cluster trust: the root for `generate-root`, the configured chain for `import-intermediate`.

With `generate-root`, seed won't create a new root CA when the state shows one was issued before (`ca.crt`, the trust bundle, the registry certificate or a kubeconfig is there) but the CA itself is incomplete. Whatever trusts the old CA wouldn't trust a new one. Restore the state from a backup (seed names a `state.from-site-*` copy when there is one), or pass `--force-new-ca`. A `ca.key.age` left without its `ca.crt` is never overwritten: seed renames it to `ca.key.age.<time>.bak` before generating a new CA.

## Undoing a seed

```console
# ./dciab seed dciab.yaml --remove [--state DIR] [--force] [--purge-state]
```

`--remove` undoes what seed changed on the host, so a test host or CI runner can be reused:

- deletes the kind cluster `dciab-bootstrap`;
- removes the registry container `dciab-registry`;
- for a site without `spec.hypervisors`, undoes the DNS-only `dciab services` that seed ran: removes its pod, the DNS service IPs it added to `spec.bootstrap.interface`, and the firewalld ports it opened (runtime only).

It touches only what seed recorded creating, in `<state>/seed-created.json` and `<state>/services/created`. An address that was on the NIC before, a port that was already open, or a `dciab-registry` container that seed didn't start (a different container ID) is left alone, and `--remove` says so. A site with `spec.hypervisors` runs `dciab services` on its own, so seed leaves its pod, addresses and ports in place. A seed from before this record existed has nothing recorded; remove its parts by hand.

Each part is dropped from the record once it is undone, so `--remove` can be rerun after a failure, and a finished one changes nothing. It won't delete a bootstrap cluster that still holds Cluster API Clusters (MGMT not yet moved off it), or one it can't ask; `--force` deletes it anyway. Images stay in podman's storage.

The state folder stays: it holds the platform CA, the registry's storage and the kubeconfigs. `--purge-state` deletes it too, after everything else was undone.

> **Warning:** `--purge-state` deletes the CA's certificate and its encrypted key. Nothing that trusts that CA can be served a certificate from it again. Back up the state folder first unless the site is being thrown away.
