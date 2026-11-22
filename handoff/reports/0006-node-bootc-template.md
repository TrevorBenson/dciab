---
id: "0006"
result: blocked
commit: 18792a8
date: 2026-11-22
---

Earlier runs: run 1 (`619c9a0`) built template 123, 20G disk, growth passed. Run 2 (`366b7bc`) built template 124, thin, growth passed, disk still 10 GiB. This is run 3, against `main` at `18792a8` (#34).

## Summary

The template builds, publishes thin **without** a cloud-init drive, and grows its root: **VMID 127** `rocky10-bootc-guestexec-kube-v1.36.6`. Fix 1 (no `ide2`) works. Fix 2 half works: `qemu-ga` now allows `guest-exec`, but **SELinux blocks the agent from running `cloud-init`**, so `qm guest exec <vm> -- cloud-init status`, the exact check CAPMOX does, still fails with `Permission denied`. Result is `blocked` on the task's own check. Task 0003 would hit the same wall.

## Steps

1. **Plan: PASS.** Same pins as before (recipe `7de01402…`, manifest `minimal`, bootc-image-builder `sha256:dab86ee6…0d9f8`).
2. **Build: PASS**, `sudo -n`, `EXIT_CODE=0`, **407 s**. Node image **1359 MiB**. Disk `disk.qcow2` 714,539,008 bytes, **10 GiB virtual, 679 MiB used**; p1 1 MiB BIOS boot, p2 501 MiB EFI, p3 1 GiB boot, **p4 8.5 GiB root**. Lint warnings: 3 in the node image (`nonempty-run-tmp`, `var-log`, `var-tmpfiles`) and 1 in the base (`zstd`), unchanged from run 2.
3. **Publish template: PASS.** VMID 127 was free. `scp` (sha256 `e9f7b2db…f7d0`), `qm create 127 …` as run 2, `qm importdisk`, `qm set 127 --scsi0 pool0:vm-127-disk-0,discard=on,ssd=1 --boot order=scsi0`, `qm template 127`. **`qm config 127` shows no `ide2`.** No resize. Thin: `pool0/base-127-disk-0` volsize 10G, used 999M, `refreservation` none.
4. **Growth test: growth PASS, agent check FAIL.**
   - Full clone VMID 128, disk resized to 25G before first boot, cloud-init drive added to the clone (static `192.168.186.210`, throwaway key). SSH answered shortly after start.
   - `dciab-growroot`: active (exited). `df -h /sysroot /var`: **24G**. `lsblk`: `sda4` **23.5G**. `bootc status`: `localhost/dciab-node:v1.36.6`. `getenforce`: Enforcing. `crio`: active.
   - `cloud-init status --long`: `done` (`degraded done`, the known `user:` notice).
   - `/etc/sysconfig/qemu-ga`: `FILTER_RPC_ARGS="--allow-rpcs=guest-exec,guest-exec-status,guest-sync-delimited,…"`: **PASS**.
   - `qm guest exec 128 -- /bin/true`: exit 0 (guest-exec works). **`qm guest exec 128 -- cloud-init status` (also `/usr/bin/cloud-init status`): `Failed to execute child process "cloud-init" (Permission denied)`: FAIL.**
   - Reboot: `growroot: /dev/sda partition 4 already fills the disk`; root stayed 24G.
5. **Clean up: PARTIAL.** Test VM 128, its disks, the throwaway key and temp files removed. **Template 123 deleted** (Trevor said to). Template 124 kept until the new one passes. `images/node-bootc/output/` is root-owned and still needs `sudo rm -rf` by Trevor.

## Problems

1. **SELinux denies the guest agent running `cloud-init`.** Guest kernel log, three times (once per attempt):
   `avc: denied { execute } for comm="qemu-ga" name="cloud-init" scontext=system_u:system_r:virt_qemu_ga_t:s0 tcontext=system_u:object_r:cloud_init_exec_t:s0 tclass=file permissive=0`.
   qemu-ga runs as `virt_qemu_ga_t`, and Rocky 10's policy does not let that domain execute `cloud_init_exec_t` (`/usr/bin/cloud-init`). `/bin/true` works. Running it through `bash -c` fails differently (`cloud-init: command not found`, because the agent's PATH lacks `/usr/bin`... it ran `/usr/bin/bash`, so this is only its PATH). CAPMOX 0.9.2 runs `cloud-init status` through the agent, so it stays at `WaitingForCloudInit` on this image.
2. Lint warnings and the 10 GiB disk are unchanged from run 2 (not new).

## Suggested fixes (for the planner; none applied)

- Add a small SELinux policy module to the image (allow `virt_qemu_ga_t` execute on `cloud_init_exec_t`, plus read/exec of what `cloud-init status` touches: `/run/cloud-init`, `/var/lib/cloud`), built into the image with `semodule -i` in the Containerfile, and keep SELinux Enforcing. Check with `ausearch`/`audit2allow` on a clone, because other denials may follow after the first.
- Alternatively, run the agent unconfined: `semanage permissive -a virt_qemu_ga_t` (weaker; permissive for that domain only).
- I can test a candidate fix on a clone without changing the repo if you want a quick loop (for example `semodule` on a running clone), but that is outside this task, so I did not.

## Artifacts

- **Template VMID 127** `rocky10-bootc-guestexec-kube-v1.36.6`, node `pve1`, storage `pool0`, 10 GiB thin (999 MiB used), no `ide2`, SeaBIOS, `x86-64-v3`, 2 cores, 4096 MiB, agent on. Rocky 10.3, bootc 1.17.1, CRI-O 1.36.9, Kubernetes 1.36.6. Built from `main` at `18792a8`; recipe `7de0140298a426c89d7ca014f127689a596b5cc0`. Kept (usable for 0003 only once the SELinux denial is fixed).
- Template 124 kept; 123 deleted; no test VMs.
- Left on the workstation: `images/node-bootc/output/` (root-owned, git-ignored), `~/.dciab/0006/` build log.

## Workarounds

- Ran the root build with `sudo -n` (scoped sudoers rule) instead of Trevor.
- Used SSH with a throwaway key for in-VM checks, and `qm guest exec` from `pve1` for the agent check.

## Questions

- **For the planner:** which SELinux fix do you want (policy module vs permissive domain)? Should the README's "Publishing a template" gain this `qm guest exec <clone> -- cloud-init status` check?
- **For Trevor:** delete 124 now (it also cannot work with CAPMOX) or after the new template passes? Please run `sudo rm -rf images/node-bootc/output`.
