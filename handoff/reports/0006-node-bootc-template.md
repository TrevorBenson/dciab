---
id: "0006"
result: pass
commit: 366b7bc
date: 2026-11-21
---

First run: built template 123 at commit `619c9a0` before the small, thin template change (#22), so it has the old 10 GiB root resized to 20G. It passed growth with a 40G clone (38.5 GiB root). This is the rerun against current `main`.

## Summary

The bootc template built from `main` at `366b7bc` (template VMID **124**, `rocky10-bootc-thin-kube-v1.36.6`) grows its root filesystem. A clone resized to 25G, the size task 0003's nodes use, came up with a 23.5 GiB root partition (`/sysroot` and `/var` show 24G) 12 s after `qm start`, and a reboot left it unchanged. **But the template is not small: its disk is still 10 GiB with an 8.5 GiB root, so `minsize = "2 GiB"` in `images/node-bootc/config.toml` had no effect** (Problems 1). The zvol stays thin, so it costs about 1 GiB on `pool0`. Task 0003 can use 124.

## Steps

1. **Plan: PASS.** `build.sh --print` shows the same pins as 0004 (recipe `7de01402…`, manifest `minimal`, the same bootc-image-builder digest).
2. **Build: PASS**, run by me with `sudo -n` (Trevor added a scoped sudoers rule), `EXIT_CODE=0`, **404 s** total:
   - Base: 226 s (the recipe's `grub2-probe` message about `/boot` is printed by the compose and is harmless).
   - Node image: 93 s (to 319 s). **1359 MiB**, against the guard limit of 1843 MiB for the configured 2 GiB root.
   - Disk (qcow2 only, 85 s): `disk.qcow2` is 714,997,760 bytes, **10 GiB virtual**, 679 MiB used.
   - Partition layout (qcow2 converted to raw, then `sfdisk -l`): p1 1 MiB BIOS boot; p2 501 MiB EFI System; p3 1 GiB Linux extended boot; **p4 8.5 GiB Linux filesystem** (root).
   - Lint warnings: **3** in the node image (down from 4 in 0004; `zstd` is installed now). Also one in the base build's own lint (`runtime-deps: zstd`, expected for the bare base). See Problems 2.
3. **Publish template: PASS.** VMID 124 confirmed free first.
   - `scp` of the qcow2 to `pve1:/var/tmp` took 7 s; the sha256 matched (`58bef833…4448`).
   - `qm create 124 --name rocky10-bootc-thin-kube-v1.36.6 --ostype l26 --cpu x86-64-v3 --cores 2 --memory 4096 --bios seabios --scsihw virtio-scsi-single --agent enabled=1 --net0 virtio,bridge=vmbr0 --serial0 socket --vga serial0`, then `qm importdisk 124 … pool0`, `qm set 124 --scsi0 pool0:vm-124-disk-0,discard=on,ssd=1 --boot order=scsi0 --ide2 pool0:cloudinit`, **no resize**, `qm template 124`.
   - **Thin provisioning: yes.** `zfs list -o name,volsize,used,refreservation`: `pool0/base-124-disk-0` volsize 10G, used 999M, `refreservation` none. The template converts the zvol name to `base-124-disk-0`.
4. **Growth test: PASS.**
   - Full clone to VMID 126 (`qm clone 124 126 --full 1 --storage pool0`), disk resized to **25G** before first boot (`qm resize 126 scsi0 25G`), cloud-init drive with a throwaway key, static IP 192.168.186.210 on untagged `vmbr0`.
   - SSH answered **12 s** after `qm start`. `systemd-analyze`: 961 ms kernel + 1.8 s initrd + 9.9 s userspace = 12.7 s.
   - `dciab-growroot`: `active (exited)`, status 0. Journal: `data blocks changed from 2230272 to 6162683`.
   - `df -h /sysroot /var`: **24G** each (1.8G used). `lsblk`: `sda` 25G, `sda4` **23.5G** root, from 8.5 GiB.
   - `bootc status`: image `localhost/dciab-node:v1.36.6`. `getenforce`: **Enforcing**. `crio`: active. `kubelet`: activating (waiting for a join, as expected).
   - `cloud-init status --long`: `status: done`, but `extended_status: degraded done`, `errors: []`. The only recoverable item is the deprecation notice `'user' of type string is deprecated in 22.2 and scheduled to be removed in 27.2`, which comes from Proxmox's cloud-init drive (also in 0004, Problems 6). The old `/dev/composefs` resize warning is gone.
   - **Reboot:** `growroot: /dev/sda partition 4 already fills the disk` (`NOCHANGE: partition 4 is size 49301471. it cannot be grown`); the root stayed at 23.5G.
   - Test VM 126 deleted afterwards, with its disk.
5. **Clean up: PARTIAL.** Template 124 kept. Test VM 126, its zvols, the throwaway key and the temp files on `pve1` removed. `images/node-bootc/output/` could **not** be removed: it is root-owned (written by the root build), and the runner's sudoers rule covers only the `build.sh` scripts. Trevor can run `sudo rm -rf images/node-bootc/output`; it is git-ignored. **Template 123 (first-run, 20G disk) still exists:** the task says to ask Trevor whether to delete it (Questions).

## Problems

1. **The template is not small.** `images/node-bootc/config.toml` sets the root `minsize = "2 GiB"`, and `build.sh` checks the node image against it (1359 MiB against a 1843 MiB limit, so it passes), but the disk bootc-image-builder produces is still **10 GiB virtual with an 8.5 GiB root** (p4 17,842,176 sectors), the same layout as the first run. `minsize` is only a floor. bootc-image-builder's own default disk size wins when it is larger. Suspected cause: bootc-image-builder defaults the disk to 10 GiB and makes the root fill what is left, so the config can raise the root but cannot lower it below that default. Nothing is broken: clones grow to their `diskGiB`, and the zvol is thin (999 MiB used of 10G), so the cost on `pool0` is the same as a small template. But the "2 GiB root" in #22 and `config.toml`'s comment is not what ships. I did not edit the recipe (`may_change` is empty). A fix would need a different bootc-image-builder size setting, or building the disk another way; the planner should decide.
2. **Lint warnings that remain (node image: 3).**
   - `nonempty-run-tmp`: `/run/cloud-init`, `/run/lock`, `/run/lock/lvm`, `/run/lvm`.
   - `var-log`: `/var/log/hawkey.log`.
   - `var-tmpfiles`: 15 directories missing tmpfiles.d entries, among them `/var/db/sudo` (0700), `/var/lib/NetworkManager` (0700), `/var/lib/chrony` (0750 chrony), `/var/lib/cloud`; and 8 non-directory files in `/var`, mostly `/var/lib/dnf/history.sqlite*` and dnf repo `countme` files.
   - The `zstd` warning is gone from the node image.
3. **cloud-init is `degraded` only because of the deprecation notice** (above). The task asks for "done, not degraded"; strictly the extended status is `degraded done`. It is not an error, and it comes from how Proxmox writes `user:`, not from this recipe.
4. **`images/node-bootc/output/` is root-owned**, so the runner cannot clean it (Steps 5).

## Artifacts

- **Template `rocky10-bootc-thin-kube-v1.36.6`, VMID 124, node `pve1`, storage `pool0`.** 10 GiB virtual disk (8.5 GiB root), about 1 GiB used, thin; SeaBIOS, `x86-64-v3`, 2 cores, 4096 MiB, guest agent on, cloud-init drive on `ide2`. Rocky 10.3, bootc 1.17.1, CRI-O 1.36.9, Kubernetes 1.36.6, kernel 6.12.0-264.16.1.el10_3. Built from `main` at `366b7bc`; recipe commit `7de0140298a426c89d7ca014f127689a596b5cc0`; bootc-image-builder `sha256:dab86ee6…0d9f8`. Kept.
- **Template 123 (`rocky10-bootc-kube-v1.36.6`, first run, 20G disk) still exists.** Not mine to delete without asking.
- Left on the workstation: `images/node-bootc/output/` (root-owned; git-ignored).

## Workarounds

- Ran the root build myself with `sudo -n`, with stage timings added by piping its output through a loop; this is the task's command otherwise. The task said Trevor would run it; his scoped sudoers rule made that unnecessary.
- Inspected the qcow2's partition layout by converting a copy to raw in my scratch directory and running `sfdisk -l`, because `virt-filesystems` could not read the root-owned file or start its appliance.
- Imported the disk by SSH as root to `pve1` (`qm importdisk`), as in 0004.
- Checked the clone with a throwaway SSH key on its cloud-init drive, because `qm guest exec` is disabled on this Proxmox and Trevor's key is passphrase-protected. The key is deleted.

## Questions

- **For the planner:** how should the template really shrink (Problems 1)? `config.toml`'s `minsize` does not lower bootc-image-builder's 10 GiB default. Is a 10 GiB thin template acceptable, or do you want the root at 2 GiB?
- **For Trevor:** delete template 123 now that 124 passes? (The task says to ask.) And please run `sudo rm -rf images/node-bootc/output` to finish cleanup, or tell me to leave it for the next `build.sh`, which clears it.
- **For the planner:** is `degraded done` from the `user:` deprecation notice acceptable for the "done, not degraded" check?
