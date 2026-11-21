---
id: "0005"
title: "Spike: bootstrap appliance as a Ventoy vDisk on the DCiaB USB"
status: open
runner: workstation
depends: ["0004"]
report: handoff/reports/0005-bootstrap-appliance-ventoy.md
may_change: [bundle.lock, images/bootstrap-bootc/, images/usb/, lib/dciab_bootstrap_host/]
---

## Goal

Find out whether the bootstrap appliance can run the DCiaB bootstrap from a Ventoy stick (ADR 0002):
- it boots as a Ventoy Linux vDisk with Secure Boot on
- it configures itself from `dciab.yaml` on the stick
- kind and podman work on it, end to end
- the same stick still boots installers by hand

No long soak: `dciab media` will copy the payload to a local disk and run the heavy state from there (ADR 0002 §5), so the stick only carries the OS.

Read `docs/adr/0002-bootstrap-appliance-on-ventoy.md`, `images/bootstrap-bootc/README.md` and `images/usb/README.md` first.

**This is a spike.** You may edit files under `images/bootstrap-bootc/`, `images/usb/` and `lib/dciab_bootstrap_host/` to get it working. Record each edit in the report: what failed, what you changed, why. Expected trouble spots:
- `install-vtoyboot.sh`: vtoyboot.sh isn't written for image builds
- `Ventoy2Disk.sh` on a loop device
- mounting the `DCIAB` partition while the vDisk on it is in use

**Out of scope:**
- `dciab services`, `hypervisors` and `media` (not implemented)
- `dciab media` and the work disk (not implemented)
- long-running load or power-loss testing
- VLAN-tagged bootstrap ports

## Needs

- Task 0004 done: the Rocky 10 bootc base (`localhost/dciab-rocky-bootc-base:minimal`) in root's podman storage, and `nodeImages.bootc.bootcImageBuilder` pinned. If the base image is gone, rebuild it with `sudo images/node-bootc/build.sh`.
- Rootful podman, xorriso, exfatprogs, parted, curl; about 60 GB free disk.
- From Trevor, **before step 5** (the test config needs them):
  - one free IP on untagged `vmbr0` for the appliance, and its gateway and prefix
  - his SSH public key for `identity.initialAdmins`
  - two VMIDs on `pve1`, the server-zero VM and an installer-test VM, so you can choose the server-zero VM's MAC now
- From Trevor, at the stop point before step 6: the go-ahead to create the VMs, and someone at the Proxmox console (noVNC) for the MokManager screens.

## Steps

1. **Pins.** In `bundle.lock` → `bootstrapAppliance`:
   - Check `ventoy.sha256` against the Ventoy v1.1.17 release's `sha256.txt`, and `vtoyboot.sha256` against the vtoyboot v1.0.36 release page. The planner computed the vtoyboot one from a download; it couldn't read the release page.
   - Fill in `toolChecksums.kubectl` (the published `.sha256` for `kubectl` at `kubernetes.version`, linux/amd64) and `toolChecksums.helm` (the published `.sha256sum` for `helm-<tools.helm>-linux-amd64.tar.gz`). Check `toolChecksums.kind` the same way.
   - If newer Ventoy or vtoyboot releases exist, record them, but keep these versions unless one fails.
   - Run `python3 -m pytest -q`.
2. **Plan.** Run `images/bootstrap-bootc/build.sh --print` and `images/usb/build.sh --print`. They must show the pinned versions and the base image.
3. **Build the appliance.**
   - Run `sudo images/bootstrap-bootc/build.sh --size 24 2>&1 | tee /tmp/dciab-appliance.log`, with `set -o pipefail`.
   - The log must show `install-vtoyboot: Ventoy hook is in the initramfs` and `bootc container lint` passing.
   - Record the build time, the image size (`podman image ls`) and the raw disk size.
4. **Inspect without booting.** In `sudo podman run --rm localhost/dciab-bootstrap:<version> ...`, record:
   - `kind version`, `helm version`, `kubectl version --client`: these must match `bundle.lock`
   - `PYTHONPATH=/usr/share/dciab/lib python3 -m dciab_validate --help` works
   - `dciab validate /usr/share/dciab/examples/proxmox-bare.yaml` fails only because secret files are missing
5. **Build the stick image.**
   - Run `sudo images/usb/build.sh --with-isos`.
   - Record the time, the image size, and the partition layout (`sfdisk -l`, `blkid` on a loop device).
   - Then add a test config to the image's `DCIAB` partition (loop-mount it):
     - `dciab.yaml` made from `examples/proxmox-bare.yaml`, with `bootstrap.interface` set to the server-zero VM's NIC MAC (quoted), `bootstrap.managementVLAN: untagged`, Trevor's IP and gateway in `bootstrap.address` and `network.nodes`, and his key in `identity.initialAdmins`
     - dummy files for every secret it references
   - Keep the config's other values coherent enough that `dciab validate` passes; record anything you had to change.
   - Copy the Rocky 10 ISO from `nodeImages.rocky10` into `isos/` too, if it fits. Do all of this on the image file before step 6 imports it, because the imported volume is a copy.
6. **Boot server zero.**
   1. Create the server-zero VM:
      - OVMF with an EFI disk using `pre-enrolled-keys=1` (Secure Boot on)
      - 8 vCPU, 16 GB RAM
      - virtio NIC on untagged `vmbr0`, with the MAC from step 5
   2. Import the stick image as its only disk (SATA, raw on `pool0`), using the import method task 0004 recorded.
   3. Boot it. Record each screen in order, with the time each took:
      - MokManager: enroll Ventoy's key (Trevor at the console)
      - the Ventoy menu: the appliance entry is the default, with its alias
      - the vDisk boot
      - the console output from `dciab-bootstrap-setup`
   4. Check that it passes:
      - SSH as Trevor's admin user works
      - `ip addr` shows the address on the right NIC
      - `/var/mnt/dciab` is mounted, and its files are mode 0600
      - `dciab validate /var/mnt/dciab/dciab.yaml` passes
      - `mokutil --sb-state` shows Secure Boot enabled
      - `findmnt /` and `lsblk` show how the vDisk is mapped; record this
7. **End to end.** On the appliance, as the admin user with sudo:
   1. Start a registry container (`docker.io/library/registry` is fine for a test), then push and pull one small image through it.
   2. `KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster`, then `kubectl get nodes` (Ready) and run one pod to completion. Delete the cluster.
   3. Reboot the VM cleanly. Check that it comes back with the same address, that SSH works, and that `dciab-bootstrap-setup` succeeded again.
   - Record the time for each part and any errors. This is a pass/fail check, not a measurement.
8. **Installers from the same stick.**
   - With server zero shut down, attach the same disk to the installer-test VM: OVMF with Secure Boot on (the key is per machine, so MokManager appears again), and no other disks except an empty 32 GB disk.
   - From the Ventoy menu, boot the Proxmox VE ISO in `isos/` and confirm the installer's first screen appears. Don't install.
   - Repeat with the Rocky 10 ISO, if step 5 added it.
9. **Clean up.**
   - Delete both VMs and the imported disks.
   - Remove `images/bootstrap-bootc/{build,output}/` and `images/usb/{build,output}/`, and delete local images you don't need (keep the bootc base).
   - `git status` must show only `handoff/`, `bundle.lock` and edits under the `may_change` paths.

## Stop and ask

- **Before step 5:** ask Trevor for the IP, gateway, prefix, SSH key and VMIDs listed under Needs.
- **Before step 6:** show Trevor the step 3–5 results and the VM plan. Step 6 creates VMs on his Proxmox.
- **At step 6.3 and step 8:** the MokManager enrollment needs someone at the console.
- **If the appliance fails to boot as a vDisk after two different fixes:** stop. Report what you tried, and whether booting the raw disk directly (without Ventoy) works, since that tells the planner which part failed.

## Report

Use the format in `handoff/README.md`. It must include:
- every edit under the `may_change` paths, with the reason for each
- the boot sequence from step 6.3, with timings
- the end-to-end results (step 7)
- a verdict on ADR 0002 §3: keep vDisk boot, or fall back to installing the appliance on a disk, with the deciding reasons

Record durable facts in `LAB.md`, such as how to create a Secure Boot OVMF VM through the API here.
