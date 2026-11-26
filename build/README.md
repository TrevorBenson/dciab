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
| `dciab seed` | done | Loading the payload's images into the seed registry ([SEED.md](../docs/SEED.md)) |
| Node images (`archive.py nodes`) | Proxmox VE built | The bootc node image (an OCI image in the layout, once pinned) and each platform's disk in `payload/nodes/`, published by the lab runner (`lab-node-image.yml`) and pinned in `bundle.lock` (`nodeImages.published`). vSphere and Metal3 disks join with their lanes |

## Image inventory

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > cilium.yaml
$ kubeadm config images list --kubernetes-version v1.36.6 > kubeadm.txt
$ python3 build/inventory.py --manifests "$crds" --extra cilium.yaml --kubeadm-images kubeadm.txt
```

The sources are:
- **`bundle.lock`:** every image it pins by digest: kube-vip, the kind node image, the Day 0 services, bootc-image-builder, and so on.
- **Images DCiaB builds itself,** such as the Day 0 chrony image (`images/day0-chrony/build.sh`). CI builds and publishes each on `main`, once per version, and `bundle.lock` pins its digest (`day0.images.chrony`). From then on it is listed like any other pinned image. Until it is pinned the inventory leaves it out with a warning, and `--require-built` (used by the full archive build) refuses.
- **The Cluster API, provider and cert-manager manifests**, rendered with the provider variables DCiaB installs them with, and the CSI driver manifests (`bundle.lock` `csiDrivers`).
- **Rendered add-ons:** Cilium and Longhorn. Longhorn's manager starts most of its images itself, so `addons/longhorn/install.sh <config> --images` lists them from the chart's values and CI passes each as `--image`.
- **The Kubernetes control plane images** kubeadm pulls for the bundle's version.

Not covered yet:
- **The bootc node image,** which DCiaB builds itself.
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
$ python3 build/archive.py nodes    --out payload --oras payload/bin/oras [--require-built]
$ python3 build/archive.py sums     --out payload
```

```text
payload/
  images/        one OCI layout holding every image
  images.yaml    each image's source reference, tag, digest and name in the layout
  charts/        <chart>-<version>.tgz
  providers/     Cluster API, provider and cert-manager manifests
  bin/           kind, kubectl, helm, clusterctl, oras, cosign (linux-amd64)
  nodes/         <platform>/<disk> and <disk>.sha256sum; nodes.yaml lists them
  sboms/         SPDX SBOMs (package.py sboms)
  SHA256SUMS
```

**Why every platform.** The provider manifests and `bundle.lock` name multi-platform images by their index digest, and nodes pull that digest from the site registry. A registry only serves an index whose images it holds, so the layout keeps the exact index and every image in it. `oras cp` copies the graph byte for byte; skopeo can't write a Docker manifest list into an OCI layout without converting it, which changes its digest ([ADR 0003](../docs/adr/0003-archive-images-with-oras.md)). Trimming to one platform would change the digest. That costs space; a later change can filter platforms if the site registry learns to serve a trimmed index under a new digest that DCiaB rewrites into the manifests.

**Node disks.** The node image is built on the lab runner (its Rocky bootc base can't be built on a hosted runner) by `.github/workflows/lab-node-image.yml` on `main`, once per tag (`hack/lab/node-image-id.sh`): the bootc image to `ghcr.io/<owner>/dciab-node`, and each disk as an OCI artifact (`dciab-node-disk:<tag>-<platform>`) holding the disk and its `.sha256sum`. `bundle.lock` pins both (`nodeImages.published`), and `archive.py nodes` pulls each pinned disk by digest, checks its sha256 and keeps it with a `.sha256sum` file. A disk that isn't pinned yet is skipped with a note; the full archive (`--require-built`) refuses a missing Proxmox VE disk. The pinned node image itself is listed by the inventory like any other pinned image. Test tooling (`labRunner`), the pve-assistant's build base and the disks are kept out of the image inventory.

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

**Reproducible.** Entries are sorted, owned by 0:0 with fixed modes (755 or 644) and dated `SOURCE_DATE_EPOCH`, or the last commit's time. The same inputs give the same tar byte for byte, except the signatures: ECDSA signatures differ on every run. The tar isn't compressed, since images already are; `--zstd` writes `.tar.zst`. `package` refuses a payload that doesn't match its `SHA256SUMS`.

**What ships.** Only the paths in `SOURCES` in `package.py`, an allowlist of what the shipped `dciab` reads at run time plus the docs and examples. Inside them, caches, byte code, dot files and `tests` are skipped. In a git checkout only files git tracks ship, so a file left in `examples/` stays out until it's committed; the build lists what it left out. Outside a git checkout every file under those paths ships. A test checks that every path the CLI and its steps open is in the list. Everything else stays out: `handoff/`, `build/`, `tests/`, and `images/` (build recipes; CI builds those images).

**No secrets.** The build fails if a file under those paths, tracked or not, looks like a site secret: anything in a `secrets/` folder (in any case); a `*.key`, `*.pw`, `*.env`, `.env`, `*.age`, `kubeconfig`, `*.kubeconfig`, `*.p12`, `*.pfx`, `*.jks`, `*.ppk` or `htpasswd` file; or a file holding a private key (PEM, DER, PuTTY, a PKCS#12 bundle or a Java keystore), an age identity, an htpasswd entry, or a kubeconfig with a client key, token or password. Public certificates and public keys still ship. Keep a site's `dciab.yaml` and `secrets/` outside these paths, for example at the top of the checkout.

**Known gaps.** The check is a safety net, not a guarantee. It doesn't catch:

- a kubeconfig written as JSON;
- a kubeconfig whose user holds an OIDC token (`auth-provider` with an `id-token` or `refresh-token`);
- a DER private key with bytes after its outer SEQUENCE;
- a PKCS#8 v2 (OneAsymmetricKey) DER key, whose version is 1.

**Signing.** The site is offline, so keyless signing (which needs Fulcio and Rekor) doesn't apply: `--signing-key` takes a cosign key pair's private key, password in `COSIGN_PASSWORD`, and signs without a transparency log. Keep the private key out of the repository. The site gets the public key separately from the archive and runs:

```console
$ ./dciab verify-archive --key cosign.pub dciab-<VERSION>.tar   # before unpacking
$ ./dciab verify-archive --key cosign.pub                       # the unpacked archive this dciab is in
```

It checks `MANIFEST.sig` with `cosign verify-blob`, then hashes every file: a missing, changed or extra file fails. It uses `cosign` on `PATH` if there is one, else `payload/bin/cosign`. The archive's own cosign can't vouch for itself, so check with a cosign the site already trusts (`--cosign`) where you can. `--checksums-only` skips the signature for unsigned builds.

CI packages the small payload on every PR, signs it with a throwaway key, verifies the tar and the unpacked folder, checks a changed file fails, and rebuilds unsigned to compare byte for byte. The `archive payload` workflow does the same for the full payload.
