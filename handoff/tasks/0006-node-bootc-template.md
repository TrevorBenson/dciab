---
id: "0006"
title: Rebuild the bootc node template with root filesystem growth
status: open
runner: workstation
depends: ["0004"]
report: handoff/reports/0006-node-bootc-template.md
may_change: []
---

## Run 5 (2026-11-22)

Template 129 predates #56, which adds the iSCSI initiator and NFS client Longhorn needs (`iscsid` enabled); `dciab mgmt` now ends with a `longhorn` step (task 0007). Rebuild and republish with a new VMID, run the same checks as run 4 (including `qm guest exec <clone> -- cloud-init status`), and also check on the clone that `iscsiadm --version` works and `iscsid` is enabled. Update `LAB.md` to the new template. Delete 129 once the new one passes (Trevor agreed). Overwrite the report, keeping one line per earlier run.

## Rerun 2 (2026-11-22)

Task 0003 found two defects that stop CAPMOX on templates 123 and 124 (`handoff/reports/0003-proxmox-cluster.md`): the template's own cloud-init drive (`ide2`) hides CAPMOX's cloud-init ISO, and the image's guest agent blocked `guest-exec`. The image now allows `guest-exec` and `guest-exec-status`, and the template is published **without** a cloud-init drive. Rebuild and republish with a new VMID (step 3), and check both fixes (step 4). Overwrite the report, keeping one line per earlier run. Ask Trevor whether to delete 123 and 124 once the new template passes.

## Rerun (2026-11-21)

The first run (report at commit `619c9a0`) built template 123 before the small, thin template change (#22) merged, so 123 has the old 10 GiB root, resized to 20G. Run the task again against current `main` to publish the small template that tasks 0003 and 0007 use. Overwrite the report, keeping a one-line pointer to the first run. Use a new VMID; ask Trevor whether to delete 122 and 123 once the new one passes.

## Goal

Rebuild the bootc node template with the fixes from the task-0004 review, and confirm a clone grows its root filesystem to fill its disk. Task 0003 (the first real cluster) then uses this template.

What changed since 0004:
- The disk is bootc-image-builder's default, 10 GiB with an 8.5 GiB root (the rerun found a `config.toml` root size can't lower it; it has since been removed), published thin at that size.
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
3. **Publish the template.** Use the steps in the 0004 report's "Publishing a template" section, with the new VMID: check that it's free, create the VM, import the disk, attach it and convert it to a template. **Don't add a cloud-init drive:** leave out the report's `--ide2 pool0:cloudinit`, because CAPMOX attaches its own ISO and a second NoCloud source wins over it. `qm config <vmid>` must show no `ide2`. **Skip the resize:** the template keeps the size it was built with. Record the template disk's virtual size and the space it uses on `pool0` (`zfs list -o name,volsize,used,refreservation` for its zvol), so we know whether the pool keeps it thin. If the zvol reserves its full size (`refreservation` equal to `volsize`), tell Trevor: thin provisioning (`sparse`) is a storage setting on `pool0`.
4. **Growth test.**
   1. Full-clone the template to the test VMID, and resize the clone's disk to **25G** before the first boot, the size task 0003's nodes use.
   2. Add a cloud-init drive **to the clone** (`qm set <clone> --ide2 pool0:cloudinit`, then the IP, key and DNS), and boot it. The template itself has none.
   3. Check on the VM:
      - `systemctl status dciab-growroot` succeeded
      - `df -h /sysroot /var` shows about 25G
      - `lsblk` shows the root partition filling the disk
      - `cloud-init status --long` is `done` (`degraded done` from Proxmox's `user:` deprecation notice alone is fine)
      - from `pve1`, `qm guest exec <clone> -- cloud-init status` returns its output (this is how CAPMOX reads it), and `/etc/sysconfig/qemu-ga` lists `guest-exec,guest-exec-status`
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
