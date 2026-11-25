---
id: "0005"
title: "Spike: install server zero from the DCiaB USB (Ventoy, appliance installer ISO)"
status: open
runner: workstation
depends: ["0004"]
report: handoff/reports/0005-bootstrap-appliance-ventoy.md
may_change: [bundle.lock, images/bootstrap-bootc/, images/usb/, lib/dciab_bootstrap_host/]
---

## Goal

Find out whether server zero can be installed from the DCiaB USB and come up ready for dciab (ADR 0002, revised):
- The appliance installer ISO boots from the Ventoy menu with Secure Boot on.
- It installs unattended onto the disk matching `spec.bootstrap.workDisk.disk`, and only that disk.
- The installed appliance configures itself from `dciab.yaml`, with the stick plugged in and after it's removed.
- kind and podman work on it, end to end.
- The same stick still boots the Proxmox VE and Rocky installers by hand.

An earlier version of this task booted the appliance as a Ventoy vDisk. The partial report (`handoff/reports/0005-bootstrap-appliance-ventoy.md`) found that Secure Boot, MokManager enrollment, the stick layout and both installer ISOs work, but the vDisk entry returned to Ventoy's menu before reaching a kernel. So the appliance is now an installer ISO. Overwrite that report when you finish this run, keeping a one-line pointer to the earlier finding. Read `docs/adr/0002-bootstrap-appliance-on-ventoy.md`, `images/bootstrap-bootc/README.md` and `images/usb/README.md` first.

**This is a spike.** You may edit files under `images/bootstrap-bootc/`, `images/usb/` and `lib/dciab_bootstrap_host/` to get it working. Record each edit in the report: what failed, what you changed, why. Expected trouble spots:
- how bootc-image-builder merges `kickstart.ks` with its own lines (`ostreecontainer`, and any defaults it adds)
- mounting exFAT and running `python3` in the installer's `%pre`
- `Ventoy2Disk.sh` on a loop device

**Out of scope:**
- `dciab services` and `hypervisors` (step 6's storage checks and step 7.4 cover the part of `dciab media` that needs this enforcing host)
- long-running load or power-loss testing
- VLAN-tagged bootstrap ports

## Needs

- VM 125 from the earlier run: ask Trevor whether to delete it before step 5, or reuse its VMID for the server-zero VM.
- Task 0004's Rocky 10 bootc base (`localhost/dciab-rocky-bootc-base:minimal`) in root's podman storage, and `nodeImages.bootc.bootcImageBuilder` pinned. If the base is gone, Trevor rebuilds it with `sudo images/node-bootc/build.sh`.
- Root builds: the runner can't use `sudo` (`LAB.md`), so Trevor runs `build.sh` for both images.
- Rootful podman, exfatprogs, parted and curl on the workstation, and about 40 GB free disk.
- From Trevor, **before step 4** (the test config needs them):
  - one free IP on untagged `vmbr0` for the appliance, and its gateway and prefix
  - his SSH public key for `identity.initialAdmins`
  - two VMIDs on `pve1`: the server-zero VM and an installer-test VM
- From Trevor, at the stop point before step 5: the go-ahead to create the VMs, and someone at the Proxmox console (noVNC) for MokManager.

## Steps

1. **Pins.** In `bundle.lock`, under `bootstrapAppliance`, check `pyyaml.sha256` against PyPI's published sha256 for `pyyaml-6.0.3.tar.gz`. The tool checksums were verified in the earlier run. Run `python3 -m pytest -q`.
2. **Plan.** Run `images/bootstrap-bootc/build.sh --print` and `images/usb/build.sh --print`. They must show the pinned versions and the base image.
3. **Build.** If Trevor already ran these builds from current `main` in the same session as task 0006's, use their logs and outputs instead of rebuilding. Otherwise Trevor runs, from the repo root, with `set -o pipefail`:
   - `sudo images/bootstrap-bootc/build.sh 2>&1 | tee /tmp/dciab-appliance.log`. The log must show `bootc container lint` passing and end with `output/bootiso/install.iso`.
   - `sudo images/usb/build.sh --with-isos 2>&1 | tee /tmp/dciab-usb.log`
   - Record the ISO and stick image sizes, the lint warnings, and the partition layout (`sfdisk -l` and `blkid` on a loop device).
4. **Test config on the stick image.** Loop-mount the image's `DCIAB` partition, and add:
   - `dciab.yaml`, made from `examples/proxmox-bare.yaml`:
     - `bootstrap.interface`: the server-zero VM's NIC MAC (quoted; pick it now)
     - `bootstrap.managementVLAN: untagged`
     - Trevor's IP and gateway in `bootstrap.address` and `network.nodes`
     - his key in `identity.initialAdmins`
     - `bootstrap.workDisk.disk: {serialNumber: dciab-work}`: step 5 gives the VM's target disk that serial
   - dummy files for every secret the config references
   - the Rocky 10 ISO from `nodeImages.rocky10` in `isos/`, if it fits

   Keep the rest coherent enough that `dciab validate` passes, and record anything you changed. Do all of this on the image file before step 5 imports it, because the imported volume is a copy.
5. **Install server zero.**
   1. Create the server-zero VM:
      - OVMF with an EFI disk using `pre-enrolled-keys=1` (Secure Boot on)
      - 8 vCPU, 16 GB RAM
      - a virtio NIC on untagged `vmbr0`, with the MAC from step 4
   2. Give it two disks:
      - the stick image, imported as a SATA disk (raw on `pool0`), using the import method in `LAB.md`
      - an empty 40 GB SCSI disk with `serial=dciab-work`, the work disk
   3. Boot from the stick disk.
      - Enroll Ventoy's key (Trevor at the console).
      - Check that the Ventoy menu shows the alias, with no default and no countdown.
      - Choose the appliance installer.
   4. Record each screen in order, with how long it took: the shim and GRUB of the ISO, the installer, the `%pre` message (`DCiaB: installing onto /dev/sdX`), the install, and the reboot.
   5. After the install, set the VM to boot from the work disk, keeping the stick attached.
6. **First boot checks.**
   - `lsblk`: the stick is untouched (its partitions and the `DCIAB` label unchanged), and the work disk holds the appliance.
   - SSH as Trevor's admin user works.
   - `ip addr` shows the address on the right NIC.
   - `/var/lib/dciab/site/` holds `dciab.yaml` and `secrets/`, with the files at mode 0600.
   - `dciab validate /var/lib/dciab/site/dciab.yaml` passes.
   - `mokutil --sb-state` says Secure Boot is enabled.
   - `bootc status` and `getenforce` (Enforcing).
   - Root's container storage is the default one on the work disk (the appliance's root filesystem): `sudo podman info --format '{{.Store.GraphRoot}}'` prints `/var/lib/containers/storage`, `ls -Zd /var/lib/containers/storage` shows `container_var_lib_t`, no `/etc/containers/storage.conf.d/50-dciab-media.conf` exists, `sudo dciab media <config> --checksums-only --print` says `storage: unchanged`, and `sudo podman images` is empty: the appliance carries no images (they come from the archive's payload when `dciab media` runs).
   - `sudo ausearch -m avc -ts boot` shows no denials for podman or `/var/lib/dciab`.
7. **End to end.** On the appliance, as the admin user with sudo:
   1. Start a registry container (`docker.io/library/registry` is fine for a test), then push and pull one small image through it.
   2. `KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster`, then `kubectl get nodes` (Ready) and run one pod to completion. Delete the cluster.
   3. Shut down, detach the stick disk, and boot. It must come back with the same address and working SSH, with `dciab-bootstrap-setup` logging that it used the copy in `/var/lib/dciab/site`.
   4. **SELinux path of `dciab media`'s storage step** (#165): the appliance's own storage never moves (its root filesystem is the work disk), so give the step another filesystem: `sudo truncate -s 4G /var/tmp/dciab-alt.img && sudo mkfs.xfs -q /var/tmp/dciab-alt.img && sudo mkdir -p /mnt/dciab-alt-work && sudo mount -o loop /var/tmp/dciab-alt.img /mnt/dciab-alt-work`. Reattach the stick, then on a copy of the config set `spec.bootstrap.workDisk` to `path: /mnt/dciab-alt-work` and run `sudo dciab media <copy> --checksums-only --until storage`. That moves root's storage, which runs the `semanage fcontext` / `restorecon` branch. Passing: `ls -Zd /mnt/dciab-alt-work/containers` shows `container_var_lib_t`, `sudo podman run --rm --network none <a Day 0 image> true` works, and `sudo ausearch -m avc -ts recent` shows no new denials. Then undo it: `sudo rm /etc/containers/storage.conf.d/50-dciab-media.conf`, `sudo semanage fcontext -d /mnt/dciab-alt-work/containers`, check `sudo podman info --format '{{.Store.GraphRoot}}'` names `/var/lib/containers/storage` again, then `sudo umount /mnt/dciab-alt-work` and remove `/var/tmp/dciab-alt.img`.
   - Record the time for each part and any errors. This is a pass/fail check, not a measurement.
8. **Safety check: an ambiguous disk choice.**
   - Attach the stick to a fresh VM with **two** empty disks that both have `serial=dciab-work`, boot the appliance installer, and confirm it stops before partitioning, listing the disks.
   - Record the message, then delete that VM.
9. **Installers from the same stick.**
   - Attach the stick disk to the installer-test VM: OVMF with Secure Boot on (the key is per machine, so MokManager appears again), and an empty 32 GB disk.
   - From the Ventoy menu, boot the Proxmox VE ISO in `isos/` and confirm the installer's first screen appears. Don't install.
   - Repeat with the Rocky 10 ISO, if step 4 added it.
10. **Clean up.**
    - Delete the VMs and the imported disks.
    - Remove `images/bootstrap-bootc/{build,output}/` and `images/usb/{build,output}/`, and delete local images you don't need (keep the bootc base).
    - `git status` must show only `handoff/`, `bundle.lock` and edits under the `may_change` paths.

## Stop and ask

- **Before step 4:** ask Trevor for the IP, gateway, prefix, SSH key and VMIDs listed under Needs.
- **Before step 5:** show Trevor the step 3–4 results and the VM plan. Step 5 creates VMs on his Proxmox.
- **At steps 5.3 and 9:** MokManager enrollment needs someone at the console.
- **If the installer fails in `%pre` or partitioning after two different fixes:** stop, and attach `/tmp/dciab-pre.log` and the anaconda logs from the installer's shell (Alt+F2) to the report.

## Report

Use the format in `handoff/README.md`. It must include:
- every edit under the `may_change` paths, with the reason
- the boot and install sequence from step 5.4, with timings
- the end-to-end and safety results (steps 7 and 8), including the storage labels and AVC checks (steps 6 and 7.4)
- whether the appliance installer is ready to be the documented way to set up server zero

Record durable facts in `LAB.md`, such as how to create a Secure Boot OVMF VM, and a disk with a serial, through the API here.
