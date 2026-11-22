---
id: "0006"
result: pass
commit: d6f9ea1
date: 2026-11-22
---

Earlier runs: run 1 (`619c9a0`) template 123, growth passed. Run 2 (`366b7bc`) template 124, thin, disk still 10 GiB. Run 3 (`18792a8`) template 127, blocked: SELinux denied the guest agent running `cloud-init`. Run 4 (PR #38) template 129 with the `dciab-qemu-ga` SELinux module, passed. This is run 5, against `main` at `11cebc3`.

## Summary

Template **VMID 130** `rocky10-bootc-iscsi-kube-v1.36.6` passes every check, including the two new ones: `iscsiadm --version` works and `iscsid` is enabled. Same as run 4, the disk is 10 GiB thin with no cloud-init drive, a 25G clone grows its root to 23.5 GiB, and `qm guest exec <clone> -- cloud-init status` returns `status: done` with SELinux Enforcing. Template 129 is deleted, as agreed.

## Steps

1. **Plan: PASS.** Same pins as run 4.
2. **Build: PASS**, `sudo -n`, `EXIT_CODE=0`, **443 s**. Node image **1394 MiB**. `disk.qcow2` 725,286,912 bytes, 10 GiB virtual, 689 MiB used; output is owned by the runner (the `build.sh` chown from #38 works). Lint warnings: **4** in the node image (`nonempty-run-tmp`, `sysusers`, `var-log`, `var-tmpfiles`; `sysusers` is new, from the iSCSI and NFS packages' groups) and 1 in the base (`zstd`).
3. **Publish template: PASS.** VMID 130 was free; `qm create`, `importdisk`, `qm template`; `qm config 130` shows no `ide2`; `pool0/base-130-disk-0` volsize 10G, used 1017M, `refreservation` none.
4. **Growth test: PASS** on clone 131 (25G, cloud-init drive on the clone, 192.168.186.210):
   - `dciab-growroot` active; `/sysroot` and `/var` 24G; `sda4` 23.5G.
   - `cloud-init status --long`: `done` (`degraded done`, the known `user:` notice); `qm guest exec 131 -- cloud-init status`: `status: done` (exit code 2 for degraded).
   - `qemu-ga` allow-list has `guest-exec,guest-exec-status`; `semodule -l` lists `dciab-qemu-ga`; `getenforce` Enforcing; `crio` active; `bootc status` image `localhost/dciab-node:v1.36.6`.
   - **`iscsiadm --version` 6.2.1.11; `systemctl is-enabled iscsid`: enabled.**
   - Reboot: `growroot: /dev/sda partition 4 already fills the disk`; root stayed 24G.
5. **Clean up: PASS.** Test VM 131, key and temp files removed; template **129 deleted**; the output directory is owned by the runner, so I leave it for the next build (it is git-ignored).

## Problems

None blocking. The new `sysusers` lint warning is cosmetic.

## Artifacts

- **Template VMID 130** `rocky10-bootc-iscsi-kube-v1.36.6`, node `pve1`, storage `pool0`, 10 GiB thin (1017 MiB used), no `ide2`, SeaBIOS, `x86-64-v3`, 2 cores, 4096 MiB, agent on. Rocky 10.3, bootc 1.17.1, CRI-O 1.36.9, Kubernetes 1.36.6, `iscsiadm` 6.2.1.11. Built from `main` at `11cebc3`. Kept.
- No test VMs. Build log in `~/.dciab/0006/`.

## Workarounds

- Ran the root build with `sudo -n`; SSH with a throwaway key for in-VM checks.

## Questions

- None.
