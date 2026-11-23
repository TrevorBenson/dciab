# Lab runner (self-hosted GitHub Actions runner)

A Rocky Linux 10 bootc image that runs DCiaB's **infrastructure tests** as GitHub Actions jobs: the ones that need a real hypervisor API, a kind management cluster that reaches cluster VIPs, or the seed registry on port 443. It's test tooling. It's not part of the archive and a site never runs it.

## Two kinds of runner

| Work | Runs on | Why |
| --- | --- | --- |
| Building images and disks (node, appliance, this image), unit tests, renders, kind e2e without infrastructure | GitHub-hosted Ubuntu runners | Free, clean every time, passwordless sudo and KVM. Nothing there needs a lab |
| Publishing templates, building clusters, `dciab seed` + `dciab mgmt`, access-model tests | **This image**, as a VM next to the hypervisor | Needs the hypervisor's API, the node networks and root, and should run on the OS DCiaB's bootstrap host runs |

It uses the same base as the node image and the bootstrap appliance (`nodeImages.bootc`), so seed and mgmt are tested on Rocky Linux 10, with SELinux enforcing and rootful podman, as at a site.

## What's in it

- podman, buildah, skopeo; bootc-image-builder runs from its pinned image (`nodeImages.bootc.bootcImageBuilder`)
- the archive's binaries, from `build/archive.py binaries`: kind, kubectl, helm, clusterctl, oras, age, cosign
- python3 with PyYAML and jsonschema (for `dciab`), qemu-img, xorriso, git, jq
- cloud-init and qemu-guest-agent, so a clone gets its address, SSH key and runner config at first boot
- the GitHub Actions runner (`labRunner.actionsRunner`), read-only under `/usr/lib/dciab-lab-runner/`
- `dciab-lab-runner.service`, which runs `/usr/libexec/dciab-lab-runner/loop`

## How it runs jobs

When the service starts (once per boot), the loop records the VM's IPv4 addresses and dummy links. Then it repeats:

1. **Clean.**
   - Kill the job user's processes.
   - Remove all of root's containers, pods, volumes and networks, and check none are left.
   - Reload firewalld, which drops runtime-only ports.
   - Remove IPv4 addresses and dummy links added since boot. `dciab seed`, for example, adds its DNS service addresses to the host NIC.
   - Empty `/tmp` and `/var/tmp` (except systemd's private directories), `/root/.kube` and `/root/.dciab`.
   - Replace the runner directory and the job user's home with fresh copies.

   If any of these steps fails, the loop doesn't register a runner: it retries the cleanup with backoff. Pulled images stay cached, and so does **`/var/cache/dciab`**, which jobs may use to cache downloads (such as a payload) as long as they check every file against `bundle.lock` before using it. A reboot clears anything else a job changed at runtime, such as routes.

   **Cleanup covers the state lab jobs are known to leave; it doesn't make the VM pristine.** Anything else a job writes as root, under `/var/lib` for example, stays until the VM is re-cloned from its template. That's acceptable because only code merged to the branch runs here. If a job needs a guaranteed-clean VM, re-clone the runner VM first (a later janitor can do that on a schedule).
2. **Register.** Ask GitHub for a **just-in-time runner**: single-use, ephemeral, registered to one repository, with labels `self-hosted,linux,x64` plus `DCIAB_LAB_LABELS`.
3. **Run one job** as `dciab-runner`, which has passwordless sudo (the jobs need root). Afterwards, delete the runner's registration.

The VM's own addresses must be static: an IPv4 address that appears after boot counts as a job's and is removed.

## Security model

Every job has root on this VM and reaches real infrastructure. The repository is public, so several layers decide what runs.

1. **The job-started hook** (`/usr/libexec/dciab-lab-runner/job-started.sh`) refuses any job that isn't a `push`, `schedule` or `workflow_dispatch` on `refs/heads/<DCIAB_LAB_BRANCH>` of `DCIAB_LAB_REPO`, from a workflow file on that branch. That rules out pull requests (including from forks) and dispatches of other branches, whatever their workflow says. It fails closed: a missing variable refuses the job.
   - **A refusal kills the runner.** The runner runs the hook as an ordinary pre-job step, and a failed step still lets later `if: always()` steps run. So the hook kills every process of the job user, itself included, before the job's first step can start. The loop then cleans up and registers a new runner. GitHub shows the job as failed, with the refusal in the "Set up runner" step's log (or as the runner losing contact), and the VM's journal records it (`journalctl -t dciab-lab-runner`).
2. **GitHub settings:**
   - Under Actions → General, require approval for workflows from fork pull requests (all outside contributors).
   - Use **two Environments**, so read-only and scheduled jobs don't wait for a person:
     - `lab-read`, with no reviewers, for smoke tests, `dciab validate --live` and the janitor
     - `lab-write`, with required reviewers, for anything that creates or destroys infrastructure

     Each holds only the hypervisor token for its purpose, as an environment secret.
3. **Workflow rules**, checked by `tests/test_lab_runner.py` for every workflow that runs on a self-hosted runner:
   - no `pull_request`, `pull_request_target` or `workflow_run` triggers
   - the `dciab-lab` concurrency group, with `cancel-in-progress: false`: there's one lab, with one set of fixed addresses, shared with runs by hand
   - actions pinned by commit SHA
4. **The VM** runs nothing else, and is cleaned between jobs.

**Secrets in jobs.** `sudo` drops the environment, so pass a secret explicitly (`sudo --preserve-env=DCIAB_PVE_TOKEN …` or `sudo env VAR="$VAR" …`). Mask derived values with `::add-mask::`, and never `tee` output that contains one into a log or file.

**The registration token** can create runners on the repository. For a repository owned by a user account, that takes **Administration: write** on that one repository (a fine-grained personal access token or a GitHub App). That permission is broad: it can also change repository settings and branch protection, and a leaked token is not bound by the hook.
- The token lives on the VM, readable by root only (`/etc/dciab-lab-runner/token`, mode 0600). A job can read it, because a job has root; that's accepted, since only code merged to the branch runs here.
- Scope it to this repository alone, give it a short expiry, and rotate it.
- **Narrower:** an organization-owned repository can register runners with the organization permission "Self-hosted runners: write" instead, and put the runner in a runner group restricted to this repository (and, where the plan allows, to chosen workflows). The hook stays as defense in depth.

## Build

On a host with the Rocky 10 bootc base (from `images/node-bootc/build.sh`, or `nodeImages.bootc.baseImage` when pinned):

```bash
images/lab-runner/build.sh --print     # the plan and pins
sudo images/lab-runner/build.sh        # localhost/dciab-lab-runner:<version> and output/qcow2/disk.qcow2
```

## Deploy

Import `disk.qcow2` into the hypervisor as a VM disk, as for a node template (the platform table in `images/node-bootc/README.md`). Unlike a node, the runner has no CAPI provider handing it cloud-init data, so its clone **does** get the hypervisor's own cloud-init drive, with its network, an SSH key, and these two files:

```yaml
#cloud-config
write_files:
  - path: /etc/dciab-lab-runner/runner.conf
    permissions: "0600"
    content: |
      DCIAB_LAB_REPO=<owner>/<repo>
      DCIAB_LAB_LABELS=dciab-lab
      DCIAB_LAB_BRANCH=main
  - path: /etc/dciab-lab-runner/token
    permissions: "0600"
    content: <token>
```

Instead of `write_files`, you can copy both files in over SSH after the first boot (`install -m 0600`) and then run `systemctl start dciab-lab-runner`. Either way, without `runner.conf` the service stays off. The root filesystem grows to fill the disk at boot (`dciab-growroot`, shared with the node image). Size it for kind plus the seed registry: 8 vCPU, 16 GiB RAM, a 100 GiB disk. It needs a NIC on every network a test reaches: the hypervisor API, and the node network that cluster VIPs live on.

Lab workflows target it with `runs-on: [self-hosted, dciab-lab]`. Actions that download toolchains for Ubuntu only (`actions/setup-python` and similar) don't work here; use the image's own python3 and binaries.

## Updating

- **The Actions runner:** GitHub requires self-hosted runners to stay recent. A just-in-time runner updates itself when a job starts, if its version is too old, but the update is thrown away with the runner directory. So bump `labRunner.actionsRunner` (version and sha256) when GitHub releases a new runner, then rebuild.
- **The VM:** `bootc upgrade` from a published image, or re-clone it from a new template.
