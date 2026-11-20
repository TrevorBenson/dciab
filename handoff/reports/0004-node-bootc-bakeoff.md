---
id: "0004"
result: pass
commit: 120e834
date: 2026-11-20
---

## Summary

The bootc image builds, boots and runs CRI-O on Proxmox: a template (VMID 122) exists, a clone reached SSH 12 s after `qm start`, and every step 6 check passed. It needed three small edits under `images/node-bootc/` (below). One real gap remains: **the root filesystem does not grow** (see Problems 4), so a node gets only about 8.5 GiB however large its disk is. I recommend bootc, conditional on fixing that. The comparison has holes: the 0002 template (VMID 121) was deleted during lab cleanup before I could measure it.

## Steps

1. **Pins: PASS.** `sigRecipe.commit` is `7de0140298a426c89d7ca014f127689a596b5cc0` (head of `r10`). `bootcImageBuilder` is `quay.io/centos-bootc/bootc-image-builder@sha256:dab86ee6b46dcade411b37f4e70b07d93b6a06da0dbfec6fe89247a220e0d9f8`, the `latest` tag created 2026-11-09. It has no version label, so `latest` is all that identifies it. Prebuilt Rocky 10 bootc base: none found. `docker.io/rockylinux/bootc` does not exist, and the quay.io and docker.io names I tried (`quay.io/rockylinux/bootc`, `…/rockylinux-bootc`, `quay.io/rocky-bootc/rocky-bootc`, `docker.io/rockylinux/rocky-bootc`) are unauthorized or denied, so I could not list tags. `baseImage` is left empty. `pytest`: 87 passed.
2. **Plan: PASS.** `build.sh --print` shows the commit, manifest `minimal` and the pinned builder.
3. **Build: PASS after 2 failures** (Problems 1 and 2).
   - Base: 185 s, 826 MB.
   - Node: 1.34 GB. A cached rebuild took 3 s; I did not time the first uncached dnf layer separately.
   - Disks: Trevor ran this step as root, so I have no timing. `disk.raw` is 10 GiB virtual (1.43 GiB on disk); `disk.qcow2` is 10 GiB virtual (673 MiB on disk).
   - Base and node stages ran rootless with the same podman commands as `build.sh`. Only the disk step needed root (Workarounds).
4. **Inspect: PASS.** 297 packages; CRI-O 1.36.9; kubelet, kubeadm and kubectl 1.36.6; cri-tools 1.36.0; cloud-init 24.4; qemu-guest-agent 10.1.0; NetworkManager 1.58.0; chrony 4.8; bootc 1.17.1; Rocky 10.3; kernel 6.12.0-264.16.1.el10_3. `bootc container lint` passes with 4 warnings (Problems 5). `/opt/cni/bin` is a symlink to `/var/lib/cni/bin` (after edit 3).
5. **Template: PASS.** VMID 122 (confirmed free first). Steps are under "Publishing a template" below.
6. **Boot test: PASS, with one warning.** A full clone (VMID 123) on untagged `vmbr0` with a cloud-init drive and static IP 192.168.186.210:
   - Time to SSH: **12 s** from `qm start`. systemd reported 9.7 s total (0.96 s kernel, 1.8 s initrd, 6.9 s userspace).
   - `bootc status` shows the booted image `localhost/dciab-node:v1.36.6`.
   - SELinux: **Enforcing**.
   - `crio` is active. `kubelet` is enabled and `activating`, waiting for a join, as expected.
   - `crictl info` works.
   - `net.ipv4.ip_forward = 1` and `net.bridge.bridge-nf-call-iptables = 1`. The `overlay` and `br_netfilter` modules are loaded.
   - `cloud-init status` is `done` but `degraded` (Problems 4).
   - The test VM was deleted afterwards.
7. **Compare: PASS, with gaps.** See the table.
8. **Clean up: PASS.** Template 122 kept; `images/node-bootc/output/` removed; test VM, temp files on `pve1` and the throwaway SSH key removed. `git status` shows `handoff/`, `bundle.lock` and the three `images/node-bootc/` edits.

### Comparison

| | bootc (VMID 122) | image-builder (VMID 121) |
| --- | --- | --- |
| Build time | Base 185 s; node cached 3 s (first node build not timed); disks not timed (run by Trevor) | Packer 9m25s (attempt 4, per 0002 report) |
| Disk size | 20 GiB template disk. Image: 673 MiB qcow2, 1.43 GiB raw on disk | 20 GiB template disk |
| Package count | 297 | not measured: template deleted |
| Time to SSH | 12 s from `qm start` | not measured: template deleted |
| Container runtime | CRI-O 1.36.9 | containerd 2.3.2, runc 1.4.3 |
| SELinux mode | Enforcing | not measured: template deleted |
| Updates | Replace the image (`bootc upgrade` / new template). `/usr` is read-only. | Replace the template. The OS is mutable, so it can drift. |
| Local patches or workarounds | 3 edits under `images/node-bootc/` | 1 patch (`goss_vars_growpart.py`) plus the disk-format default and the untagged-network requirement |

## Problems

1. **Base build failed at `preflight.sh`.** `build.sh` fetched only the pinned commit, so the recipe's `fedora-bootc` git submodule was missing: `Please run: git submodule update --init --recursive`. Cause: the recipe needs its submodule (gitlab.com/fedora/bootc/base-images, pinned by the commit's gitlink). Fixed in `build.sh` (edit 1). I first took it for a root problem: my rootless run failed at the same step with no output, and the message only appeared in Trevor's root run.
2. **Node stage failed at `bootc container lint`** with `runtime-deps: checking whether usr/local/sbin/chcon exists: a path led outside of the filesystem`. It fails on the base image alone. Cause: `/usr/local` is an absolute symlink to `/var/usrlocal`, which this bootc (1.17.1) rejects. Creating `/var/usrlocal` did not help; a relative link (`../var/usrlocal`) does. `--skip runtime-deps` also works, but hides a real check. Fixed in `Containerfile` (edit 2).
3. **`/opt/cni/bin` was a real directory, not a symlink.** `kubelet` pulls in `kubernetes-cni`, which installs the stock plugins there, and `ln -sfn` into an existing directory only adds a stray link inside it. The plugins also sat outside CRI-O's `plugin_dirs`. Fixed in `Containerfile` (edit 3): move them to `/usr/libexec/cni`, then make `/opt/cni/bin` the link.
4. **The root filesystem does not grow, and cloud-init reports `degraded`.** The template disk is 20 GiB, but the root partition (`sda4`, xfs, mounted at `/sysroot`) is 8.5 GiB.
   - `cloud-utils-growpart` is not installed, and cloud-init's `resizefs` looks at `/` (a composefs overlay) and fails: `cc_resizefs.py[WARNING]: Device '/dev/composefs' did not exist. cannot resize: dev=composefs mnt_point=/ path=/`.
   - Effect: CAPMOX grows the disk to the class's `diskGiB`, but the node still gets about 8.5 GiB. Not fixed in this spike. Suggested fix: install `cloud-utils-growpart`, and configure cloud-init to grow `/sysroot` (growpart on `sda4`, then `xfs_growfs /sysroot`).
   - The 8.5 GiB size comes from the image's own disk size in bootc-image-builder, so the planner could also set that larger at build time.
5. **Lint warnings that remain.** `runtime-deps: Missing required runtime dependencies: zstd` (a real missing package), `nonempty-run-tmp` (`/run/cloud-init`), `var-log` (`/var/log/hawkey.log`), `var-tmpfiles` (`/var/db/sudo`, `/var/db/sudo/lectured`). I did not change these.
6. **`cloud-init` also warns that `user:` (string) is deprecated** (removal scheduled for 27.2). That string comes from Proxmox's cloud-init drive, not from this recipe.

## Edits under `images/node-bootc/`

1. `build.sh`: after `git checkout`, run `git submodule update -q --init --recursive --depth 1`. Reason: Problems 1.
2. `Containerfile`: `RUN ln -sfn ../var/usrlocal /usr/local` before the final `bootc container lint`. Reason: Problems 2.
3. `Containerfile`: replace `mkdir -p /opt/cni && ln -sfn /var/lib/cni/bin /opt/cni/bin` with a step that moves the stock plugins to `/usr/libexec/cni`, removes `/opt/cni/bin`, and links it to `/var/lib/cni/bin`. Reason: Problems 3.

## Publishing a template (step 5)

`local` on this Proxmox has no `import` content type, so I used SSH to the node instead of the `import-from` API. All of this ran on `pve1`:

1. Copy the disk (the qcow2 is 673 MiB; the raw is a sparse 10 GiB): `scp disk.qcow2 root@pve1:/var/tmp/`. It took 7 s; the checksums matched.
2. Create the VM: `qm create 122 --name rocky10-bootc-kube-v1.36.6 --ostype l26 --cpu x86-64-v3 --cores 2 --memory 4096 --bios seabios --scsihw virtio-scsi-single --agent enabled=1 --net0 virtio,bridge=vmbr0 --serial0 socket --vga serial0`.
3. Import: `qm importdisk 122 /var/tmp/disk.qcow2 pool0`. It converts to raw on the ZFS pool.
4. Attach and boot order: `qm set 122 --scsi0 pool0:vm-122-disk-0,discard=on,ssd=1 --boot order=scsi0 --ide2 pool0:cloudinit`.
5. Grow to match the 0002 template: `qm resize 122 scsi0 20G`. This does not grow the filesystem (Problems 4).
6. `qm template 122`, then delete the temp file.

The disk is a hybrid GPT (BIOS-boot partition, ESP, `/boot`, root), so SeaBIOS works; I did not try UEFI.

For the test clone: `qm clone 122 123 --full 1 --storage pool0`, then `qm set 123 --ciuser dciab --sshkeys <file> --ipconfig0 ip=192.168.186.210/24,gw=192.168.186.1 --nameserver 192.168.186.1`, then `qm start 123`.

## Recommendation

**Adopt the bootc recipe, once Problems 4 is fixed.** Deciding reasons:
- CRI-O is the preferred runtime, and this recipe has it natively. image-builder always installs containerd.
- It boots in 12 s, enforces SELinux, and the same OCI image yields raw, qcow2 and vmdk disks.
- Its three edits were small and each had a clear cause. The image-builder path needed an upstream-package workaround plus network and disk-format constraints.
- Against it: the unfixed disk growth (a real blocker for nodes with 40 GiB disks), root needed for the disk step, a few lint warnings, and `bootc-image-builder:latest` having no version label.

Could a bootc node drive task 0003? Probably, for the ClusterClass. The r1 class writes containerd mirror files (`/etc/containerd/certs.d/*`) that a CRI-O node simply ignores, and kubeadm autodetects a lone runtime socket. With the placeholder `mirror.invalid` that loses nothing. I did not test a join, and the disk growth finding applies.

## Artifacts

- **Template `rocky10-bootc-kube-v1.36.6`, VMID 122, node `pve1`, storage `pool0`** (20 GiB disk, SeaBIOS, `x86-64-v3`, guest agent on, a cloud-init drive on `ide2`). Kept.
- Rocky 10.3; bootc 1.17.1; CRI-O 1.36.9; Kubernetes 1.36.6.
- Recipe commit `7de0140298a426c89d7ca014f127689a596b5cc0`; bootc-image-builder `sha256:dab86ee6…0d9f8`.

## Workarounds

- Ran the base and node stages **rootless** with `build.sh`'s own podman commands, from a script in `~/.dciab/0004/` that mirrored `build.sh` (checking the recipe out under `~/.dciab/0004/` because a failed root run had left `output/rocky-bootc` root-owned). Trevor ran the disk step as root.
- Imported the disk by SSH as root to `pve1` (`qm importdisk`) rather than the API.
- `qm guest exec` is disabled on this Proxmox, and Trevor's `id_ed25519` is passphrase-protected, so I could not use either for the in-VM checks. I added a throwaway passphrase-less key to the test VM's cloud-init drive, restarted the VM (which also gave the 12 s measurement), and deleted the key afterwards.
- Did not boot a clone of 121 for the comparison: it no longer exists.

## Questions

- For the planner: fix Problems 4 in the recipe, or at build time (a larger image size in bootc-image-builder)? And should `zstd` be added (Problems 5)?
- Should the bootc template replace 121 for task 0003? It would take the class's disk size only after Problems 4 is fixed; until then a node has about 8.5 GiB.
