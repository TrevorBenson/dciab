# Working on DCiaB (for AI agents)

Read `README.md` for what DCiaB is, and `docs/ROADMAP.md` for what exists today and what doesn't. Don't assume a bootstrap step exists because the README lists it. The roadmap's checkboxes are the source of truth.

## Two agents, one repo

DCiaB is built by a **planner** (Claude in a cloud session: writes the code and docs, can't reach the lab) and a **runner** (Claude Code on Trevor's workstation: runs tasks against real infrastructure). They hand work over through `handoff/`; read **`handoff/README.md`** before doing anything else.

- **If you are the runner** (Claude Code on Trevor's machine), you only run tasks from `handoff/tasks/` and report in `handoff/reports/`. When Trevor says "run handoff task NNNN", follow that task file and the runner rules in `handoff/README.md`. Don't change code, charts, classes or docs. Report what's wrong instead.
- **If you are the planner,** follow the planner rules there.

## Rules for any change

- **Use a branch and a PR for every change.** Never push to `main`.
- **Never edit a released ClusterClass revision** (`charts/capi-clusterclasses/classes/rN/`). Read `docs/VERSIONING.md` first. CI enforces it.
- **Every version lives in `bundle.lock`.** Never inline a version in a chart or script.
- **Product docs stay product-focused.** `README.md`, `docs/` and component READMEs describe DCiaB for any site. Lab specifics and test nuances belong under `handoff/` (`LAB.md`, tasks, reports); see `handoff/README.md`.
- **Never commit secrets, built images, ISOs or large binaries.** Node images are built from the recipes in `images/node-bootc/` (the chosen one) or `images/node/`, and live on the hypervisor.
- **Before pushing, run:**
  - `python3 -m pytest -q`
  - `shellcheck` on the shell scripts CI lists in `.github/workflows/ci.yml`
- **Test in CI, on the right runner.** Every change adds or updates the CI that proves it, so the result is a pass or fail in GitHub Actions, not an agent session.
  - **Hosted runners** (`.github/workflows/ci.yml`): unit tests, schema and validate, chart renders, the kind e2e, the seed and services jobs. Anything that needs no real infrastructure.
  - **The self-hosted lab runner** (`runs-on: [self-hosted, dciab-lab]`): anything that needs real infrastructure. Add or extend a reusable lab workflow (`lab-template.yml`, `lab-cluster.yml`, `lab-mgmt.yml`, or a new `lab-*.yml` called from `lab.yml`), following the rules `tests/test_lab_runner.py` checks: the `dciab-lab` concurrency group, actions pinned by commit SHA, `contents: read`, and cleanup of what the job creates. Lab workflows run from `main` only, so the PR says which lab run proves it after merge.
  - **Handoff tasks** only for what CI can't do: human gates, physical media, hardware the lab doesn't have, or a one-off spike (`handoff/README.md`).

## Where things are

| Path | What |
| --- | --- |
| `bundle.lock` | Every pinned version and checksum |
| `schema/`, `lib/dciab_validate/` | Site config schema and `dciab validate` |
| `charts/capi-clusterclasses/` | ClusterClasses (vSphere, Proxmox, Metal3), in frozen revisions |
| `charts/capi-cluster/` | One cluster: Cluster topology + node IP pools |
| `images/node-bootc/` | Node image, the chosen recipe: Rocky 10 bootc (RESF SIG recipe) with CRI-O; disks for Proxmox and others |
| `images/node/` | Older node template recipe (image-builder, containerd); removed once a cluster passes on bootc |
| `images/lab-runner/` | Self-hosted Actions runner for infrastructure tests (test tooling, never in the archive) |
| `hack/` | Checks and the kind e2e test (`hack/e2e/run.sh`) |
| `handoff/` | Planner ↔ runner tasks and reports |
| `docs/` | Components (what a site runs), roadmap, versioning, ADRs |
