# ADR 0003: Copy archive images with oras, not skopeo

- Status: accepted
- Date: 2026-11-22

## Context

The archive carries every container image a site needs, in one OCI image layout (`images/` in the payload). At the site, `dciab seed` loads them into the site registry, and nodes and controllers pull them from there.

Most of those images are multi-platform. Their source manifests, and `bundle.lock`, name them by the digest of the **index** (an OCI image index or a Docker manifest list), not by the digest of one platform's image. A node asks the site registry for that exact index digest. So the archive must hold:

- the index, byte for byte, so its digest doesn't change, and
- every image the index lists. A registry refuses an index whose images it doesn't hold, so you can't keep the index and drop the other platforms.

Changing a single byte of the index changes its digest, and every manifest that names the old digest then fails to pull.

## What happened with skopeo

The first implementation used `skopeo copy --all --preserve-digests docker://<image> oci:<layout>:<name>`, as the usual offline-mirroring recipes do. CI (skopeo 1.21.0-dev on `ubuntu-latest`) showed two problems.

1. **It refuses a reference with both a tag and a digest.** `docker://ghcr.io/kube-vip/kube-vip:v1.3.1@sha256:…` fails with `Invalid source name`. That one is easy to work around: copy `repo@digest` and keep the tag separately.
2. **It can't write a Docker manifest list into an OCI layout without converting it.** Copying `registry.k8s.io/pause@sha256:f548e0e8…` (a Docker manifest list) failed with:

   ```text
   Manifest list must be converted to type "application/vnd.oci.image.index.v1+json"
   to be written to destination, but we cannot modify it: "Instructed to preserve digests"
   ```

   skopeo's OCI layout destination accepts only OCI media types. A Docker manifest list (`application/vnd.docker.distribution.manifest.list.v2+json`) or a Docker schema 2 manifest must be converted to its OCI equivalent on the way in. Converting rewrites the JSON, so the digest changes. With `--preserve-digests`, skopeo stops rather than convert. Without it, skopeo converts quietly and the layout ends up holding an index under a new digest that nothing references.

Upstreams still publish Docker manifest lists, `registry.k8s.io/pause` (which every node needs) among them, and the media type can change with any release. So skopeo can't be relied on to build the archive's layout.

These workarounds were considered and rejected:
- **One `dir:` folder per image.** It keeps the manifest bytes, but there's no shared blob store (layers are duplicated across images) and no standard layout that a registry or other tools can read.
- **One platform only.** Selecting a platform changes the digest, as above.
- **Converting, then rewriting every manifest to the new digests.** That breaks the provenance chain from upstream to the site, and every manifest from upstream would need patching.

## Decision

The archive build copies images with **oras** (`oras cp --to-oci-layout <repo>@<digest> <layout>:<name>`), pinned in `bundle.lock` (`tools.oras`, and its sha256 in `binarySHA256`).

oras copies the content graph as it is: the index, each platform's manifest and every blob, with their original media types and bytes. The OCI image layout specification allows any media type in the blob store and the index, so a Docker manifest list is stored unchanged and keeps its digest.

`build/archive.py images` checks the result itself instead of trusting the tool: after each copy it reads the layout's `index.json` and confirms that the name it wrote maps to the expected digest. On a rerun, an image is skipped only when that entry and its top-level manifest blob are present and intact.

Image names inside the layout are valid OCI tags (`<repository>_<tag>`, with characters outside `[A-Za-z0-9._-]` replaced by `_`, and hashed when longer than 128 characters). `images.yaml` maps each name back to the source reference, its tag and its digest, which is what `dciab seed` uses to push each image to the site registry under its original repository, by digest and by tag.

skopeo keeps one job: `archive.py digests` and `hack/check_image_digests.py` use `skopeo inspect --raw` to read a manifest's exact bytes and hash them. That's read-only, so nothing gets converted.

## Consequences

- oras ships in the archive (`bin/oras`), and `dciab seed` uses it to copy images from the layout into the site registry. The same tool and the same byte-for-byte behavior apply at both ends.
- Every platform of every image is in the archive. That costs space. Trimming platforms would need a registry that serves a trimmed index under a new digest, plus manifests rewritten to match; that isn't planned.
- Revisit this if skopeo's OCI layout destination ever accepts Docker media types unchanged with `--preserve-digests`. Until then, CI's small payload copies `registry.k8s.io/pause` (a Docker manifest list) on every PR, which is exactly the case skopeo fails.
