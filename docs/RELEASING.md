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

A site checks an archive offline with a key and a verifier it trusts, never the copies inside the archive being checked:

- **The key:** `keys/dciab-release.pub` at the release's tag. The release notes print its sha256 fingerprint; the attached `cosign.pub` is the same file, so check its sha256 against the fingerprint before using it.
- **The verifier:** cosign on the PATH (any recent release), or `dciab` from a checkout of the tag.

From a checkout of the tag (the recommended check: the signature, every listed file, and nothing unlisted added):

```sh
cat dciab-1.0.0-alpha.1.tar.part-* > dciab-1.0.0-alpha.1.tar     # only if the release has pieces
./dciab verify-archive --key keys/dciab-release.pub dciab-1.0.0-alpha.1.tar
```

With cosign alone, all four checks are needed. The last one matters: MANIFEST only covers the files it lists, so a file added to a tampered download (a cached `.pyc`, say) would pass the first three.

```sh
tar -xf dciab-1.0.0-alpha.1.tar dciab-1.0.0-alpha.1/MANIFEST dciab-1.0.0-alpha.1/MANIFEST.sig
cosign verify-blob --key cosign.pub --insecure-ignore-tlog=true \
  --signature dciab-1.0.0-alpha.1/MANIFEST.sig dciab-1.0.0-alpha.1/MANIFEST
tar -xf dciab-1.0.0-alpha.1.tar && cd dciab-1.0.0-alpha.1 && sha256sum -c --quiet MANIFEST
find . ! -type d ! -name MANIFEST ! -name MANIFEST.sig | sed 's|^\./||' | sort \
  | diff - <(cut -c67- MANIFEST | sort) && echo 'nothing unlisted'
```

The `.sha256` asset isn't signed; it only catches a damaged download. The archive is signed without a transparency-log entry (it is built for offline sites), hence `--insecure-ignore-tlog`.

## The release key

Archives are signed with a cosign key pair, the same way `build/package.py` signs every archive.

- **The private key and its password** are held by the project owner. They are generated offline (`cosign generate-key-pair`) and kept outside the repository, with an offline backup.
- On GitHub, the private key and its password exist only as the secrets `DCIAB_RELEASE_COSIGN_KEY` and `DCIAB_RELEASE_COSIGN_PASSWORD` of the **`release` Environment**. That Environment has the owner as a required reviewer and allows deployments from `main` only.
- **The public key** is committed as `keys/dciab-release.pub` and attached to every release. A real release run refuses to start without it. Its sha256 fingerprint is printed in every release's notes; once the key exists, record it here too: `sha256sum keys/dciab-release.pub`.
- **Only one job ever reads the key.** The release workflow builds and packages the archive in a job with no environment and no secrets. A separate job, `sign-and-publish`, is the only one on the `release` Environment: it runs only this repository's code and cosign (checked against `bundle.lock`), signs the archive (`build/package.py finalize`), verifies it against `keys/dciab-release.pub` and publishes it. Every action in the workflow is pinned by commit SHA.

Rotating the key means a new pair, new Environment secrets and a PR replacing `keys/dciab-release.pub`. Archives signed with the old key still verify with the old public key, which stays attached to their releases.

## Cutting a release

1. **Check the stage's gates** in [VERSIONING.md](VERSIONING.md) and the [roadmap](ROADMAP.md).
2. **Make sure `VERSION` on `main` is the cut you want** (`1.0.0-alpha.1` for `r1alpha1`). `python3 build/release.py plan --ref refs/heads/main` prints what would be published.
3. **Dry run.** Actions → `release` → Run workflow on `main`, with `dry_run` checked (the default). It checks `VERSION` and that the tag is new, builds the full payload, packages it, signs a copy with a throwaway key (the same `finalize` step a real run uses), verifies it and lists the assets in the run summary. It needs no secrets and publishes nothing.
4. **Real run.** The same, with `dry_run` unchecked. The build runs without secrets; then the `release` Environment asks its reviewers to approve the `sign-and-publish` job, once. That job signs the archive with the release key, verifies it with `keys/dciab-release.pub`, checks the tag is still new, creates the release as a draft, uploads the assets and publishes it. Publishing creates the tag on the commit the run built, and the job checks the tag points at that commit.
5. **Move `VERSION` to the next cut** in a PR: `python3 build/release.py next` prints it (`1.0.0-alpha.2`), and `--stage beta` or `--stage ga` moves to the next stage. Bumping `VERSION` also rebuilds the lab runner image; re-pin `labRunner` in `bundle.lock` afterwards (VERSIONING.md).

The workflow never runs on a push or a merge, and it refuses to run from any branch but `main` or to reuse a tag that exists.

## Before the full archive can be built

The full payload includes the published node disks (`nodeImages.published` in `bundle.lock`). It fails until the lab workflow `lab-node-image.yml` has published the Proxmox VE disk and its pins are in `bundle.lock` ([build/README.md](../build/README.md)).
