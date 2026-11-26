# CI findings (2026-11-25)

Answers to the review's questions, with evidence. Timings come from
[baseline.md](baseline.md); file references are to `main` at `6705aef`. Every number is
labelled **measured** (from the Actions API or a job log), **local** (measured on this
review's 2-vCPU VM), **extrapolated** or **assumed**.

Re-raised from #240: only the reinstall leg's pre-installed disk, which #240 explicitly
left for later ("not done here"). Nothing #240 shipped is proposed again.

## The headline

1. **PR feedback has two shapes.** Without the emulated lab, ≈3.2 min p50 (measured, set by
   the seed job). With it (52% of PR pushes, measured), ≈10 min p50 and ≈15 min p95
   (measured, set by the `untagged-reinstall` leg). The second shape is where the minutes are.
2. **#240 pushed one PR push past GitHub Free's 20 concurrent jobs.** 18 `ci.yml` jobs + 3
   emulated legs = 21. After #240, 20% of jobs queued > 20 s, against 4% before, and the
   count of running jobs never exceeded 20 (measured). Job queue p95 is now 45–116 s.
3. **Most of the critical path is the e2e work itself**: 71% of the seed job, 80% of the
   reinstall leg (measured). The real levers are fewer sequential installs, caching what's
   pinned by digest, and fewer jobs. Interpreter speed, `uv` and Mojo barely matter here.

## Q1. Image and layer caching

**Do parallel jobs rebuild the same images?** Yes.

| Image | Built in | Builds per PR push | Each (p50, measured) |
| --- | --- | --- | --- |
| Day 0 chrony | `day0-chrony` (ci.yml:677), `services` (ci.yml:804), every emulated leg (hypervisors-emulated.yml:110–114) | 2, or 5 with the emulated lab | 10–11 s |
| pve-assistant | `pve-assistant` (ci.yml:570), every emulated leg (hypervisors-emulated.yml:110–114) | 1, or 4 | 16 s |
| backup | `backup-image` (ci.yml:729) | 1 | 16 s |

- **No BuildKit/Buildah layer cache anywhere.** The three `build.sh` scripts run
  `podman build --pull=missing` (images/day0-chrony/build.sh:36, images/pve-assistant/build.sh:35,
  images/backup/build.sh:83): no `--cache-from`/`--cache-to`, no registry or GHA cache
  backend, and every hosted runner starts empty, so every build is cold.
- **No shared prebuilt base.** But the recipes already publish to GHCR on `main` and have a
  content id (`build.sh --recipe-id` for chrony and backup, `--print-pins` for pve-assistant).
  A job could pull the image for the current recipe id when it exists and build only when
  the recipe changed. That's cheaper than adding a layer cache.
- **Layer ordering**: the images are small single-purpose recipes (a base, `dnf install`, a
  copy). Ordering isn't the problem; cold starts are.
- **Duplicated build time per run** (extrapolated from the p50s): chrony 3 extra builds ×
  ≈10 s, pve-assistant 3 extra × ≈16 s ≈ **80 s of runner time** per emulated-triggering push.
  On the critical path: **29 s** in the reinstall leg (measured step, both images).

**The biggest pull is not an image build:** the seed job's "Payload" step pulls 1.7 GB of
images and 415 MB of binaries with `oras` on every run, 42 s p50 (measured, ci.yml:408–421).
Everything in it is pinned by digest or sha256 in `bundle.lock`, so it's safe to cache on a
key of `bundle.lock` plus the build scripts. `day0-preload` (56 s, ci.yml:1028–1051) and
`archive-inventory` pull overlapping sets.

**Not an image, but found while reading build logs:** the `backup image` job's tool check
(ci.yml:737–740) takes 61 s, and 60.2 s of it is silence after `clusterctl version` prints
(job 139632010496 log, 01:36:20.24 → 01:37:20.46). Likely cause: `clusterctl`'s update
check, or the image's `--help`, waiting on a network that `--network none` removed.
Confirming which needs one timed run (`CLUSTERCTL_DISABLE_VERSIONCHECK=true`, then time each
command). Off the critical path, but it holds a runner slot for a minute (see the 20-job cap).

## Q2. Change-scoped testing

**Package graph** (from imports in `lib/`, measured):

```
dciab_validate → backup, cluster, identity, storage
dciab_backup   → cluster, storage        dciab_storage → backup        (cycle)
dciab_cluster  → identity, storage, validate                           (cycle via validate)
dciab_services → hypervisors, seed, validate
dciab_hypervisors → seed, services, state, validate                    (cycle with services)
dciab_seed     → services, state                                       (cycle with services)
dciab_media → archive, bootstrap_host, state;  dciab_status → services, state
leaves: dciab_archive, dciab_identity, dciab_state
```

**Is it feasible?** Partly, and it wouldn't pay for this repo today.

- **Couplings that block fine-grained test impact analysis:**
  1. Two import cycles: `validate ↔ backup ↔ storage ↔ cluster`, and
     `services ↔ hypervisors ↔ seed`. Touching any member reaches every test of every member.
  2. `tests/conftest.py` imports `dciab_validate.cli` at collection (line 15), and its
     autouse fixture imports `dciab_seed.cli` for every test (line 25), so every test file
     depends on both cycles.
  3. Shared inputs used almost everywhere: `schema/`, `bundle.lock`, `examples/*.yaml`
     (the `EXAMPLES` fixture parametrizes many tests over all four), and the `dciab` entry
     script, which 14 test files name (string match; most run it as a subprocess).
- **What the PRs actually touch** (measured over the 85 PR CI runs in the sample):
  3 runs (4%) were docs/handoff-only; a job-level filter for the seed job (its packages, their
  cycles, `build/`, `charts/`, `addons/`, `bundle.lock`, `schema/`, `examples/proxmox.yaml`,
  `dciab`) would have skipped it on 5 runs (6%). Lane PRs are broad (6–34 files).
- **Verdict:** The emulated lab already has the one path filter that saves real time (it
  skips 48% of PR pushes). Unit tests are off the critical path (Q3), so test impact analysis
  would save nothing a reviewer waits for. A docs-only skip for the heavy `ci.yml` jobs is
  cheap but rare (≈4% of runs). Listed as low priority.
- **What decoupling would take, if wanted later:** the cycles hang on a few function-level
  imports: `services → hypervisors` is one helper (`dciab_hypervisors.iso.bmc_ip`,
  lib/dciab_services/render.py:202); `services → seed` is the payload lookup
  (`dciab_seed.plan`, lib/dciab_services/cli.py:111, 296); `storage → backup` is
  `class_selector` (lib/dciab_storage/__init__.py:142). Moving those into a small shared
  module (or `dciab_state`) breaks both cycles. Assumed S–M (under a day each, with tests).
  Not worth it for CI time alone; worth it if test selection is ever wanted.

## Q3. Python toolchain

- **Versions:** the unit matrix runs 3.9 and 3.12 (ci.yml:25); every other job uses 3.12;
  the two `rockylinux:10` jobs use the distro's 3.12. The 3.9 leg exists because the
  README (line 72) and the `dciab` launcher (lines 53–54) promise Python 3.9+. ADR 0002 says a
  Linux-laptop bootstrap host must run Rocky Linux 10 (Python 3.12), so the 3.9 promise may
  be stale. The 3.9 leg is the slower unit leg (88 vs 77 s, measured) but off the critical
  path, so dropping it saves one job slot, no wall time. **Decided (Trevor, 2026-11-25):**
  the minimum is EL10's Python, 3.12. #256 raises the launcher and README floor and drops
  the 3.9 leg. 3.13 would speed up pure-Python code a little; the critical paths don't run
  pure-Python hot loops, so the gain is ≈0 s (assumed).
- **`uv`:** not used. Installs are `pip install` of 2–4 small packages: 2–8 s p50 per job
  (measured), 21 `pip install`/`setup-python` lines in ci.yml. `uv` with a cache would cut each
  to ≈1–2 s (assumed from typical uv vs pip ratios), so about **3–6 s per job and ≈2–3 s on
  the critical path** (seed's pip step is 2 s p50). Low value.
- **pytest config:** no `pytest.ini`/`pyproject.toml`; xdist `-n auto` since #240.
  Local measurements (2 vCPU, `-n 2`):
  - collection: **1510 tests in 2.7 s**, not a problem.
  - suite: 109 s wall, 87 s user CPU: about 20% of the wall time is waiting, not computing.
  - `--durations=25`: one test dominates. `test_hypervisors.py::test_a_network_reload_that_drops_the_api_fails_clearly`
    takes **25.05 s**, while the next is 4.1 s. Cause (read from the code): the test
    monkeypatches `post.time.sleep` (tests/test_hypervisors.py:2402), but
    `post.network()` takes `sleep: Callable = time.sleep` as a default argument
    (lib/dciab_hypervisors/post.py:171–172), bound at import, so the patch never applies and
    the retry loop sleeps 5 × 5 s for real. Fix: pass `sleep=` through, or patch the default.
  - next slowest: `test_mgmt_no_volumes` (4.1 s), a `vsphere` ClusterClass checker setup
    (4.0 s), `test_mgmt_pivot` resume tests (1–2.4 s each). No expensive session fixtures.
- **Effect on CI:** with 4 xdist workers on a hosted runner, the 25 s test occupies one
  worker for 40% of the 60 s suite. Fixing it shortens the unit jobs by an estimated 5–15 s
  (extrapolated); they stay off the critical path.

## Q4. Mojo for unit tests

**Dropped.** Python test execution is **0% of both critical paths** (measured): the unit
jobs finish at ≈90 s while the seed job runs to ≈190 s and the reinstall leg to ≈550 s. Even
summed over all `ci.yml` runner time, `pytest` is ≈120 s of ≈1,000 s (≈12%, sum of measured job p50s),
and the suite is partly waiting rather than CPU-bound (Q3). The time that matters is kind,
Kubernetes controllers, podman, nested KVM and Proxmox installs, none of which Python or
Mojo speed changes. No further assessment is warranted.

## Also looked for

- **Redundant or serial work:**
  - Image rebuilds (Q1); payload pulls in three jobs (Q1).
  - The reinstall leg runs two Proxmox installs back to back (≈250 s + ≈220 s, measured):
    the only real serial chain in PR CI.
  - The seed job's firewalld section (33 s, ci.yml:522–551) re-runs seed against the
    existing state. It depends on that state, so splitting it off means a second job that
    rebuilds seed's state up to the registry (≈20 s with a cached payload, extrapolated).
- **Caches:** the CRD cache hits (sampled logs). The seed, `day0-preload` and archive
  payload steps fetch CRDs uncached (5 s, local). Nothing caches the seed payload, apt
  packages (43 s in each emulated leg), pip, or podman layers.
- **Runners:** standard 4-vCPU hosted runners throughout. Larger hosted runners need a paid
  plan (Team or Enterprise) (assumed from GitHub's plan docs). The self-hosted lab runner
  refuses PR jobs by design, so it can't take PR load. Nested Proxmox installs are the one
  workload that might benefit from more cores; unmeasured.
- **Flakiness and retries** (measured, last 100 `ci` runs / 32 emulated runs): 2 + 2 runs
  re-attempted. Job failures: seed 3, services 3, emulated (old `untagged`/`tagged`) 4 + 4,
  six other jobs once each. The logs of those failures weren't read, so which were real
  regressions and which were flakes is unknown. Retry cost when it happens: one full rerun
  of the failed workflow (≈3 min `ci.yml`, ≈10 min emulated).
- **Concurrency:** `cancel-in-progress` for superseded PR runs is in place in `ci.yml` and the
  emulated workflow (#240). Nothing to add.
- **Deprecation, not speed:** every job warns that `actions/checkout@v4` and
  `actions/setup-python@v5` target Node.js 20, now forced onto Node 24.

## Data I couldn't get

| Question | Missing | How to get it |
| --- | --- | --- |
| Cache hit rates over all runs, cache sizes and evictions (10 GB repo limit) | `/repos/{o}/{r}/actions/caches` is blocked from this session's proxy | `gh api repos/TrevorBenson/dciab/actions/caches` from a workstation, or the O1 workflow |
| Whether the account is on Free (20 jobs) or Pro (40) | account billing | Trevor; the plateau at 20 says Free |
| Which job failures were flakes | failed jobs' logs | read the 15 failed jobs' logs, or let O1 classify them going forward |
| The 60 s in the backup image check | which command waits | one timed run |
| Lab runner timings over time | only 17 + 8 runs, `lab.yml` none completed | O1 collects them |
| Post-#240 p95s with confidence | only 10 + 7 runs since #240 merged | re-run this analysis after ≈50 PR pushes |
