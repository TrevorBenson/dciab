---
id: "0011"
result: pass
commit: d6a9398
date: 2026-11-24
---

This task was run differently from the file: Trevor changed the plan during the run (build the image in CI, bootstrap the first VM by hand, see "Workarounds"). Every goal passes; two checklist items, the Trevor-supplied inputs and the GHCR scope, differ from what the file says.

## Summary

The lab runner is up: VM **120** `dciab-lab-runner` on `pve1`, **192.168.186.73/24**, registered to `TrevorBenson/dciab` with labels `self-hosted,linux,x64,dciab-lab`. It ran two dispatched jobs from `main` (the second proves the between-job cleanup), starts each job clean, and refused a dispatch from another branch before any step ran. The image, its qcow2 and the Rocky bootc base are published to GHCR by a workflow on the runner itself, and pinned in `bundle.lock`.

## Steps

1. **Plan: PASS.** `build.sh --print` showed the pins from `bundle.lock`.
2. **Build: PASS**, twice, both on the workstation with `sudo -n` (`node-bootc/build.sh --base-only`, then `lab-runner/build.sh --type qcow2`; the second run after the unit fix below). Image 2.17 GB, `disk.qcow2` 1,026,752,512 bytes (10 GiB virtual, 976 MiB used), `bootc container lint` passed. Per-stage timings were not recorded: the build logs show no timestamps.
3. **Create the VM: PASS.** VMID 120 was free (checked through the API). 8 vCPU `x86-64-v3`, 16384 MiB, virtio-scsi, agent on, SeaBIOS, one virtio NIC on untagged `vmbr0` (no VLAN 150 NIC: none was asked for; tasks 0003/0007 reach the node network through `vmbr0`). The disk went in with `scp` + `qm importdisk` over SSH (`local` has no `import` content type, as `LAB.md` says); resized to 100G; cloud-init drive on `ide2`; `onboot`.
4. **First boot: PASS after a fix.** growroot active, `/sysroot` and `/var` 99G, Enforcing, `bootc status` booted `localhost/dciab-lab-runner:0.1.0`, `dciab-runner` exists and `sudo -n true` works as it, cloud-init `done`. **The first image failed here:** `dciab-lab-runner.service` was `WantedBy=multi-user.target` with `After=cloud-final.service`, an ordering cycle (cloud-final runs after multi-user), so systemd deleted the start job. It is now `WantedBy=cloud-init.target`, with a test (PR #130, `d010311`). I rebuilt and replaced the VM's disk.
5. **Register: PASS.** Config and token were written over SSH on stdin (token never on a command line). The journal shows `runner 21 registered for TrevorBenson/dciab (labels self-hosted,linux,x64,dciab-lab)`; GitHub listed it online.
6. **Smoke job from `main`, twice: PASS.**

   | Run | Result | Duration |
   | --- | --- | --- |
   | 45314921949 | success | 90 s |
   | 45315146165 | success | 52 s |

   The second run's "Nothing left from an earlier job" step passed, so the loop removed the root-owned files, the dummy link `dciab-smoke0`, the container and the home file. I did not run the by-hand SSH checks between the runs; the in-job step covers the same list.
7. **Refused job: PASS.** `gh workflow run lab-runner-smoke.yml --ref ci/lab-runner-image` (the branch carried the workflow). The VM journal: `refused job: ref 'refs/heads/ci/lab-runner-image': only refs/heads/main runs here (run 45315294590, TrevorBenson/dciab/.github/workflows/lab-runner-smoke.yml@refs/heads/ci/lab-runner-image)`. After "Set up job" no step ran; "Always runs (refused jobs must never get here)" was skipped. GitHub reported the job as failed after **604 s** (the runner was killed, so GitHub waited for it); the loop registered a new runner (24) **6 s** after the refusal, and the repository shows no leftover offline runner.
8. **Clean up: PASS.** The VM stays. `images/lab-runner/{build,output}` and the scratch directory are removed; the GitHub token stays in `~/.dciab/lab-runner/github-token`. The admin key moved to `~/.dciab/lab-runner/` (see Problems).

## Problems

- **Image bug, fixed:** the ordering cycle in step 4.
- **My mistake:** a cleanup command deleted the VM's admin SSH key (the file was scratch I had not yet moved). I made a new key, `~/.dciab/lab-runner/admin_key`, and re-seeded it through Proxmox.
- **Hosted Ubuntu cannot build the base.** The SIG recipe's nested `rpm-ostree compose` fails on `ubuntu-latest` with `groupadd: cannot open /etc/gshadow: Permission denied` (userns and AppArmor workarounds changed nothing). So the image is built by a workflow on this runner, not a hosted one (#130). The cause on Ubuntu is not found.
- **`qemu-ga` on the VM does not allow `guest-exec`** (the runner image has the default block list). Not needed today.
- `~/.ssh/id_ed25519` on the workstation appears to be passphrase-protected, so batch SSH with it fails; I used a dedicated key.
- GitHub does not report the PAT's expiry to me; the date is Trevor's to record (see `LAB.md`).

## Artifacts

- **VM 120** `dciab-lab-runner`, `pve1`, `pool0` (100G), 192.168.186.73/24 on untagged `vmbr0`, user `rocky`.
- **GHCR, tag `0.1.0-e6d2bd7c`:** image `ghcr.io/trevorbenson/dciab-lab-runner@sha256:082f4f4baca828c45e764d4c8d140c52609f61d4a2632423602e8c568b5764c0`, disk `ghcr.io/trevorbenson/dciab-lab-runner-disk@sha256:c2dc4152602f037be74fa9496355292e4c86cdd28385cc9d4ab71305a835fe1f`, base `ghcr.io/trevorbenson/dciab-rocky-bootc-base@sha256:56cfba5c1ad3fb20f5a4e3fb532b01139b8f8089cfb86ef1e3cdc07760f2ebbd`, all pinned in `bundle.lock`.
- **Repository settings:** fork pull request approval is `all_external_contributors`; environments `lab-read` and `lab-write` exist (main only; `lab-write` requires a reviewer).
- The image holds no SSH keys, tokens or host keys (scanned in the built image: no `ssh_host_*`, `authorized_keys`, `id_*`, `runner.conf`, token or credential patterns).

## Workarounds

- **First VM bootstrapped by hand** (as `LAB.md` allows for a first runner): a local `sudo -n` build, `scp` + `qm importdisk`. Every later image is built and published by `.github/workflows/lab-runner-image.yml` on the runner.
- **Proxmox access:** the diagnostic SSH key for the one import; the VM itself was created and configured with `qm`. The publisher token was rotated afterwards (it had appeared in a session log); the `lab-write` secret is updated.

## Questions

- **For the planner:** should the runner image's `qemu-ga` allow `guest-exec` (as node images do) so CI can run commands in the VM without SSH?
- **For Trevor:** the expiry date of the token in `~/.dciab/lab-runner/github-token`.
