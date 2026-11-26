# CI recommendations (2026-11-25)

Ranked plan from [findings.md](findings.md), against the critical paths in
[baseline.md](baseline.md). Nothing here is implemented.

## How to read the table

- **Critical paths (post-#240 p50, measured):** **A**, emulated lab off (48% of PR pushes):
  **193 s**. **B**, emulated lab on (52%): **597 s**, p95 922 s.
- **Savings** are seconds off a path's p50 unless marked *p95*, then the share of that path.
  **Blended** = 0.48 × ΔA + 0.52 × ΔB, the average saving per PR push.
- **Basis:** M = measured, X = extrapolated from measurements, A = assumed.
- **Effort:** S < 1 day, M 1–3 days, L > 3 days. **Score** = blended ÷ effort points
  (S = 1, M = 3, L = 8). Changes that target the tail are scored on their p95 saving and
  marked †.
- **Bucket** refers to the breakdown tables in baseline.md §3.

## Quick wins (< 1 day each)

| # | Change | Bucket | Savings | Basis | Effort | Risk | Depends on |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | **Fit a PR push under 20 concurrent jobs:** fold six small `ci.yml` jobs into three. `lint`: shellcheck + frozen classes. `checks`: image digests + Redfish mockups. The two `rockylinux:10` container jobs become one. (The chrony job stays alone: it holds `packages: write`.) 18 jobs → 15; 18 with the emulated lab (17 → 14, and 17, once #256 drops the 3.9 leg). | queue | B p95: −40 to −50 s (−5%); p50 ≈ 0 | M (queue p95 45–57 s on emulated legs post-#240, ≤10 s before) | S | Low. A failure names a combined job; no check is required by the ruleset, so renames break nothing | — |
| R4 | **Cache the seed payload**, saved from `main` only like the ISO, keyed on `hash(bundle.lock, build/archive.py, build/inventory.py, hack/fetch_crds.py)`. Also give `seed-e2e`, `day0-preload` and the archive steps the existing CRD cache. Keep the `sums` step, so a restored payload is still checked. | image pull | A: −25 to −30 s (−14%) | X (42 s Payload step measured; a 2.1 GB restore assumed ≈15 s, as the 1.7 GB ISO restores in 14 s; CRDs 5 s, local) | S | Low. Everything in it is pinned by digest or sha256. Uses ≈2.1 GB of the 10 GB cache | — |
| R3a | **Pull, don't rebuild, unchanged images:** in the emulated legs and the `services` job, pull `ghcr.io/…/dciab-day0-chrony` / `dciab-pve-assistant` for the current recipe id when it exists, and build only when the recipe changed. The `day0-chrony` and `pve-assistant` jobs keep building, since they're what proves the recipe. | image build | B: −15 to −20 s (−3%) | X (29 s measured for both builds; pulls of these small images assumed ≈5–10 s) | S | Low–medium. A leg tests the published image, not a fresh `dnf install` of the same recipe; that's closer to what a site runs | pve-assistant needs a `--recipe-id` like chrony's |
| R7 | **Find the 60 s silence in the backup image check** (`CLUSTERCTL_DISABLE_VERSIONCHECK=true`, then time each command) and remove it. | tests (off path) | 0 s on paths; the job 86 → ≈26 s, freeing a runner slot a minute earlier | M (60.2 s gap in the job log) | S | Low | — |
| R6 | **Fix the 25 s real sleep** in `test_a_network_reload_that_drops_the_api_fails_clearly`: pass `sleep=` to `post.network()` or patch the default argument. | tests (off path) | 0 s on paths; unit jobs −5 to −15 s | X (25.05 s local; 4 workers in CI) | S (≈1 h) | None | — |
| R3b | **Cache the emulated legs' apt downloads** (`/var/cache/apt/archives`, keyed on the package list + runner image). | dependency install | B: −5 to −10 s (−1–2%) | A (43 s step measured; the download share isn't) | S | Low | — |

## Ranked by score

| Rank | # | Change | Bucket | ΔA | ΔB | Blended | Effort | Score | Risk | Depends on |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |
| 1 | R1† | Fit under 20 concurrent jobs | queue | ≈0 | −45 s *p95* | −23 s *p95* | S | 23† | Low | — |
| 2 | R4 | Cache the seed payload (+ CRD cache everywhere) | image pull | −28 s (−14%) | 0 | −13 s | S | 13 | Low | — |
| 3 | R3a | Pull unchanged chrony / pve-assistant images | image build | 0 | −18 s (−3%) | −9 s | S | 9 | Low–med | recipe id for pve-assistant |
| 4 | R2 | **Reinstall leg from a cached pre-installed disk** (deferred by #240): restore a qcow2 of a host that `main` installed, plus its install record, then run only the reinstall scenarios. | tests (e2e) | 0 | −130 s (−22%) | −68 s | L | 8.5 | Medium (below) | cache budget shared with R4 |
| 5 | R11 | Skip heavy `ci.yml` jobs on docs/handoff-only PRs (`dorny/paths-filter` or job `if:`) | all | −8 s avg | 0 | −4 s | S | 4 | Low: nothing is a required check | — |
| 6 | R3b | Cache apt downloads in emulated legs | dependency install | 0 | −8 s | −4 s | S | 4 | Low | — |
| 7 | R5 | Move seed's firewalld section (33 s) into its own parallel job that seeds up to the registry from the cached payload | tests (integration) | −33 s (−17%) | 0 | −16 s | M | 5.3 | Low–med: duplicated setup | R4 (else the new job is slower than what it removes); R1 (it adds a job) |
| 8 | R10 | `uv` with its cache for the `pip install` steps | dependency install | −2 s | −1 s | −2 s | S | 2 | Low | — |
| 9 | R7 | Remove the backup check's 60 s wait | tests (off path) | 0 | 0 | 0 (frees a slot) | S | — | Low | — |
| 10 | R6 | Fix the 25 s sleep in one unit test | tests (off path) | 0 | 0 | 0 | S | — | None | — |
| — | R9 | **Decided: Python 3.12 (EL10's) is the minimum.** #256 raises the launcher and README floor and drops the 3.9 leg | — | 0 | 0 | 0 (−1 job) | S | — | Low | — |
| — | R12 | Bump `actions/checkout` / `setup-python` to their Node 24 majors | — | 0 | 0 | 0 | S | — | Low | — |

**R2's risks, spelled out:**
- **The key can't include the hypervisor code.** The emulated lab runs only when that code
  changes, so such a key would always miss. The reinstall leg would therefore reinstall over a
  host that `main`'s code installed. First-install is still proven on every run by the install
  leg, but a PR that changes the install record's format needs a key bump (key on the
  record's schema version).
- **Size.** An installed Proxmox VE disk, compressed: ≈2–4 GB (assumed). With the ISO (1.7 GB)
  and the payload (≈2.1 GB) that's ≈6–8 GB of the 10 GB repo cache, so saves must be
  main-only.
- **Restore time.** Assumed 30–45 s, which leaves the leg (≈300 s + restore) under the other
  two (412–417 s). So the saving is capped by those legs at ≈130 s, not the full ≈250 s install.

**Not recommended**

- **Test impact analysis / decoupling the import cycles:** unit tests are off both critical
  paths. Only 4–6% of PR runs could skip anything (findings Q2).
- **Mojo:** Python test execution is 0% of both critical paths (findings Q4).
- **Larger hosted runners:** not available on the Free plan (assumed from GitHub's plan docs);
  unmeasured benefit even for nested installs. The self-hosted lab runner refuses PR jobs
  by design.
- **Python 3.13 for speed:** ≈0 s on the paths (assumed; no pure-Python hot loop there).

## What the top changes add up to

| | Path A p50 | Path B p50 | Path B p95 |
| --- | ---: | ---: | ---: |
| Today (post-#240, M) | 193 s | 597 s | 922 s |
| Quick wins: R1 + R4 + R3a (+ R3b) (X) | ≈165 s (−15%) | ≈575 s (−4%) | ≈850 s (−8%) |
| + R2 and R5 (X/A) | ≈130 s (−33%) | ≈445 s (−25%) | ≈720 s (−22%) |

The p95 column rests on 5 post-#240 runs; treat it as a direction, not a forecast. R1 leaves
headroom for one PR push, not two at once (or a PR push plus a post-merge `main` run): the
cap returns then. O2 below watches for it.

Below ≈130 s, path A is set by the ClusterClass e2e (113 s, measured: kind create 42 s +
`run.sh` 51 s). Below ≈420 s, path B is set by the install and tagged legs, each one full
Proxmox VE install plus its scenarios. Going further would mean proving less per run or
paying for faster runners.

## Observability

Proportionate to a one-repo project on hosted runners, using what's already there (the
Actions API, artifacts, issues). No new service.

| # | Change | What it gives | Effort |
| --- | --- | --- | --- |
| O1 | **`ci-metrics.yml`:** a scheduled (daily) and `workflow_dispatch` workflow on a hosted runner, `permissions: {actions: read, contents: write, issues: write}`. It pulls the last N runs of `ci` and the emulated lab through the REST API (the same queries as this review), computes per-job and per-step p50/p95, queue time and push → all-green per path, and appends rows to a CSV on an orphan `ci-metrics` branch, queryable with DuckDB or pandas, and graphable later in the platform's Grafana if wanted. It writes the week's table to the job summary. | Durations, queue times and pass/fail kept over time; this review becomes repeatable | M |
| O2 | **p95 regression alert in O1:** compare the last 7 days' p95 per job and per path with the previous 28 days'. Above +20% **and** +30 s, open or update one `tooling` issue naming the job, the step that grew, and the first run where it moved. Also flag queue p95 > 30 s (the 20-job cap) and any job whose re-attempt rate exceeds 5%. | Regressions caught without anyone looking | S (on top of O1) |
| O3 | **Test durations:** the unit jobs add `--junitxml=junit.xml --durations=25` and upload `junit.xml`; O1 reads the artifacts, tracks per-test time, and lists tests over 5 s. The emulated legs' `report.json` already records seconds per scenario; O1 reads those artifacts too. | Per-test and per-scenario trends (the 25 s test would have shown up on day one) | S |
| O4 | **Cache hits:** each `actions/cache` step gets an `id`, and a following step writes `cache=<name> hit=<cache-hit>` to the job summary; O1 counts them. O1 also reads `/actions/caches` for sizes and evictions against the 10 GB limit (R2 and R4 make that budget matter). | Cache hit rate and size, watched | S |

OpenTelemetry CI traces (for example an action that exports each run as a trace) would need
a collector and backend that this project doesn't run for CI. Revisit if one appears; O1's
CSV holds the same per-job and per-step spans.

**O1 needs a token decision:** `GITHUB_TOKEN` with `actions: read` covers runs, jobs and
artifacts. Pushing to an orphan branch needs `contents: write`, which the repo's workflow
conventions (`contents: read`) don't grant elsewhere. The alternative is keeping the CSV
as a 90-day artifact and rebuilding history from the API, which only keeps 400 days of
runs.
