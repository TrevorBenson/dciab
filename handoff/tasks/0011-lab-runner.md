---
id: "0011"
title: Stand up the self-hosted lab runner VM on Proxmox (Rocky 10 bootc, ephemeral JIT runner)
status: open
runner: workstation
depends: ["0006"]
report: handoff/reports/0011-lab-runner.md
may_change: []
---

## Goal

Build the lab runner image, run it as a VM on `pve1`, register it with GitHub as a self-hosted runner for this repository, and show that it:
- runs a dispatched job from `main`
- starts each job clean
- refuses a job from any other branch

After this, Proxmox tasks can move from this runner (Claude Code) to `workflow_dispatch` workflows on the lab runner, one at a time.

Read first:
- `images/lab-runner/README.md` (what it is, the security model, deploying it)
- `.github/workflows/lab-runner-smoke.yml` (the job you'll dispatch)
- `handoff/LAB.md` (importing a disk, the networks)

**Out of scope:**
- Any workflow that touches Proxmox. The smoke job doesn't.
- Publishing the image to GHCR. A later PR builds it in CI.
- The GitHub Environment for the Proxmox token.

## Needs

- The Rocky 10 bootc base (`localhost/dciab-rocky-bootc-base:minimal`) in root's podman storage, from task 0006. If it's gone, `sudo images/node-bootc/build.sh` rebuilds it.
- Root for the build. Run 5 of task 0006 used `sudo -n`; if that fails, Trevor runs the build.
- `gh` on the workstation, signed in with access to `TrevorBenson/dciab` (to dispatch workflows). Otherwise Trevor dispatches from the Actions tab.
- From Trevor, in one message:
  - **A VMID** for the runner VM on `pve1`.
  - **Networks:** a static IP, prefix and gateway on untagged `vmbr0`, plus DNS. Also say whether to add a second NIC on VLAN 150 now (tasks 0003 and 0007 need kind to reach VIPs there) or later.
  - **His SSH public key** for the VM's admin user.
  - **A GitHub token**, written by Trevor to `~/.dciab/0011/token` (mode 600). It must be a fine-grained personal access token limited to `TrevorBenson/dciab` only, with repository permission **Administration: Read and write**, which lets it create just-in-time runners. Record its expiry date in `LAB.md`, never the token.
  - **Confirmation** that the repository's Actions setting requires approval for workflows from fork pull requests.

## Steps

Keep everything outside the repo in `~/.dciab/0011/` (mode 700). Run commands with `set -o pipefail`.

1. **Plan.** Run `images/lab-runner/build.sh --print`. Expected: the actions runner version and URL from `bundle.lock` `labRunner`, the base above, and the pinned bootc-image-builder.
2. **Build.**
   - Run `sudo images/lab-runner/build.sh 2>&1 | tee /tmp/dciab-lab-runner-build.log`.
   - Expected:
     - the tools step checks every binary
     - the runner's sha256 matches
     - `Runner.Listener --version` prints the pinned version during the image build
     - `bootc container lint` passes
     - it ends with `output/qcow2/disk.qcow2`
   - Record the time of each stage, the image size (`podman image ls`), the disk's virtual and used size, and the lint warnings.
3. **Create the VM.** (Stop and ask first.)
   - Check through the API that the VMID is free.
   - Create the VM on `pve1`:
     - 8 vCPU (`x86-64-v3`), 16384 MiB
     - virtio-scsi, guest agent on, SeaBIOS (as the node template)
     - a virtio NIC on untagged `vmbr0`, plus the VLAN 150 NIC if Trevor asked for it
   - Import `disk.qcow2` into `pool0` the way `LAB.md` describes, attach it as `scsi0`, and resize it to **100G**.
   - Add a cloud-init drive (`--ide2 pool0:cloudinit`) with the IP, gateway, DNS, Trevor's key and a `ciuser` admin user. Unlike node templates, this VM keeps its cloud-init drive.
   - Start it.
4. **First boot checks**, over SSH as the admin user:
   - `systemctl is-active dciab-growroot` is active, and `df -h /sysroot /var` shows about 100G
   - `getenforce` is Enforcing, and `bootc status` shows `localhost/dciab-lab-runner:<version>`
   - `id dciab-runner` exists, and `sudo -u dciab-runner sudo -n true` works
   - `systemctl status dciab-lab-runner` is inactive with an unmet condition (no `runner.conf` yet)
   - `cloud-init status --long` is done
5. **Register.**
   - Copy in the config and the token without putting the token on a command line:
     ```bash
     printf 'DCIAB_LAB_REPO=TrevorBenson/dciab\nDCIAB_LAB_LABELS=dciab-lab\nDCIAB_LAB_BRANCH=main\n' \
       | ssh <admin>@<ip> 'sudo install -m 0600 /dev/stdin /etc/dciab-lab-runner/runner.conf'
     ssh <admin>@<ip> 'sudo install -m 0600 /dev/stdin /etc/dciab-lab-runner/token' < ~/.dciab/0011/token
     ssh <admin>@<ip> 'sudo systemctl start dciab-lab-runner'
     ```
   - Expected: `journalctl -u dciab-lab-runner` shows "runner registered for TrevorBenson/dciab", and the repository's Settings → Actions → Runners lists it as Idle, with labels `self-hosted, linux, x64, dciab-lab`.
6. **Smoke job from `main`, twice.**
   - Run `gh workflow run lab-runner-smoke.yml --ref main`, then watch it with `gh run watch`. Every step must pass.
   - Dispatch it again once the first run is done. It must pass too. Its "Nothing left from an earlier job" step proves the loop cleaned up between jobs.
   - Record each run's ID and duration, and confirm a new runner registered after each job (`journalctl`).
7. **Refused job.**
   - Push your handoff branch (it has the workflow), then run `gh workflow run lab-runner-smoke.yml --ref handoff/0011-lab-runner`.
   - Expected: the job fails at "Set up runner", with the hook's message `refused: ref 'refs/heads/handoff/0011-lab-runner': only refs/heads/main runs here`, and no step after it runs.
   - Record the message, and that the runner registered again afterwards.
8. **Clean up.**
   - **Keep the VM:** it's the lab runner.
   - Remove `images/lab-runner/{build,output}/`, `~/.dciab/0011/token`, and any local images you don't need (keep the bootc base).
   - Afterwards, `git status` must show only your `handoff/` changes.

## Stop and ask

- **Before step 2,** if `sudo -n` doesn't work: Trevor runs the build.
- **Before step 3:** step 3 creates a VM on Proxmox. Show Trevor the VM plan.
- **If any smoke step fails, or step 7's job is not refused:** stop, collect the job log and `journalctl -u dciab-lab-runner -n 200`, and report.
  - A refusal that doesn't happen is a security finding: stop the service (`sudo systemctl stop dciab-lab-runner`) before reporting.
  - If **step 6** fails at "Set up runner" with `refused: ... ''` (an empty variable), the hook didn't get the job's `GITHUB_*` variables. That's a finding for the planner, not something to work around.

## Report

Use the format in `handoff/README.md`. Include:
- the build timings and sizes
- each smoke run's ID and duration, and step 7's refusal message
- under **Artifacts:** the VMID, name, IP and networks of the runner VM

Record in `LAB.md`:
- the runner VM's VMID, IP and NICs
- the token's scope and expiry date (not the token)
- that runner config lives in `/etc/dciab-lab-runner/` on the VM
- how to rotate the token: replace the file, then `systemctl restart dciab-lab-runner`
