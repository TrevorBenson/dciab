---
id: "0006"
result: pass
commit: 619c9a0
date: 2026-11-21
---

## Summary

The rebuilt bootc template (VMID 123) grows its root filesystem. A clone with its disk resized to 40G came up with a 38.5 GiB root partition (`/sysroot` and `/var` show 39G) 12 s after `qm start`, and a reboot left it unchanged with `growroot: /dev/sda partition 4 already fills the disk`. 122 is still there; Trevor decides whether to delete it. Task 0003 can use 123.

## Steps

1. **Plan: PASS.** `build.sh --print` showed the same pins as task 0004. `pytest`: 117 passed.
2. **Build: PASS.** Trevor ran `sudo images/node-bootc/build.sh --type qcow2`. `Build complete!` in the log; qcow2 is 682 MB (679 MiB on disk), 10 GiB virtual.
   - The log has no timestamps, so I have no per-stage times: the only times are the log's end (21:01) and the build starting after Trevor ran it.
   - Lint: the node image has **3 warnings** (down from 4; `zstd` is gone): `nonempty-run-tmp` (`/run/cloud-init`), `var-log` (`/var/log/hawkey.log`) and `var-tmpfiles` (`/var/db/sudo`, `/var/db/sudo/lectured`). The `zstd` warning still shows in the *base* image's lint, which runs earlier and isn't changed by this task.
   - The base compose logs `grub2-pc.posttrans: grub2-probe: error: failed to get canonical path of '/boot'`; the build still passes.
3. **Publish: PASS.** Free VMID 123 confirmed first. Steps as in the 0004 report: `scp` the qcow2 to `pve1`, `qm create`, `qm importdisk 123 … pool0`, attach as `scsi0`, add the cloud-init drive, `qm resize 123 scsi0 20G`, `qm template 123`.
4. **Growth test: PASS.**
   - Full clone to VMID 124, disk resized to **40G** before first boot, static IP 192.168.186.210. SSH came up **12 s** after `qm start`.
   - `systemctl status dciab-growroot`: `active (exited)`, `Result=success`. The journal shows `data blocks changed from 2230272 to 10094843`.
   - `df -h /sysroot /var`: both `/dev/sda4`, 39G, 2.1G used.
   - `lsblk`: `sda` 40G; `sda4` 38.5G xfs at `/sysroot`.
   - `cloud-init status --long`: `done`. It still says `degraded`, but only because of `DEPRECATED: 'user' of type string is deprecated in 22.2 …`. That string comes from Proxmox's cloud-init drive (`ciuser`), not from this recipe. The `cannot resize … composefs` warning from task 0004 is gone.
   - `bootc status` shows `localhost/dciab-node:v1.36.6`; SELinux Enforcing; `crio` active.
   - **Reboot:** SSH back after 15 s. `dciab-growroot` logged `NOCHANGE: partition 4 is size 80758751. it cannot be grown` and `growroot: /dev/sda partition 4 already fills the disk`, then ran `xfs_growfs` as a no-op (size stayed 39G).
   - Test VM 124 deleted.
5. **Clean up: PASS.** `images/node-bootc/output/` removed, temp files on `pve1` and the throwaway key removed. Template 123 kept. VMID 122 still exists; see Questions.

## Problems

1. **`cloud-init` reports `degraded`** even though it finishes `done`, because Proxmox's cloud-init drive writes `user:` as a string, which cloud-init 24.4 flags as deprecated (removal in 27.2). The task expects "not degraded"; this can't be fixed in the recipe, and a ClusterClass using CAPMOX would see the same.
2. **Two templates share the name `rocky10-bootc-kube-v1.36.6`** (122 and 123). Anything that looks a template up by name would find both; by VMID it's unambiguous.

## Artifacts

- **Template `rocky10-bootc-kube-v1.36.6`, VMID 123, node `pve1`, storage `pool0`**: 20 GiB disk, SeaBIOS, `x86-64-v3`, guest agent on, a cloud-init drive. Rocky 10.3, CRI-O 1.36.9, Kubernetes 1.36.6, bootc 1.17.1, with `dciab-growroot`, `zstd`, `xfsprogs` and `cloud-utils-growpart`.
- VMID 122 (the task 0004 template) still exists.

## Workarounds

- Same as task 0004: SSH as root to `pve1` for `qm importdisk`, and a throwaway passphrase-less key on the test VM's cloud-init drive (deleted afterwards).

## Questions

- For Trevor: delete VMID 122 now that 123 passes? I won't delete it unasked.
- For the planner: should the class or recipe set a `users:` list so cloud-init isn't `degraded` on Proxmox? It's harmless today.
