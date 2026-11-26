# Lab CI: the infrastructure tests as workflows

What the runner did by hand in tasks 0003, 0006 and 0007 now runs as GitHub Actions workflows on the lab runner (`images/lab-runner/`). The scripts do the work (`hack/lab/`); the workflows only chain them, so each script can also be run by hand with the same environment.

## Workflows

| Workflow | Kind | What it does | Task |
| --- | --- | --- | --- |
| `lab.yml` | entry, run by hand from the Actions tab | `run`: `template`, `cluster`, `mgmt` or `all`, in that order | all |
| `lab-template.yml` | reusable | build the node image (root), publish it as a template through the API, test a 25G clone (growth, SELinux module, `guest-exec cloud-init status`, iSCSI, reboot) | 0006 |
| `lab-cluster.yml` | reusable | kind management cluster, `lab01` (3 control plane + 1 worker), Cilium, checks, replace a worker and a control plane node, always clean up | 0003 |
| `lab-mgmt.yml` | reusable | payload (cached by `bundle.lock`), the capmox token checked live (`validate --live`), `dciab seed` twice, `dciab mgmt` with each step timed, MGMT manages itself, replace a node on MGMT, re-run changes nothing, no permission failure in CAPMOX's logs, always clean up and check that nothing is left | 0007, 0008 |
| `.github/actions/lab-pve` | composite action | checks the TLS fingerprint, the token and the node; masks the secret | all |

Rules every lab workflow follows (`tests/test_lab_runner.py`, `tests/test_lab_workflows.py`): `runs-on: [self-hosted, dciab-lab]`, no pull-request triggers, actions pinned by SHA, no inputs interpolated into `run:`. The **entry** workflow sets the `dciab-lab` concurrency group; reusable workflows must not repeat it (a called workflow that declares its caller's group is cancelled). Stages are separate reusable workflows because each is useful alone (publish a template without building a cluster) and because a later stage can take an existing template by name.

Templates are found **by name**, never by VMID (VMIDs changed on every template build). The default name is `dciab-node-<commit>`.

## Environments and credentials

| Environment | Reviewers | Used by | Secrets | Variables |
| --- | --- | --- | --- | --- |
| `lab-write` | TrevorBenson (each job waits for approval) | the three reusable workflows | `PVE_TOKEN_ID/SECRET` (publisher), `CAPMOX_TOKEN_ID/SECRET`, `JANITOR_TOKEN_ID/SECRET` (unused yet) | `PVE_URL`, `PVE_TLS_FINGERPRINT` |
| `lab-read` | none | nothing yet (smoke checks, `validate --live`) | `PVE_TOKEN_ID/SECRET` (audit) | the same two |

Both deploy from `main` only. The tokens and their privileges are in `handoff/LAB.md`. Nothing here uses SSH to a Proxmox host.

## Prerequisites on the lab (one time)

- **The lab runner VM exists** (task 0011). Until then these workflows can't run.
- **`local` storage allows the content type `import`**, so the publish can upload a disk image through the API and create the VM with `import-from`. As a Proxmox admin: `pvesm set local --content backup,vztmpl,iso,snippets,import` (keep the current list and add `import`). `hack/lab/template.sh publish` checks this and says so.
- The addresses in `handoff/lab/network.yaml` are free: lab jobs and hand runs share them, so don't run both at once.

## Not done yet

- A **janitor** workflow: delete leftover CI VMs and `user-data-*.iso` files after a cancelled job. It needs its own Environment without reviewers so it can run on a schedule.
- A scheduled run, and per-job approval fatigue: `lab-write` asks for approval on each of the three stages of an `all` run.
- The `untaint-control-plane` input on `lab.yml` and `lab-mgmt.yml` is a workaround for issue #112 (a worker-less MGMT can't schedule cert-manager). It is off by default so CI shows the failure.
- The `mgmt` flow has not run yet. The move fix (#126) and the worker-less control plane fix (#112) are in; its first run is task 0007's Retry 2 (#113).
