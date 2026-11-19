# Versioning

DCiaB carries several version numbers. They change for different reasons, and only one of them is a release.

| What | Example | Where | Changes when |
| --- | --- | --- | --- |
| DCiaB release | `v0.1.0` | `VERSION`, git tag `vX.Y.Z` | A release is cut |
| Site config schema | `dciab.io/v1alpha1` | `apiVersion` in `dciab.yaml`, `schema/dciab.schema.json` | The config format changes incompatibly |
| ClusterClass revision | `r1` | `charts/capi-clusterclasses/classes/rN/`, class names `dciab-<provider>-rN` | The first class change after a release |
| Chart version | `0.4.0` | `charts/*/Chart.yaml` | Any change to a chart |
| Upstream pins | CAPI `v1.14.4` | `bundle.lock` | An upstream component is upgraded |

## DCiaB release

`VERSION` holds the archive's SemVer version. A release is a git tag `vX.Y.Z` that matches it, on a commit of `main`. Prerelease tags (`v0.2.0-rc.1`) are for testing and freeze nothing.

A site config must name the archive it was written for in `spec.bundleVersion`. `dciab validate` rejects a config written for a different archive, because the config has to match the pins in that archive's `bundle.lock`.

Merging a PR is not a release. Releases are cut when there is something worth shipping to a site, not to unlock development.

## Site config schema: `dciab.io/v1alpha1`

`dciab.yaml` uses Kubernetes-style `apiVersion` and `kind` so the format reads familiarly and can be versioned the same way. It is a file format checked by `schema/dciab.schema.json`; it is **not** a CRD and is never applied to a cluster.

The version follows the Kubernetes convention for maturity:

- `v1alpha1` now: the format may still change between releases without a migration path.
- `v1beta1` once sites depend on it: changes come with a migration path or a deprecation period.
- `v1` once it is stable.

If DCiaB later adds real CRDs (for example a controller that reconciles site state), each starts at `v1alpha1` under `dciab.io` and follows the same path.

## ClusterClass revisions: `rN`

### Why revisions exist

Changing a ClusterClass rolls every cluster that uses it; most changes replace nodes. So a class that clusters depend on must never change. Instead a new **revision** is added beside it, and clusters move to it one at a time, in an order the operator chooses.

The suffix is `r` (revision), not `v`, so it is not read as a Kubernetes API version. The objects themselves are ordinary `cluster.x-k8s.io/v1beta2` ClusterClasses; `r1` only names which DCiaB revision of their contents this is.

### Draft and released

- A revision is a **draft** until a DCiaB release tag contains it. Drafts change in place, as often as needed: every PR before the release edits the same draft.
- A revision is **released** once the latest `vX.Y.Z` tag reachable from `HEAD` contains it. From then on its files never change.
- There is **at most one draft** at a time.

So revisions move at release pace, not PR pace: ten PRs between two releases produce one new revision, not ten. A release with no class changes produces none.

`hack/check_frozen_classes.sh` enforces this in CI. It fails if a file of a released revision was modified, renamed, deleted or changed in type, or if more than one draft exists.

### Lifecycle

1. **Draft.** `classes/rN/` changes freely until a release includes it.
2. **Released.** The release tag freezes it.
3. **Next change.** The first class change after a release starts the next draft: copy `classes/rN/` to `classes/rN+1/`, rename the objects (`-rN` to `-rN+1`), edit the copy, add `rN+1` to `revisions` in `charts/capi-clusterclasses/values.yaml`, and set `appVersion` in its `Chart.yaml`. Later changes before the next release go into the same draft.
4. **Move clusters.** Change `classRevision` in a cluster's `capi-cluster` values, one PR per cluster: workload clusters first, then INFRA, then MGMT. CAPI rebases the cluster onto the new class and rolls the nodes it has to.
5. **Drop.** Remove a revision from `revisions` once no cluster uses it. Its files stay in the repo, frozen.

The tests check only the revisions the chart installs, so a dropped revision is never rewritten to satisfy newer checks.

### Where revisions are used

- `charts/capi-clusterclasses/values.yaml`: `revisions`, the list of revisions installed on the management cluster.
- `charts/capi-cluster/values.yaml`: `classRevision`, the revision a cluster uses. The cluster's `classRef` is `dciab-<class>-<classRevision>`.

There has been no release yet, so `r1` is a draft.

## Chart versions

Each chart in `charts/` has its own SemVer `version`, bumped on any change to the chart, so a rendered chart can be traced to its source. It is not tied to the DCiaB release or to class revisions. `capi-clusterclasses` also sets `appVersion` to its newest class revision.

## Upstream pins

`bundle.lock` pins every upstream version the archive ships: Kubernetes, Cluster API and its providers, images (by digest), charts and tools. Downloaded manifests are pinned by SHA-256 in its `checksums` section and checked by `hack/fetch_crds.py`. Changing a pin is an ordinary PR; it reaches sites with the next release.
