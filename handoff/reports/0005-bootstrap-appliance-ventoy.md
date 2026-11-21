---
id: "0005"
result: blocked
commit: 619c9a0
date: 2026-11-21
---

## Summary

Steps 1-5 passed: the appliance image and the 42 GiB USB image built, and a test config plus two installer ISOs were staged into it. In step 6 the Secure Boot path works as ADR 0002 §4 describes (Ventoy key enrolled in MokManager at the console, Ventoy menu reached, **both installer ISOs boot**). **The appliance vDisk does not boot.** Selecting it shows a thin gray bar, then a black screen with `Press any key to continue`, then returns to the Ventoy menu. It never reaches the kernel, so the failure is before the appliance's own boot chain. I stopped there. Steps 6.3 onward (SSH, mounts, `dciab validate`), 7 (kind, registry, reboot), 8 (VM 126 installer test) and 9 (cleanup) were not run.

Trevor then reported a finding of his own: **Ventoy's Linux vDisk boot doesn't install or boot the appliance as an OS, it simulates a live ISO**, and he is pivoting. I have not verified that claim; it is recorded here as his, and it fits what I saw. I have **not** completed the "two different fixes" the task asks for, so this report does not give a final verdict on ADR 0002 §3 (see Recommendation).

## Steps

1. **Pins: PASS.** kubectl and helm checksums were empty in `bundle.lock`; I filled them (below). kind, Ventoy 1.1.17 and vtoyboot 1.0.36 verified against their published checksums. Ventoy 1.1.18 is the newest release; I kept the pinned 1.1.17.
2. **Plan / build appliance: PASS after 2 failures** (Problems 1 and 2). Trevor ran the root builds, so I have no per-stage timings for the appliance. The base and node stages are cached from earlier runs.
3. **Inspect appliance: PASS.** Image is 1.2 GB (the node image from 0004 was 826 MB for comparison). The Ventoy hook is in `/usr/lib/modules/<kver>/initramfs.img` (the build script greps `lsinitrd` for `ventoy`, and the build fails if it is absent). `bootc container lint` passes.
4. **Build the USB image: PASS, 4m 9s** (`sudo images/usb/build.sh --with-isos`, exit 0). `dciab-usb-0.1.0.img` is 45,097,156,608 bytes (42 GiB virtual), 27.3 GiB on disk right after the build and 29 GiB after staging.
   - Layout (`sfdisk -l`): GPT; p1 start 2048, 88,012,760 sectors (42 GiB), exFAT, label `DCIAB`; p2 start 88,014,808, 65,536 sectors (32 MiB), `VTOYEFI`.
   - ISOs added by the build: `proxmox-ve_9.2-1.iso`.
5. **Test config: PASS.** `~/.dciab/0005/stage.sh` (run by Trevor as root, outside the repo) loop-mounted p1 and copied `dciab.yaml` (validates, 0 errors), dummy `secrets/{bmc,hv,pki}`, `examples/`, `ventoy/ventoy.json`, `README.txt`, and `isos/Rocky-10.3-x86_64-minimal.iso` (downloaded and verified against `bundle.lock nodeImages.rocky10`). The partition holds 29 GiB with 14 GiB free. `sha256` of the final image: `d4450fc6d92437d948417962c24effe81f5276c694e1f88d73876679f333fba6`.
6. **Boot test (VM 125): FAIL, stopped.** See Boot sequence below.
7. **End to end: NOT RUN.**
8. **Installer test (VM 126): NOT RUN as a VM.** The same ISO checks were made from VM 125's Ventoy menu instead (below), which covers the first-screen part of step 8 but not a separate VM.
9. **Clean up: NOT DONE.** VM 125 is still running and the build outputs are still in place (Artifacts).

## Boot sequence (VM 125)

Timings are from the Proxmox node's clock; the workstation's clock differs, so only differences within one column are meaningful.

- **VM:** q35, OVMF, Secure Boot (`efidisk0: pool0:vm-125-disk-0,efitype=4m,pre-enrolled-keys=1`, which Proxmox 9.2.3 resolves to `ms-cert=2023k`), 1 socket x 8 cores, `x86-64-v3`, 16 GiB, virtio NIC `BC:24:11:D0:C1:AB` on untagged `vmbr0`, `vga: std`, boot order `sata0`, guest agent off, one 42 GiB SATA disk (`pool0:vm-125-disk-1`) as the only data disk.
- **Disk load:** Created empty, then the stick image was streamed into the zvol: `zstd -T0 -3 -c image | ssh root@pve1 'zstd -d | dd of=/dev/zvol/pool0/vm-125-disk-1 bs=4M'`. **1m 38s**. `head -c 45097156608 /dev/zvol/... | sha256sum` equals the image's sha256, so the disk is byte-identical.
- **`qm start 125` at 08:09:42**, Secure Boot on.
- **First screen:** blue `ERROR: Verification failed: (0x1A) Security Violation` with an OK button. Expected: Ventoy's shim does not trust Ventoy's loader until the key is enrolled.
- **MokManager:** OK went to MokManager; Trevor chose *Enroll key from disk*, picked `ENROLL_THIS_KEY_IN_MOKMANAGER.cer` from VTOYEFI, confirmed, rebooted. Time to complete not recorded.
- **After enrolment:** Ventoy's menu appears, listing `dciab-bootstrap-0.1.0.raw.vtoy` ("dciab server zero"), Proxmox VE and Rocky 10.3.
- **Selecting the appliance:** a thin landscape gray bar, then about 3 s later a black full-size screen reading `Press any key to continue...` at the top left. A key press or waiting returns to the Ventoy main menu. The VM never got an address; no reply from 192.168.186.219 after more than 5 minutes (`ping`, repeated).
- **Selecting Proxmox VE or Rocky 10.3:** Ventoy's second menu (normal / grub2 / checksum / return) appears. Trevor used grub2 mode and I did not test normal mode; **grub2 mode works**: the screen shows a thin black bar (a Proxmox console scaling quirk), and Enter then starts the Proxmox installer menu or the Rocky installer. So Secure Boot, the enrolled key, Ventoy itself and the stick layout all work. Only the vDisk entry fails.
- **Fix attempt 1, Secure Boot off:** to separate Secure Boot from the vDisk itself, I stopped VM 125, deleted `efidisk0`, and created a new one with `pre-enrolled-keys=0` (blank vars), then started it at 13:05:03 (workstation clock). The VM was running with the same disk. **I did not get a result from this attempt**: Trevor reported his pivot instead. The second fix and the raw-disk boot test were not tried.

## Edits under `may_change`

1. `bundle.lock`: `bootstrapAppliance.toolChecksums.kubectl` and `.helm`, previously empty, now `29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634` and `d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983`. Reason: `build.sh` verifies them, so the build could not run with them empty. (`kind` was already set and verified.)
2. `images/bootstrap-bootc/Containerfile`: added `RUN ln -sfn ../var/usrlocal /usr/local` before the final `systemctl enable … && bootc container lint`. Reason: Problems 1.
3. `images/bootstrap-bootc/install-vtoyboot.sh`: added `rm -f /boot/initramfs-*.img` before cleanup. Reason: Problems 2.

## Problems

1. **Appliance build failed at `bootc container lint`.** `runtime-deps: … a path led outside of the filesystem` (bootc 1.17.1). Cause: the Rocky 10 bootc base's `/usr/local` is an absolute symlink to `/var/usrlocal`. This is the same failure and the same fix as 0004's node image, because both build on the same base. Fix: a relative symlink (edit 2). Suggest the planner fixes it for every image built on this base, in one place.
2. **Appliance build lint warning: non-empty `/boot`.** `vtoyboot.sh` runs its own `dracut` inside the image build and leaves `/boot/initramfs-*.img`; bootc uses only the one under `/usr/lib/modules`. Fixed by removing it (edit 3). The script's own dracut step is expected to fail in a container, and the wrapper handles that.
3. **The appliance vDisk does not boot** (the main finding). Symptom is in the boot sequence above. What I checked:
   - **The disk image is sound.** Reading the appliance's raw disk (`images/bootstrap-bootc/output/image/disk.raw`, 25 GiB) with `sfdisk` and `mtools`: GPT with a 1 MiB BIOS-boot partition, a 501 MiB EFI System partition, a 1 GiB `/boot` and a 24 GiB xfs root. The ESP contains `EFI/BOOT/BOOTX64.EFI` (shim 1,036,024 bytes) and `FBX64.EFI`, plus `EFI/rocky/{shimx64.efi, grubx64.efi, mmx64.efi, shim.efi, grub.cfg, bootuuid.cfg, BOOTX64.CSV}`.
   - **Not the stick or Secure Boot generally:** both ISOs boot through the same Ventoy and MOK path.
   - **Ventoy's docs don't explain it.** Its vtoyboot page says "Both Legacy BIOS and UEFI are supported" and requires a fixed-size, newly created disk with the `.vtoy` suffix, and re-running `vtoyboot.sh` after any kernel or initramfs change. It says nothing about Secure Boot, ESP contents, or this failure. I did not read the separate Ventoy Secure Boot page.
   - **Trevor's finding** (his words, unverified by me): the vDisk is related to "not installing, but simulating a live ISO". I read that as Ventoy mapping the disk image the way it maps an ISO, so the appliance is not booted as an installed system from its own disk.
4. **`dciab validate` schema requires `network.nodes.vlan` even for an untagged bootstrap port.** I had to set a value in the test config to pass validation. Example addresses in `examples/` also fall outside the /24 I used and overlap pools, so I rewrote them in the test config only.
5. **`pre-enrolled-keys=1` pins the 2023 Microsoft certificate** on this Proxmox (`ms-cert=2023k`). It was enough for Ventoy's shim here; I note it in case other hosts' OVMF builds differ.

## Artifacts

- **VM 125 `dciab-server-zero`, node `pve1`, still running**, with `pool0:vm-125-disk-0` (EFI vars, Secure Boot off) and `pool0:vm-125-disk-1` (the 42 GiB stick image). 16 GiB of configured memory counts against CAPMOX's node-RAM guard until it is deleted. VM 126 was never created. Both are mine to delete once Trevor says so.
- On the workstation (not committed, git-ignored): `images/usb/output/dciab-usb-0.1.0.img` (+ `.sha256`), `images/bootstrap-bootc/output/image/disk.raw`, `images/bootstrap-bootc/build/`, `~/.dciab/0005/` (test config, dummy secrets, `stage.sh`).
- The bootc base image and the appliance image `localhost/dciab-bootstrap:0.1.0` stay in container storage (the appliance one is in Trevor's, since the root builds ran as root, and may also be in root's).
- Template 123 is untouched.
- Nothing was written to the real BMC/hypervisor secrets: every secret in the test config is a dummy file.

## Workarounds

- Wrote the stick image into the VM's disk by streaming over SSH into the zvol, not `qm importdisk`, because `local` has 38 GB free and the image is 29 GiB. The result is checksum-identical.
- Test config and ISO staging done by `~/.dciab/0005/stage.sh` run by Trevor as root, since `sudo` needs a password and the staging loop-mounts a partition. The task's step 5 does the same by hand.
- The Rocky ISO was added by that script, not by `images/usb/build.sh`, which only added the Proxmox ISO here.
- Recreated VM 125's EFI vars without Secure Boot (fix attempt 1) rather than asking Trevor to change firmware settings at the console.

## Recommendation (no final verdict yet)

I cannot give the ADR 0002 §3 verdict the task asks for: I ran one of the two fixes (Secure Boot off, no result), and not the raw-disk boot, so the task's own rule for deciding has not been met. What I can say from the evidence:
- Secure Boot, the shim path, the enrolled key, and the DCIAB partition layout all work, and Ventoy's grub2 mode boots installer ISOs correctly. The one thing that fails is the vDisk entry, in a way that gives no error text.
- If Trevor's reading is right (the vDisk entry behaves like a live-ISO mapping, not a real disk boot), then ADR 0002 §2's premise is wrong and §3's fallback is the way forward: install the appliance from an installer ISO onto a local disk in server zero, with the stick still a Ventoy stick for the other installers.
- Before the planner commits to that, one test would settle it cheaply: boot `dciab-usb-0.1.0.img`'s embedded appliance disk directly (not through Ventoy). I have the raw disk and an OVMF VM ready, and it needs no new builds.

## Questions

- **For the planner:** is the appliance to be installed from an ISO onto a disk (ADR 0002 §3)? If so, what is the ISO: the bootc image's own installer ISO from `bootc-image-builder` (`--type anaconda-iso`), or a Rocky 10 ISO plus a kickstart that runs `bootc switch`?
- **For the planner:** do you want me to run the raw-disk boot test and fix attempt 1's Secure Boot-off boot before you rework the ADR? They take about 20 minutes with Trevor at the console.
- **For Trevor:** shall I delete VM 125 and the 42 GiB zvol now? It keeps 16 GiB reserved for CAPMOX, which matters for task 0003.
- **For the planner:** the `network.nodes.vlan` requirement and the `examples/` addresses (Problems 4) need a schema decision.
