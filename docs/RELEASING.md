# Releasing DCiaB

How a release (`r1alpha1`, `r1beta1`, `r1`, ...) is cut. [VERSIONING.md](VERSIONING.md) says what the names mean and when a release is worth cutting. Merging a PR is never a release.

## What a release is

- A git tag `v` + `VERSION` (`v1.0.0-alpha.1`) on a commit of `main`.
- A GitHub release titled `r1alpha1 (v1.0.0-alpha.1)`. Alpha and beta are marked as pre-releases.
- Assets:
  - `dciab-<VERSION>.tar`: the archive (the repository, the payload, a signed MANIFEST). An archive of 2 GiB or more is attached as `dciab-<VERSION>.tar.part-NN` pieces, because GitHub refuses larger assets. Rejoin them with `cat dciab-<VERSION>.tar.part-* > dciab-<VERSION>.tar`.
  - `dciab-<VERSION>.tar.sha256`: the sha256 of the whole archive.
  - `cosign.pub`: the release public key.
  - `MANIFEST`: the archive's file list with sha256s, readable without downloading the archive.

A site checks an archive offline:

```sh
sha256sum -c dciab-1.0.0-alpha.1.tar.sha256
./dciab verify-archive --key cosign.pub dciab-1.0.0-alpha.1.tar
```

Get `cosign.pub` from somewhere other than the download it checks: the repository's `keys/dciab-release.pub`, or a copy the site already trusts.

## The release key

Archives are signed with a cosign key pair, the same way `build/package.py` signs every archive.

- **The private key and its password** are held by the project owner. They are generated offline (`cosign generate-key-pair`) and kept outside the repository, with an offline backup.
- On GitHub, the private key and its password exist only as the secrets `DCIAB_RELEASE_COSIGN_KEY` and `DCIAB_RELEASE_COSIGN_PASSWORD` of the **`release` Environment**. That Environment has the owner as a required reviewer and allows deployments from `main` only.
- **The public key** is committed as `keys/dciab-release.pub` and attached to every release. A real release run refuses to start without it.

Rotating the key means a new pair, new Environment secrets and a PR replacing `keys/dciab-release.pub`. Archives signed with the old key still verify with the old public key, which stays attached to their releases.

## Cutting a release

1. **Check the stage's gates** in [VERSIONING.md](VERSIONING.md) and the [roadmap](ROADMAP.md).
2. **Make sure `VERSION` on `main` is the cut you want** (`1.0.0-alpha.1` for `r1alpha1`). `python3 build/release.py plan --ref refs/heads/main` prints what would be published.
3. **Dry run.** Actions → `release` → Run workflow on `main`, with `dry_run` checked (the default). It checks `VERSION` and that the tag is new, builds the full payload, packages and signs it with a throwaway key, verifies it and lists the assets in the run summary. It needs no secrets and publishes nothing.
4. **Real run.** The same, with `dry_run` unchecked. The `release` Environment asks its reviewers to approve, once for the build (which reads the key) and once for publishing. The run signs with the release key, verifies the archive with `keys/dciab-release.pub`, then creates the release as a draft, uploads the assets and publishes it. Publishing creates the tag on the commit the run built.
5. **Move `VERSION` to the next cut** in a PR: `python3 build/release.py next` prints it (`1.0.0-alpha.2`), and `--stage beta` or `--stage ga` moves to the next stage. Bumping `VERSION` also rebuilds the lab runner image; re-pin `labRunner` in `bundle.lock` afterwards (VERSIONING.md).

The workflow never runs on a push or a merge, and it refuses to run from any branch but `main` or to reuse a tag that exists.

## Before the full archive can be built

The full payload includes the published node disks (`nodeImages.published` in `bundle.lock`). It fails until the lab workflow `lab-node-image.yml` has published the Proxmox VE disk and its pins are in `bundle.lock` ([build/README.md](../build/README.md)).
