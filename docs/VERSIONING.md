# Versioning

DCiaB carries several version numbers. They change for different reasons, and only one of them is a release.

| What | Example | Where | Changes when |
| --- | --- | --- | --- |
| DCiaB release | `r1alpha1`, which is SemVer `1.0.0-alpha.1` | `VERSION` (SemVer), git tag `v1.0.0-alpha.1`, GitHub release named `r1alpha1` | A release is cut |
| Site config schema | `dciab.io/v1alpha1` | `apiVersion` in `dciab.yaml`, `schema/dciab.schema.json` | The config format changes incompatibly |
| ClusterClass revision | `r1` | `charts/capi-clusterclasses/classes/rN/`, class names `dciab-<provider>-rN` | The first class change after a general availability (GA) release |
| Chart version | `0.4.0` | `charts/*/Chart.yaml` | Any change to a chart |
| Upstream pins | CAPI `v1.14.4` | `bundle.lock` | An upstream component is upgraded |

## DCiaB releases

### Names

Releases are named the way Kubernetes names API versions, with `r` (release) in place of `v`, so a release is never read as an API version: `r1alpha1`, `r1alpha2`, `r1beta1`, `r1`.

| Stage | Names | What it means | Testing that gates it |
| --- | --- | --- | --- |
| Alpha | `rNalpha1`, `rNalpha2`, ... | A lean proof of concept of the stage's scope. Breaking changes are allowed between alphas | Unit tests and the hosted CI jobs |
| Beta | `rNbeta1`, `rNbeta2`, ... | Feature-complete for the release and stabilizing | End-to-end tests on real infrastructure (the lab runner) |
| GA | `rN` | General availability: published for sites to depend on | Every gate in [ROADMAP.md](ROADMAP.md) for `rN` |

A stage can have as many cuts as it needs: work out as many bugs as possible in alpha (unit tests) and beta (end-to-end tests) before `rN` is published.

### What each release covers

**Release 1 follows the site's lifecycle** ([GLOSSARY.md](GLOSSARY.md), phases of a site):

- **`r1alpha` is Day 0.** Bootstrap from bare servers or an existing hypervisor: the bootstrap host and its Day 0 services (DNS, NTP, HTTP), hypervisors installed over Redfish, the seed registry, kind, MGMT built and pivoted, and the minimal INFRA services Day 0 or the pivot need. Basic unit tests for all of it. **Proxmox VE is required; vSphere is best-effort.** vSphere becomes a parallel lane once an ESXi lab is available, or moves to `r1beta`.
- **`r1beta` is Day 1.** From the bootstrap host to production: the Day 0 DNS, NTP and Redfish dependencies pivot to production services, hosted by DCiaB's INFRA or provided by the site, and DHCP comes up there; like-for-like moves from hypervisors DCiaB installed to the site's own (vSphere to vSphere, Proxmox VE to Proxmox VE); tested Argo CD app-of-apps Helm templates for INFRA and for Cluster API tenant clusters. vSphere is required from here.
- **`r1` is Day 2.** Production hardening and running the site: persistent storage, backups and restores, upgrades and the rest of Day 2 operations. Day 2 work is built and tested end to end in further `r1beta` cuts once the Day 1 scope is in; `r1` is published when the `r1` milestone and its gates are done.

Moving clusters between platforms (vSphere to Proxmox VE or back, including what vMotion does today) is in `r1` only if a simple, low-cost way is found. Otherwise it is roadmap work for a later release.

**Later releases (`r2`, `r3`, ...) follow a frozen roadmap**, not the site's lifecycle. Before `rN` development starts, its roadmap is frozen. Anything not on it goes to `rN+1`, unless a frozen item can't be finished without it.

- `rNalphaM`: a proof of concept of the frozen items; breaking changes allowed.
- `rNbetaM`: the frozen items stable and complete, ready for pre-release testing.
- `rN`: GA.

**Fixes after GA go into the next release.** Once `rN` is published, bug fixes land in `rN+1alpha` along with its frozen roadmap. There are no patch releases.

### How a release name maps to SemVer

Tools need SemVer: `sort -V`, GitHub's latest release, Renovate, Helm, OCI tags. So every release has one SemVer version, derived mechanically from its name, and that is the version machines read. The `rN` name is what people say, plan and track with.

| Release | SemVer (`VERSION`, `spec.bundleVersion`) | Git tag |
| --- | --- | --- |
| `rNalphaM` | `N.0.0-alpha.M` | `vN.0.0-alpha.M` |
| `rNbetaM` | `N.0.0-beta.M` | `vN.0.0-beta.M` |
| `rN` | `N.0.0` | `vN.0.0` |

Minor and patch stay `0`, because fixes ship in the next release. SemVer's precedence orders these correctly: `1.0.0-alpha.2` < `1.0.0-alpha.10` < `1.0.0-beta.1` < `1.0.0` < `2.0.0-alpha.1`.

- **`VERSION`** holds the SemVer version of the release being developed. Right after a tag is cut, `VERSION` moves to the next cut (`1.0.0-alpha.1` to `1.0.0-alpha.2`, or to `1.0.0-beta.1` once the alpha stage is done).
- **A release** is a git tag `v` + `VERSION` on a commit of `main`, plus a GitHub release titled with the `rN` name and the SemVer version: `r1alpha1 (v1.0.0-alpha.1)`.
- **The archive** is named after `VERSION` (`dciab-1.0.0-alpha.1.tar`). A site config names the archive it was written for in `spec.bundleVersion`, and `dciab validate` rejects a config written for a different archive, because the config has to match that archive's `bundle.lock`.
- **Images DCiaB builds** (the Day 0 chrony image, the backup image, the lab runner) keep content-derived tags and are pinned by digest in `bundle.lock`. They reach a site inside an archive, so they don't need the release version. The lab runner's tag starts with `VERSION` (`<VERSION>-<id>`), so every bump of `VERSION` rebuilds and republishes it on the lab runner; re-pin `labRunner` in `bundle.lock` afterwards.

Merging a PR is not a release. Releases are cut when there is something worth testing or shipping, not to unlock development.

### Tracking

GitHub issues carry two things:

- **A milestone per stage: `r1alpha`, `r1beta`, `r1`.** It says which release stage the work must be finished in. A stage's milestone is done when its scope and gates are; the last cut of that stage is then tagged, and the next cut starts the following stage. The alpha and beta cuts inside a stage (`r1alpha1`, `r1alpha2`, ...) are tags, not milestones. For `r2` and later, the milestone is created when the roadmap is frozen.
- **A label per lifecycle phase: `day0`, `day1`, `day2`.** It says what kind of work it is, so lanes can keep Day 0, Day 1 and Day 2 work apart even when they touch the same files. In release 1 the label usually matches the milestone. Work done ahead of its stage keeps its label, and `r2` work is labelled the same way. `platform:vsphere` marks the vSphere lane. `tooling` marks CI, runner and repository work that doesn't ship in an archive.

An issue with no milestone isn't scheduled yet.

## Site config schema: `dciab.io/v1alpha1`

`dciab.yaml` uses Kubernetes-style `apiVersion` and `kind` so the format reads familiarly and can be versioned the same way. It is a file format checked by `schema/dciab.schema.json`; it is **not** a CRD and is never applied to a cluster.

The schema version is independent of the release: it changes only when the format changes incompatibly, so most releases keep it. It follows the Kubernetes convention for maturity:

- `v1alpha1` now: the format may still change between releases without a migration path.
- `v1beta1` once sites depend on it, before `r1` is published: changes come with a migration path or a deprecation period.
- `v1` once it is stable.

If DCiaB later adds real CRDs (for example a controller that reconciles site state), each starts at `v1alpha1` under `dciab.io` and follows the same path.

## ClusterClass revisions: `rN`

### Why revisions exist

Changing a ClusterClass rolls every cluster that uses it; most changes replace nodes. So a class that clusters depend on must never change. Instead a new **revision** is added beside it, and clusters move to it one at a time, in an order the operator chooses.

The objects themselves are ordinary `cluster.x-k8s.io/v1beta2` ClusterClasses; `r1` only names which DCiaB revision of their contents this is.

### Draft and released

- A revision is a **draft** until a GA release tag (`vN.0.0`) contains it. Alpha and beta tags freeze nothing. Drafts change in place, as often as needed: every PR before GA edits the same draft. A management cluster that installed an older copy of the draft can't always be upgraded to the new one (Cluster API machine templates are immutable), so a management cluster with an older copy of the draft installed must be recreated to use the new one.
- A revision is **released** once the latest GA tag reachable from `HEAD` contains it. From then on its files never change.
- There is **at most one draft** at a time.

So revisions move at GA pace, and **a class revision is numbered after the release that freezes it:** `r1` GA freezes classes `r1`; the first class change after it starts draft `r2`, which `r2` GA freezes. A release with no class changes freezes none, so a revision number can be skipped.

`hack/check_frozen_classes.sh` enforces this in CI. It fails if a file of a released revision was modified, renamed, deleted or changed in type, or if more than one draft exists.

### Lifecycle

1. **Draft.** `classes/rN/` changes freely until GA `rN` includes it.
2. **Released.** The GA tag freezes it.
3. **Next change.** The first class change after GA `rN` starts the next draft, named after the release that will freeze it: copy `classes/rN/` to `classes/rM/`, rename the objects (`-rN` to `-rM`), edit the copy, add `rM` to `revisions` in `charts/capi-clusterclasses/values.yaml`, and set `appVersion` in its `Chart.yaml`. Later changes before that GA go into the same draft.
4. **Move clusters.** Change `classRevision` in a cluster's `capi-cluster` values, one PR per cluster: workload clusters first, then INFRA, then MGMT. CAPI rebases the cluster onto the new class and rolls the nodes it has to.
5. **Drop.** Remove a revision from `revisions` once no cluster uses it. Its files stay in the repo, frozen.

The tests check only the revisions the chart installs, so a dropped revision is never rewritten to satisfy newer checks.

### Where revisions are used

- `charts/capi-clusterclasses/values.yaml`: `revisions`, the list of revisions installed on the management cluster.
- `charts/capi-cluster/values.yaml`: `classRevision`, the revision a cluster uses. The cluster's `classRef` is `dciab-<class>-<classRevision>`.

There has been no GA release yet, so `r1` is a draft.

## Chart versions

Each chart in `charts/` has its own SemVer `version`, bumped on any change to the chart, so a rendered chart can be traced to its source. The charts ship inside the archive, not to a chart repository, so the archive's release version is what a site sees; the chart version is not tied to it or to class revisions. `capi-clusterclasses` also sets `appVersion` to its newest class revision.

## Upstream pins

`bundle.lock` pins every upstream version the archive ships: Kubernetes, Cluster API and its providers, images (by digest), charts and tools. Downloaded manifests are pinned by SHA-256 in its `checksums` section and checked by `hack/fetch_crds.py`. Changing a pin is an ordinary PR; it reaches sites with the next release.
