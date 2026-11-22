# dciab seed

`dciab seed` builds the two things every later step needs, using only the archive's payload:

- **The seed registry.** Zot, on the bootstrap host at `spec.bootstrap.address:443`, serving every image in the payload under its upstream name (`registry.k8s.io/pause`, `docker.io/kindest/node`, ...), by tag and by digest. It is read-only: nobody can push to it. Images get in only through `dciab seed` writing Zot's storage directly from the payload's OCI layout, with oras ([ADR 0003](adr/0003-archive-images-with-oras.md)).
- **The bootstrap cluster.** kind (`dciab-bootstrap`, on rootful podman). Its containerd pulls from every upstream registry through the seed registry, and trusts the platform CA. It runs cert-manager, Cluster API, the providers and the ClusterClasses, installed from the payload's manifests.

```console
# ./dciab seed dciab.yaml [--payload DIR] [--state DIR] [--age-identity FILE] [--until STEP]
```

It runs as root. Steps: `preflight`, `pki`, `registry`, `kind`, `providers`. Each one is safe to rerun, and a finished run changes nothing. Then run `dciab mgmt` with the same `--state`: it picks up `<state>/bootstrap.kubeconfig` and `<state>/pki/trust-bundle.pem` by itself, and takes Cilium's chart and the provider manifests from the payload.

## The platform CA

| `spec.pki.mode` | What seed does |
| --- | --- |
| `generate-root` | Creates the platform root CA (ECDSA P-384, 10 years). The private key exists in plain text only in a private temporary folder (tmpfs when the host has `/dev/shm`) during that run. It is stored only encrypted with age to every key in `spec.bootstrap.recipients` (`<state>/pki/ca.key.age`). |
| `import-intermediate` | Uses the certificate, key and chain from the config's secret references. The key is never copied into the state. |

Seed then issues the registry's TLS certificate (ECDSA P-256, 30 days). It names `registry.<network.domain>` and `spec.bootstrap.address`, so both work. A rerun reissues it only when it is missing, no longer chains to the CA, names something else, or expires within a week. With `generate-root`, reissuing needs the CA key, so pass `--age-identity` with the identity of one of the recipients.

`<state>/pki/trust-bundle.pem` is what nodes and the bootstrap cluster trust: the root for `generate-root`, the configured chain for `import-intermediate`.

## Not yet

- **DNS for `registry.<network.domain>`.** The bootstrap cluster reaches the registry by address. Cluster nodes use the name, which the Day 0 DNS (`dciab services`) or the site's DNS must answer with `spec.bootstrap.address`.
- **Packaging.** `--payload` defaults to `<archive>/payload`, which the archive packaging step will create. Until then, build a payload with `build/archive.py` (see [build/README.md](../build/README.md)).
