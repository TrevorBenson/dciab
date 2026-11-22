# Archive build

DCiaB ships as one archive that a site unpacks on the bootstrap host and runs offline. Everything in it is pinned by `bundle.lock`. The build is code here, run by CI; nothing in the archive is fetched at the site.

## Pieces

| Piece | Status | What |
| --- | --- | --- |
| Image inventory (`inventory.py`) | done | Every container image a site needs, and where each comes from |
| Image pull (`archive.py images`) | done | Each image copied by digest, every platform, into one OCI layout; tag-only images pinned in `bundle.lock` (`imageDigests`) |
| Charts and providers (`archive.py charts`, `providers`) | done | Third-party charts as `.tgz`, checked against `bundle.lock` (`chartDigests`); CAPI provider manifests from `hack/fetch_crds.py` (`checksums`) |
| Binaries (`archive.py binaries`) | done | kind, kubectl, helm, clusterctl, oras and cosign, checked by sha256 |
| SBOMs (`package.py sboms`) | done | An SPDX SBOM per image (its linux/amd64 image) and one for `bin/`, made with syft (build only) |
| Checksums (`archive.py sums`) | done | `SHA256SUMS` over the whole payload, SBOMs included |
| Packaging (`package.py package`) | done | `dciab`, its libraries, charts, add-ons, docs and the payload as one reproducible tar with a `MANIFEST` |
| Signatures (`package.py package --signing-key`, `dciab verify-archive`) | done | `MANIFEST` and `SHA256SUMS` signed with a cosign key; the site checks them offline with the public key |
| `dciab seed` | next | Loading the images into the site registry |
| Node images | later | The bootc node image as an OCI image, plus disks per platform where a platform needs one |

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
- **Rendered add-ons:** Cilium and Longhorn. Longhorn's manager starts most of its images itself, so `addons/longhorn/install.sh <config> --images` lists them from the chart's values and CI passes each as `--image`.
- **The Kubernetes control plane images** kubeadm pulls for the bundle's version.

Not covered yet:
- **Images DCiaB builds itself:** the chrony image from `images/day0-chrony` and the bootc node image. The archive build makes these instead of pulling them.
- **Images of charts other than Cilium and Longhorn**, such as MGMT's other services (Metal3, PowerDNS, Kea), until DCiaB installs them.

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
  bin/           kind, kubectl, helm, clusterctl, oras, cosign (linux-amd64)
  sboms/         SPDX SBOMs (package.py sboms)
  SHA256SUMS
```

**Why every platform.** The provider manifests and `bundle.lock` name multi-platform images by their index digest, and nodes pull that digest from the site registry. A registry only serves an index whose images it holds, so the layout keeps the exact index and every image in it. `oras cp` copies the graph byte for byte; skopeo can't write a Docker manifest list into an OCI layout without converting it, which changes its digest ([ADR 0003](../docs/adr/0003-archive-images-with-oras.md)). Trimming to one platform would change the digest. That costs space; a later change can filter platforms if the site registry learns to serve a trimmed index under a new digest that DCiaB rewrites into the manifests.

**Pinning.** Every input is checked: images by digest (and checked again in the layout after the copy), charts against `chartDigests`, binaries against `bootstrapAppliance.toolChecksums` and `binarySHA256`, and provider manifests against `checksums`. A missing pin is an error. Each step can be rerun; what is already there and correct is kept. Charts, providers and binaries are built in a staging folder and swapped in only when complete, so a failed run leaves the previous set. One run at a time per payload folder.

CI runs the inventory and a small payload on every PR (job `archive image inventory`: two small images, every chart, the providers and the binaries). The `archive payload` workflow builds the full payload on request.

## Package

```console
$ python3 build/package.py tools   --out build-tools                    # syft, checked by sha256; build only
$ python3 build/package.py sboms   --payload payload --syft build-tools/tools/syft
$ python3 build/archive.py sums    --out payload                         # again: SHA256SUMS covers the SBOMs
$ COSIGN_PASSWORD=... python3 build/package.py package --payload payload --out dist \
      --signing-key cosign.key --cosign payload/bin/cosign
```

```text
dciab-<VERSION>.tar
  dciab-<VERSION>/
    dciab  lib/  schema/  charts/  addons/  docs/  examples/  hack/ (the scripts steps run)
    README.md  VERSION  bundle.lock
    payload/     the payload above, plus sboms/ and SHA256SUMS.sig
    MANIFEST     sha256 of every other file (sha256sum format)
    MANIFEST.sig
```

**SBOMs.** syft reads an OCI layout that holds exactly one image manifest, so each image is scanned from a temporary layout holding only its linux/amd64 image (blobs hard-linked from the payload's layout). `sboms/images/<name>.spdx.json` uses the image's name in the layout (`images.yaml`); `sboms/bin.spdx.json` covers the binaries.

**Reproducible.** Entries are sorted, owned by 0:0 with fixed modes (755 or 644) and dated `SOURCE_DATE_EPOCH`, or the last commit's time. Development files never ship: `.git`, caches, `__pycache__`, `tests`, `handoff/`, `images/` (recipes, not runtime). The same inputs give the same tar byte for byte, except the signatures: ECDSA signatures differ on every run. The tar isn't compressed, since images already are; `--zstd` writes `.tar.zst`. `package` refuses a payload that doesn't match its `SHA256SUMS`.

**Signing.** The site is offline, so keyless signing (which needs Fulcio and Rekor) doesn't apply: `--signing-key` takes a cosign key pair's private key, password in `COSIGN_PASSWORD`, and signs without a transparency log. Keep the private key out of the repository. The site gets the public key separately from the archive and runs:

```console
$ ./dciab verify-archive --key cosign.pub dciab-<VERSION>.tar   # before unpacking
$ ./dciab verify-archive --key cosign.pub                       # the unpacked archive this dciab is in
```

It checks `MANIFEST.sig` with `cosign verify-blob`, then hashes every file: a missing, changed or extra file fails. It uses `cosign` on `PATH` if there is one, else `payload/bin/cosign`. The archive's own cosign can't vouch for itself, so check with a cosign the site already trusts (`--cosign`) where you can. `--checksums-only` skips the signature for unsigned builds.

CI packages the small payload on every PR, signs it with a throwaway key, verifies the tar and the unpacked folder, checks a changed file fails, and rebuilds unsigned to compare byte for byte. The `archive payload` workflow does the same for the full payload.
