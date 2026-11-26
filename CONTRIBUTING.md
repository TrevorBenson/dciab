# Contributing to DCiaB

How DCiaB itself is developed: the repository, its tests and CI, and the agents that build it. This page is for the source repository only; the archive doesn't ship it. What DCiaB is and how a site runs it: [README.md](README.md) and [docs/](docs/README.md).

## How DCiaB is built

DCiaB is built by two agents: a planner that writes the code and docs, and a runner that runs tasks against real infrastructure.

| Page | What it covers |
| --- | --- |
| [CLAUDE.md](CLAUDE.md) | The rules for any change: branches, frozen class revisions, `bundle.lock`, product docs versus lab notes, tests in CI |
| [handoff/README.md](handoff/README.md) | How the planner and the runner hand work over: tasks, reports, and each one's rules |
| [build/README.md](build/README.md) | The archive build: image inventory, payload, packaging and signing |
| [docs/RELEASING.md](docs/RELEASING.md) | Cutting a release |
| CI review (2026-11-25): [baseline](handoff/ci-review/baseline.md), [findings](handoff/ci-review/findings.md), [recommendations](handoff/ci-review/recommendations.md) | Where PR feedback time goes, and a ranked plan to cut it and watch it |
