# Contributing to DCiaB

How DCiaB itself is developed: the repository, its tests and CI, and the agents that build it. This page is for the source repository only; the archive doesn't ship it. What DCiaB is and how a site runs it: [README.md](README.md) and [docs/](docs/README.md).

## Development setup

```console
$ pip install -r requirements-dev.txt
$ python hack/fetch_crds.py      # CAPI and provider CRDs pinned in bundle.lock; prints their folder
$ python -m pytest
```

DCiaB's Python floor is Rocky Linux 10's `python3` ([docs/VALIDATE.md](docs/VALIDATE.md)). Before pushing, also run `shellcheck` on the shell scripts `.github/workflows/ci.yml` lists.

## What CI checks

On every PR, on GitHub-hosted runners (`.github/workflows/ci.yml`):

- the pytest suite, and the schema and `dciab validate` checks;
- `docs/CONFIG.md` regenerated from the schema (`hack/gen_config_docs.py`): CI fails when they differ, or when a schema key has no description;
- both charts rendered with Helm and checked against the pinned CRDs: unknown fields, simple CEL rules, ClusterClass patch paths and the patches applied in order, template references, and topology variables;
- an end-to-end test on kind: Cluster API and every provider pinned in `bundle.lock`, the ClusterClasses and one Cluster per class, and the objects CAPI's topology controller generates. No VM or server is built. To run it locally, create a throwaway kind cluster and run `hack/e2e/run.sh`;
- `bundle.lock` image digests still match their registries, and ClusterClass revisions released in a GA release (`vN.0.0`) are unchanged ([docs/VERSIONING.md](docs/VERSIONING.md));
- every relative link in a page the archive ships resolves to a path the archive ships (`tests/test_docs_links.py`).

Anything that needs real infrastructure runs on the self-hosted lab runner, from `main` only ([CLAUDE.md](CLAUDE.md), "Test in CI, on the right runner").

## Repository layout

The archive carries only the paths in `build/package.py`'s `SOURCES`: the entrypoint, `VERSION`, `bundle.lock`, `lib/`, `schema/`, `charts/`, `addons/`, two scripts from `hack/`, `README.md`, `docs/` and `examples/`, plus its payload. Everything else stays in the repository.

```text
dciab                    the entrypoint
VERSION                  the release being developed, as SemVer; configs must match it (spec.bundleVersion)
bundle.lock              every pinned version and checksum: Kubernetes, CAPI and providers, images, charts, tools
schema/                  JSON Schema for dciab.yaml
lib/                     one package per command (dciab_validate, dciab_media, dciab_services, dciab_hypervisors,
                         dciab_seed, dciab_status, ...) and lib/steps/*.sh
charts/                  capi-clusterclasses (ClusterClasses, in frozen revisions) and capi-cluster (one cluster)
addons/                  Cilium, Longhorn, CSI drivers, the vSphere CPI, kubelet-csr-approver
examples/                complete site configs
docs/                    the product docs: operators' pages, reference, roadmap, versioning, ADRs (adr/)
images/                  image recipes: node-bootc (nodes), day0-chrony, backup, pve-assistant,
                         bootstrap-bootc (the appliance), usb (the DCiaB stick), lab-runner (test tooling)
build/                   the archive build and the release (build/README.md)
hack/                    checks, the kind e2e test, lab and Redfish tooling
handoff/                 planner and runner tasks, reports, lab facts, CI reviews
tests/                   the pytest suite
```

## Writing docs

`README.md`, `docs/` and component READMEs ship to sites, so they describe DCiaB for any site: lab specifics and test nuances go under `handoff/`. [docs/ROADMAP.md](docs/ROADMAP.md) is the one place that says what is built; other pages link to it. A shipped page links to a repository-only path by its GitHub URL (`https://github.com/TrevorBenson/dciab/blob/main/...`).

## How DCiaB is built

DCiaB is built by two agents: a planner that writes the code and docs, and a runner that runs tasks against real infrastructure.

| Page | What it covers |
| --- | --- |
| [CLAUDE.md](CLAUDE.md) | The rules for any change: branches, frozen class revisions, `bundle.lock`, product docs versus lab notes, tests in CI |
| [handoff/README.md](handoff/README.md) | How the planner and the runner hand work over: tasks, reports, and each one's rules |
| [build/README.md](build/README.md) | The archive build: image inventory, payload, packaging and signing |
| [docs/RELEASING.md](docs/RELEASING.md) | Cutting a release |
| [docs/VERSIONING.md](docs/VERSIONING.md) | Release names and their SemVer versions, the config schema, class revisions, charts and pins |
| CI review (2026-11-25): [baseline](handoff/ci-review/baseline.md), [findings](handoff/ci-review/findings.md), [recommendations](handoff/ci-review/recommendations.md) | Where PR feedback time goes, and a ranked plan to cut it and watch it |
