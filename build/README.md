# Archive build

DCiaB ships as one archive that a site unpacks on the bootstrap host and runs offline. Everything in it is pinned by `bundle.lock`. The build is code here, run by CI; nothing in the archive is fetched at the site.

## Pieces

| Piece | Status | What |
| --- | --- | --- |
| Image inventory (`inventory.py`) | done | Every container image a site needs, and where each comes from |
| Image pull | next | Each image pulled by digest into an OCI layout; tag-only images get their digest resolved once, then recorded |
| Charts and providers | next | Third-party charts as `.tgz`, checked against `bundle.lock` (`chartDigests`); CAPI provider manifests from `hack/fetch_crds.py` (`checksums`) |
| Binaries | next | kind, kubectl, helm and clusterctl, pinned by sha256 |
| Node images | later | The bootc node image as an OCI image, plus disks per platform where a platform needs one |
| SBOM and signatures | later | An SBOM per image and binary, and a signed manifest of the archive |

## Image inventory

```console
$ crds="$(python3 hack/fetch_crds.py)"
$ addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > cilium.yaml
$ kubeadm config images list --kubernetes-version v1.36.6 > kubeadm.txt
$ python3 build/inventory.py --manifests "$crds" --extra cilium.yaml --kubeadm-images kubeadm.txt \
    --image "$(kind create cluster --help | sed -n 's/.*default "\(kindest[^"]*\)".*/\1/p')=kind node image"
```

The sources are:
- **`bundle.lock`:** kube-vip and the Day 0 service images, already pinned by digest.
- **The Cluster API, provider and cert-manager manifests**, rendered with the provider variables DCiaB installs them with.
- **Rendered add-ons:** Cilium.
- **The Kubernetes control plane images** kubeadm pulls for the bundle's version.
- **The kind node image** the bootstrap cluster boots.

Images marked `*` have no digest in their source manifest. The pull step resolves each one and records its digest.

CI runs the inventory on every PR (job `archive image inventory`) and uploads it as `images.yaml`.
