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
- **Never commit secrets, built images, ISOs or large binaries.** Node images are built from the recipe in `images/node/` and live on the hypervisor.
- **Before pushing, run:**
  - `python3 -m pytest -q`
  - `shellcheck` on the shell scripts CI lists in `.github/workflows/ci.yml`

## Where things are

| Path | What |
| --- | --- |
| `bundle.lock` | Every pinned version and checksum |
| `schema/`, `lib/dciab_validate/` | Site config schema and `dciab validate` |
| `charts/capi-clusterclasses/` | ClusterClasses (vSphere, Proxmox, Metal3), in frozen revisions |
| `charts/capi-cluster/` | One cluster: Cluster topology + node IP pools |
| `images/node/` | Node template build (image-builder; Rocky 10 on Proxmox) |
| `hack/` | Checks and the kind e2e test (`hack/e2e/run.sh`) |
| `handoff/` | Planner ↔ runner tasks and reports |
| `docs/` | Roadmap, versioning |
