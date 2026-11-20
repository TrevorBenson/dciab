# Handoff: tasks between agents

DCiaB is developed by two Claude agents with Trevor in the loop. This directory is how they hand work to each other: tasks go in as files, and results come back as committed reports. Every handoff is visible in Git.

| Role | Who | Can | Can't |
| --- | --- | --- | --- |
| **Planner** | Claude in a cloud session (claude.ai) | Write code, charts, docs and tasks; open, review and merge PRs; run CI | Reach the lab: no Proxmox, vSphere, BMCs, or Trevor's machines |
| **Runner** | Claude Code on Trevor's workstation | Run tasks against real infrastructure: the Proxmox API, building templates, kind, VMs | Change DCiaB itself (code, charts, classes, docs); that comes back to the planner as a finding |
| **Trevor** | The human | Approve anything that creates or destroys infrastructure; relay between agents; merge | — |

## Layout

```text
handoff/
  README.md                 this file: the protocol
  tasks/NNNN-<slug>.md      one task per file, written by the planner
  reports/NNNN-<slug>.md    one report per task, written by the runner
  reports/NNNN-<slug>/      optional small attachments (trimmed logs, command output)
```

Task numbers are four digits and never reused. The report uses the same `NNNN-<slug>` name as its task.

## A task file

```markdown
---
id: "0002"
title: Build the Rocky 10 node template on Proxmox
status: open            # open | in-progress | done | blocked
runner: workstation
depends: ["0001"]       # tasks whose reports must be done first
report: handoff/reports/0002-node-template-proxmox.md
may_change: [bundle.lock]   # files outside handoff/ the runner may edit, if any
---

## Goal       what this proves, in a sentence or two
## Needs      tools, access, and inputs to ask Trevor for
## Steps      numbered; each says what passing looks like
## Stop and ask   points where the runner must get Trevor's go-ahead
## Report     what the report must contain
```

## The loop

1. **Planner writes a task.** It adds `tasks/NNNN-*.md` with `status: open` in a PR, alongside any code the task needs. Trevor or the planner merges it.
2. **Trevor starts the runner.** He tells it: *"Run handoff task NNNN"*, or *"run the open handoff tasks"*.
3. **Runner claims it.** It pulls `main`, creates branch `handoff/NNNN-<slug>`, and sets `status: in-progress`.
4. **Runner works through the steps.** At each *Stop and ask* point it waits for Trevor. If a step fails and the task doesn't say what to do, it stops, records what happened, and finishes the report with `blocked`.
5. **Runner reports.** It writes `reports/NNNN-*.md` and sets the task to `done` (every step ran, whatever the results) or `blocked` (it could not continue). It commits only:
   - `handoff/` files
   - the files in `may_change`

   Then it pushes the branch, opens a PR titled `handoff NNNN: <title>`, and gives Trevor the PR link.
6. **Planner picks it up.** Trevor tells the planner the PR number, or the planner lists open `handoff/*` PRs. The planner reads the report, merges the PR, and follows up with fix PRs and new tasks.

The status in the task file on `main` is the state of record. Where a task stands is `git grep '^status:' handoff/tasks`.

## Rules for the runner

- **Secrets never enter Git.** No tokens, passwords, private keys or kubeconfigs, in reports or anywhere else. Credentials live outside the repo (for example `~/.dciab/`, mode 600). Write `<redacted>` where a value would appear.
- **No large files.** Never commit templates, disk images, ISOs or full logs. Attachments are trimmed text, under 200 KB per task. Name artifacts by identifier instead, such as "template VMID 9010 on pve01".
- **Don't fix DCiaB in the task branch.** If a chart, class, script or doc is wrong, describe it in the report: the exact error, where it happened, what you suspect. The planner fixes it in its own PR, so the fix goes through CI and review. A local workaround is fine only to get further, and the report must say exactly what it was.
- **Ask before creating or destroying infrastructure,** unless the task says that step is pre-approved.
- **Clean up what the task created,** unless the task says to keep it (a built template, for example). The report lists anything left behind.
- Record the commit you ran against (`git rev-parse --short HEAD`), tool versions, and how long each step took.

## A report file

```markdown
---
id: "0002"
result: pass            # pass | fail | partial | blocked
commit: abc1234         # the commit the runner tested
date: 2026-11-20
---

## Summary          two or three lines: what ran, what passed, what didn't
## Steps            one line per task step: PASS/FAIL/SKIPPED, duration, key output
## Problems         one item each: what happened, where, the exact error, the suspected cause
## Artifacts        identifiers of anything created and kept (VMIDs, names); never the artifacts
## Workarounds      anything done that the task didn't say to do
## Questions        for the planner or Trevor
```

## Rules for the planner

- Tasks only cover what exists on `main` when they're merged. Anything missing is named in the task as out of scope, not left for the runner to work out.
- Keep tasks short: steps that can be checked, and explicit stop points. Put background in docs and link it.
- A task that needs new code ships in the same PR as that code, or after it.
- Read the report before writing the next task. Don't assume a result.
