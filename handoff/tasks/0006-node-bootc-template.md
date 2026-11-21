---
id: "0006"
title: Rebuild the bootc node template with root filesystem growth
status: open
runner: workstation
depends: ["0004"]
report: handoff/reports/0006-node-bootc-template.md
may_change: []
---

## Goal

Rebuild the bootc node template with the fixes from the task-0004 review, and confirm a clone grows its root filesystem to fill its disk. Task 0003 (the first real cluster) then uses this template.

What changed since 0004:
- The disk is built small: `config.toml` gives the root 2 GiB (the image is about 1.3 GiB), and `build.sh` stops if the node image passes 1.8 GiB.
- `dciab-growroot.service` grows the partition behind `/sysroot` and its XFS filesystem at boot.
- cloud-init's own growpart and resize are turned off.
- `zstd`, `xfsprogs` and `cloud-utils-growpart` are installed.

See `images/node-bootc/README.md`.

**Out of scope:** clusters and class changes.

## Needs

- The same tools and access as task 0004.
- `build.sh` must run as root. The runner can't use `sudo` (see `LAB.md`), so Trevor runs the build command.
- From Trevor:
  - a VMID for the new template (VMID 122 stays until this one passes)
  - a VMID and IP for one test VM on untagged `vmbr0`

## Steps

1. **Plan.** Run `images/node-bootc/build.sh --print`. It should show the same pins as task 0004.
2. **Build.**
   - Ask Trevor to run `sudo images/node-bootc/build.sh --type qcow2 2>&1 | tee /tmp/dciab-bootc-build.log` from the repo root.
   - Record:
     - the time for each stage
     - the node image size `build.sh` prints, and the disk's partition layout (`qemu-img info` and `virt-filesystems -l --long -a output/qcow2/disk.qcow2`, or `fdisk -l` on the raw disk)
     - each disk's virtual size and the space it actually uses
     - the lint warnings (fewer than task 0004's four is expected: `zstd` is now installed)
3. **Publish the template.** Use the steps in the 0004 report's "Publishing a template" section, with the new VMID: check that it's free, create the VM, import the disk, attach it and convert it to a template. **Skip the resize:** the template keeps the size it was built with. Record the template disk's virtual size and the space it uses on `pool0` (`zfs list -o name,volsize,used,refreservation` for its zvol), so we know whether the pool keeps it thin. If the zvol reserves its full size (`refreservation` equal to `volsize`), tell Trevor: thin provisioning (`sparse`) is a storage setting on `pool0`.
4. **Growth test.**
   1. Full-clone the template to the test VMID, and resize the clone's disk to **25G** before the first boot, the size task 0003's nodes use.
   2. Give it a cloud-init drive and boot it.
   3. Check on the VM:
      - `systemctl status dciab-growroot` succeeded
      - `df -h /sysroot /var` shows about 25G
      - `lsblk` shows the root partition filling the disk
      - `cloud-init status --long` is `done`, not `degraded`
      - `bootc status`, `getenforce`, and `systemctl is-active crio`, as in task 0004
   4. Reboot once. `dciab-growroot` should report that the partition already fills the disk.
   5. Delete the test VM.
5. **Clean up.**
   - Keep the new template. Ask Trevor whether to delete VMID 122.
   - Remove `images/node-bootc/output/`.

## Stop and ask

- **Before step 3:** Proxmox writes.
- **If growth fails:** collect `journalctl -u dciab-growroot`, `findmnt -rn -o TARGET,SOURCE,OPTIONS`, and `lsblk -f`, then report `blocked`. Don't edit the recipe.

## Report

Use the format in `handoff/README.md`. Under **Artifacts**, give the new template's VMID, name and versions. Update `LAB.md`: the new template, and whether VMID 122 still exists.
