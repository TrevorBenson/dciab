# CI recommendations (2026-11-25 Pacific; data to 2026-11-26 02:00 UTC)

Ranked plan from [findings.md](findings.md), against the critical paths in
[baseline.md](baseline.md). Nothing here is implemented.

## How to read the table

- **Critical paths (post-#240 p50, measured):** **A**, emulated lab off (48% of PR pushes):
  **193 s**. This is the `ci.yml` run alone (all events, n=9), because only 2 post-#240 PR
  pushes ran without the emulated lab, and they agree (191 s). **B**, emulated lab on
  (52%): **597 s**, p95 922 s (n=5).
- **Savings** are seconds off a path's p50 unless marked *p95*, then the share of that path.
  **Blended** = 0.48 × ΔA + 0.52 × ΔB, the average saving per PR push.
- **Basis:** M = measured, X = extrapolated from measurements, A = assumed.
- **Effort:** S < 1 day, M 1–3 days, L > 3 days. **Score** = blended ÷ effort points
  (S = 1, M = 3, L = 8). Changes that target the tail are scored on their p95 saving and
  marked †.
- **Bucket** refers to the breakdown tables in baseline.md §3.

## Quick wins (< 1 day each)

Every S-effort item, in score order. R7, R6 and R12 save nothing on a critical path but
are cheap and free runner time (which is what the queue competes for) or fix hygiene.

| # | Change | Bucket | Savings | Basis | Effort | Risk | Depends on |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R4 | **Cache the seed payload**, saved from `main` only like the ISO, keyed on what it contains rather than on the files that compute it. That key is the hash of the step's computed image inventory (`build/inventory.py`'s output, ≈5 s with cached CRDs) **plus** `bundle.lock`, `hack/fetch_crds.py`, `build/archive.py` and the `--only` list. The inventory covers the images, including changes through `hack/render_components.py`. `bundle.lock` and `fetch_crds.py` cover the binaries, charts and provider manifests, which aren't in the inventory. After a restore, check every inventory image against its digest, every binary against `binarySHA256`, and every chart against its pinned digest; the provider manifests are covered by the key. The `sums` step only *writes* SHA256SUMS over what's on disk, so it checks nothing. Also give `seed-e2e`, `day0-preload` and the archive steps the existing CRD cache. | image pull | A: −25 to −30 s (−14%) | X (42 s Payload step measured; a 2.1 GB restore assumed ≈15 s, as the 1.7 GB ISO restores in 14 s; CRDs 5 s, local) | S | Low. Everything in it is pinned. Uses ≈2.1 GB of the 10 GB cache | — |
| R3a | **Pull, don't rebuild, unchanged images:** in the emulated legs (both images) and the `services` job (chrony only), pull `ghcr.io/…/dciab-day0-chrony` / `dciab-pve-assistant` for the current recipe id when it exists, and build only when the recipe changed. Today the id is `sha256(base + Containerfile)`; extend it to cover `build.sh` too. Packages that `dnf` installs can still change under an unchanged id. The legs would then test the published image, which is what `bundle.lock` pins and sites run. The `day0-chrony` and `pve-assistant` jobs keep building fresh, so a package change still gets built and checked on every PR. | image build | B: −15 to −20 s (−3%); ≈90 s less runner time per emulated push | X (29 s measured for both builds in a leg; pulls of these small images assumed ≈5–10 s) | S | Low–medium. A leg tests the published image, not a fresh `dnf install` of the same recipe; that's closer to what a site runs | pve-assistant needs a `--recipe-id` like chrony's |
| R11 | **Skip heavy `ci.yml` jobs on docs/handoff-only PRs** (`dorny/paths-filter` or job `if:`). | all | ≈4% of PR pushes skip ≈190 s: −8 s average on A | M (3 of 85 PR runs were docs/handoff-only) | S | Low: nothing is a required check | — |
| R1† | **Fewer, larger small jobs:** fold six small `ci.yml` jobs into three. `lint`: shellcheck + frozen classes. `checks`: image digests + Redfish mockups. The two `rockylinux:10` container jobs become one. (The chrony job stays alone: it holds `packages: write`.) 17 jobs → 14; 16 → 13 after #256. | queue | B p95: ≈ −5 s; p50 ≈ 0 | A (each folded job saves ≈5–10 s of setup and one slot; the measured queueing comes from overlapping PRs, which this barely changes) | S | Low. A failure names a combined job; renames break nothing because no check is required | — |
| R10 | **`uv` with its cache** for the `pip install` steps. | dependency install | A −2 s, B −1 s | A (pip steps 2–8 s measured; uv ratio assumed) | S | Low | — |
| R3b | **Cache the emulated legs' apt downloads** (`/var/cache/apt/archives`). | dependency install | B: −1 to −2 s | M (≈50 MB downloaded in ≈2 s, job 139635528680's log; the rest of the 43 s step is unpacking) | S | Low | — |
| R7 | **Find the 60 s silence in the backup image check** (`CLUSTERCTL_DISABLE_VERSIONCHECK=true`, then time each command) and remove it. | tests (off path) | 0 s on paths; the job 86 → ≈26 s, a runner free a minute earlier on every push | M (60.2 s gap in the job log) | S | Low | — |
| R6 | **Fix the 25 s real sleep** in `test_a_network_reload_that_drops_the_api_fails_clearly`: pass `sleep=` to `post.network()` or patch the default argument. | tests (off path) | 0 s on paths; unit job −5 to −15 s | X (25.05 s local; 4 workers in CI) | S (≈1 h) | None | — |
| R12 | **Bump the actions that target Node.js 20** to their Node 24 majors: `checkout`, `setup-python`, `cache`, `cache/restore`, `cache/save`, `upload-artifact`. | — | 0 s | — | S | Low | — |

## Ranked by score

| Rank | # | Change | Bucket | ΔA | ΔB | Blended | Effort | Score | Risk | Depends on |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |
| 1 | R4 | Cache the seed payload (+ CRD cache everywhere) | image pull | −28 s (−14%) | 0 | −13 s | S | 13 | Low | — |
| 2 | R3a | Pull unchanged chrony / pve-assistant images | image build | 0 | −18 s (−3%) | −9 s | S | 9 | Low–med | recipe id for pve-assistant |
| 3 | R2 | **Reinstall leg from a cached pre-installed disk** (deferred by #240): restore a qcow2 of a host that `main` installed, plus its install record, then run only the reinstall scenarios. | tests (e2e) | 0 | −130 s (−22%) | −68 s | L | 8.5 | Medium (below) | cache budget shared with R4 |
| 4 | R5 | Move seed's firewalld section (33 s) into its own parallel job. Its check execs into the kind node, so the new job runs seed through `kind` from the cached payload: ≈10 s setup + ≈15 s restore + ≈60 s seed (dns, registry, kind) + 33 s ≈ 120 s (X), under the ≈157 s the seed job keeps | tests (integration) | −33 s (−17%) | 0 | −16 s | M | 5.3 | Low–med: duplicated setup | R4 (else the new job is slower than what it removes) |
| 5 | R11 | Skip heavy jobs on docs/handoff-only PRs | all | −8 s avg | 0 | −4 s | S | 4 | Low | — |
| 6 | R1† | Fold six small jobs into three | queue | ≈0 | −5 s *p95* (A) | −3 s *p95* | S | 3† | Low | — |
| 7 | R10 | `uv` with its cache | dependency install | −2 s | −1 s | −1.5 s | S | 1.5 | Low | — |
| 8 | R3b | Cache apt downloads in emulated legs | dependency install | 0 | −1.5 s | −0.8 s | S | 0.8 | Low | — |
| 9 | R7 | Remove the backup check's 60 s wait | tests (off path) | 0 | 0 | 0 (frees a runner 60 s sooner) | S | — | Low | — |
| 10 | R6 | Fix the 25 s sleep in one unit test | tests (off path) | 0 | 0 | 0 | S | — | None | — |
| — | R9 | **Decided: Python 3.12 (EL10's) is the minimum.** #256 (merged) raised the launcher and README floor and dropped the 3.9 leg; an emulated-lab push is now 19 jobs | — | 0 | 0 | 0 (−1 job) | S | — | Low | — |
| — | R12 | Bump the Node 20 actions | — | 0 | 0 | 0 | S | — | Low | — |

**The queue, honestly:** the measured queueing (queue p95 65–81 s post-#240) happens only
when runs from different PRs overlap. Two overlapping emulated-lab pushes are ≈38 jobs
against a cap of 20, whatever R1 does. What helps is less runner time held per push
(R7 frees 60 s, R3a ≈90 s, R4 ≈25 s) and fewer lane PRs pushing at the same moment. Neither
is measurable until it's done; O2 tracks queue p95 so the effect shows.

**R2's risks, spelled out:**
- **The key can't include the hypervisor code.** The emulated lab runs only when that code
  changes, so such a key would always miss. The reinstall leg would therefore reinstall over a
  host that `main`'s code installed. First-install is still proven on every run by the install
  leg, but a PR that changes the install record's format needs a key bump (key on the
  record's schema version).
- **Size.** An installed Proxmox VE disk, compressed: ≈2–4 GB (assumed). With the ISO (1.7 GB)
  and the payload (≈2.1 GB) that's ≈6–8 GB of the 10 GB repo cache, so saves must be
  main-only.
- **State.** The reinstall scenario retires the previous install's sealed API token, and it needs the age identity that sealed it. The cache must therefore carry the whole site state the install leaves (install record, sealed token and the age identities) together with the disk, or the leg must recreate equivalent state before the scenario.
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
| Quick wins: R4 + R3a + R1 + R3b (X/A) | ≈165 s (−15%) | ≈577 s (−3%) | ≈898 s (−3%) |
| + R2 and R5 (X/A) | ≈132 s (−32%) | ≈447 s (−25%) | ≈768 s (−17%) |

The p95 column rests on 5 post-#240 runs; treat it as a direction, not a forecast. It
applies the p50 savings of R3a, R3b and R2 to the tail too, plus R1's assumed p95 saving (A). R4 and R5 touch path A only. Each removes fixed work
that every run does, so slow runs lose it as well, but nothing here measures that. It
excludes any queue relief from overlapping PRs needing fewer runner-minutes (see "The
queue, honestly").

Below ≈130 s, path A is set by the ClusterClass e2e (113 s, measured: kind create 42 s +
`run.sh` 51 s). Below ≈420 s, path B is set by the install and tagged legs, each one full
Proxmox VE install plus its scenarios. Going further would mean proving less per run or
paying for faster runners.

## Observability

Proportionate to a one-repo project on hosted runners, using what's already there (the
Actions API, artifacts, issues). No new service.

| # | Change | What it gives | Effort |
| --- | --- | --- | --- |
| O1 | **`ci-metrics.yml`:** a scheduled (daily) and `workflow_dispatch` workflow on a hosted runner, `permissions: {actions: read, contents: write, issues: write}`. It pulls the last N runs of `ci` and the emulated lab through the REST API (the same queries as this review), computes per-job and per-step p50/p95, queue time and push → all-green per path, and appends rows to a CSV on an orphan `ci-metrics` branch, queryable with DuckDB or pandas, and graphable later in the platform's Grafana if wanted. It writes the week's table to the job summary. Runs are serialized with `concurrency: {group: ci-metrics, cancel-in-progress: false}`. Rows are keyed by run id, attempt, job id and step number, and written as upserts, so a run seen in two windows counts once. A push that loses a race re-fetches the branch tip and retries. | Durations, queue times and pass/fail kept over time; this review becomes repeatable | M |
| O2 | **p95 regression alert in O1:** compare the last 7 days' p95 per job and per path with the previous 28 days'. Above +20% **and** +30 s, open or update one `tooling` issue (looked up by a fixed title per job, so overlapping checks don't duplicate it) naming the job, the step that grew, and the first run where it moved. Also flag queue p95 > 30 s (the 20-job cap) and any job whose re-attempt rate exceeds 5%. | Regressions caught without anyone looking | S (on top of O1) |
| O3 | **Test durations:** the unit job adds `--junitxml=junit.xml --durations=25` and uploads `junit.xml` with `if: always()` and `if-no-files-found: ignore`, so failed runs keep their durations; O1 reads the artifacts, tracks per-test time, and lists tests over 5 s. The emulated legs' `report.json` already records seconds per scenario; O1 reads those artifacts too. | Per-test and per-scenario trends (the 25 s test would have shown up on day one) | S |
| O4 | **Cache hits:** each `actions/cache` step gets an `id`, and a following step writes `cache=<name> hit=<cache-hit>` to the job summary; O1 counts them. O1 also reads `/actions/caches` for sizes and evictions against the 10 GB limit (R2 and R4 make that budget matter). | Cache hit rate and size, watched | S |

OpenTelemetry CI traces (for example an action that exports each run as a trace) would need
a collector and backend that this project doesn't run for CI. Revisit if one appears; O1's
CSV holds the same per-job and per-step spans.

**O1 needs a token decision:** `GITHUB_TOKEN` with `actions: read` covers runs, jobs and
artifacts. Pushing to an orphan branch needs `contents: write`, which the repo's workflow
conventions (`contents: read`) don't grant elsewhere. The alternative is keeping the CSV
as a 90-day artifact and rebuilding history from the API, which only keeps 400 days of
runs.
