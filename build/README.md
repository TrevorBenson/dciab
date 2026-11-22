# Archive build

DCiaB ships as one archive that a site unpacks on the bootstrap host and runs offline. Everything in it is pinned by `bundle.lock`. The build is code here, run by CI; nothing in the archive is fetched at the site.

## Pieces

| Piece | Status | What |
| --- | --- | --- |
| Image inventory (`inventory.py`) | done | Every container image a site needs, and where each comes from |
| Image pull (`archive.py images`) | done | Each image copied by digest, every platform, into one OCI layout; tag-only images pinned in `bundle.lock` (`imageDigests`) |
| Charts and providers (`archive.py charts`, `providers`) | done | Third-party charts as `.tgz`, checked against `bundle.lock` (`chartDigests`); CAPI provider manifests from `hack/fetch_crds.py` (`checksums`) |
| Binaries (`archive.py binaries`) | done | kind, kubectl, helm, clusterctl and oras, checked by sha256 |
| Checksums (`archive.py sums`) | done | `SHA256SUMS` over the whole payload, the file signing will sign |
| Packaging | next | The payload, `dciab` and its libraries as one archive, and `dciab seed` loading the images into the site registry |
| Node images | later | The bootc node image as an OCI image, plus disks per platform where a platform needs one |
| SBOM and signatures | later | An SBOM per image and binary, and a signed manifest of the archive |

## Image inventory

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > cilium.yaml
$ kubeadm config images list --kubernetes-version v1.36.6 > kubeadm.txt
$ python3 build/inventory.py --manifests "$crds" --extra cilium.yaml --kubeadm-images kubeadm.txt
```

The sources are:
- **`bundle.lock`:** every image it pins by digest: kube-vip, the kind node image, the Day 0 services, bootc-image-builder, and so on.
- **The Cluster API, provider and cert-manager manifests**, rendered with the provider variables DCiaB installs them with.
- **Rendered add-ons:** Cilium.
- **The Kubernetes control plane images** kubeadm pulls for the bundle's version.

Not covered yet:
- **Images DCiaB builds itself:** the chrony image from `images/day0-chrony` and the bootc node image. The archive build makes these instead of pulling them.
- **Images of charts other than Cilium**, such as Longhorn. The archive vendors the Longhorn chart; its images join the inventory when DCiaB installs it (MGMT's services).

A manifest that needs a provider variable `bundle.lock` doesn't set is an error, and so is a manifests folder with nothing in it.

Images marked `*` have no digest yet: their source names them by tag only, and `bundle.lock` has no `imageDigests` entry for them. The archive build refuses them until they are pinned.

## Payload

```console
$ python3 build/archive.py digests  --inventory images.yaml      # imageDigests lines to pin, if any
$ python3 build/archive.py binaries --out payload                # first: images uses its oras
$ python3 build/archive.py images   --inventory images.yaml --out payload --oras payload/bin/oras
$ python3 build/archive.py charts   --out payload
$ python3 build/archive.py providers --out payload --manifests "$crds"
$ python3 build/archive.py sums     --out payload
```

```text
payload/
  images/        one OCI layout holding every image
  images.yaml    each image's source reference, tag, digest and name in the layout
  charts/        <chart>-<version>.tgz
  providers/     Cluster API, provider and cert-manager manifests
  bin/           kind, kubectl, helm, clusterctl, oras (linux-amd64)
  SHA256SUMS
```

**Why every platform.** The provider manifests and `bundle.lock` name multi-platform images by their index digest, and nodes pull that digest from the site registry. A registry only serves an index whose images it holds, so the layout keeps the exact index and every image in it. `oras cp` copies the graph byte for byte; skopeo can't write a Docker manifest list into an OCI layout without converting it, which changes its digest. Trimming to one platform would change the digest. That costs space; a later change can filter platforms if the site registry learns to serve a trimmed index under a new digest that DCiaB rewrites into the manifests.

**Pinning.** Every input is checked: images by digest (and checked again in the layout after the copy), charts against `chartDigests`, binaries against `bootstrapAppliance.toolChecksums` and `binarySHA256`, and provider manifests against `checksums`. A missing pin is an error. Each step can be rerun; what is already there and correct is kept. Charts, providers and binaries are built in a staging folder and swapped in only when complete, so a failed run leaves the previous set. One run at a time per payload folder.

CI runs the inventory and a small payload on every PR (job `archive image inventory`: two small images, every chart, the providers and the binaries). The `archive payload` workflow builds the full payload on request.
