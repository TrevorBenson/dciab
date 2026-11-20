---
id: "0004"
title: "Spike: Rocky 10 bootc node image (RESF SIG recipe, minimal, CRI-O) vs the image-builder template"
status: open
runner: workstation
depends: ["0002"]
report: handoff/reports/0004-node-bootc-bakeoff.md
may_change: [bundle.lock, images/node-bootc/]
---

## Goal

Decide which node image recipe DCiaB keeps: the image-builder template from task 0002 (containerd), or the image-mode recipe in `images/node-bootc/`. That recipe is Rocky 10 bootc from the RESF SIG/Containers recipe with its minimal manifest, plus CRI-O and kubeadm. Build the bootc image, turn it into a Proxmox template, boot it, and compare.

Read `images/node-bootc/README.md` first.

**This is a spike.** You may edit files under `images/node-bootc/` to get the recipe working. For each edit, record in the report what failed and what you changed, so the planner can review it in the PR. Don't change anything else outside `handoff/` except the `bundle.lock` pins.

**Out of scope:**
- Building a cluster from the bootc template. That's a follow-up task if this one recommends bootc.
- Changing the ClusterClasses.
- vSphere.

## Needs

- Rootful podman, git, python3 with PyYAML, and about 30 GB free disk.
- Network access to git.resf.org, the Rocky mirrors, pkgs.k8s.io, download.opensuse.org and quay.io.
- From Trevor, at the stop point:
  - a VMID for the bootc template
  - an IP or DHCP for one test VM on untagged `vmbr0`
  - the same Proxmox access as task 0002

## Steps

1. **Pins.** In `bundle.lock` → `nodeImages.bootc`:
   - `sigRecipe.commit`: the current head of branch `r10`, from `git ls-remote https://git.resf.org/sig_containers/rocky-bootc.git r10`.
   - `bootcImageBuilder`: `quay.io/centos-bootc/bootc-image-builder@sha256:<digest>`. Resolve the digest of the current release tag with `skopeo inspect`, and record which tag it was.
   - Check whether RESF publishes a current prebuilt Rocky 10 bootc image (for example `docker.io/rockylinux/bootc`). Record the tags and dates you find, but leave `baseImage` empty: this spike builds the minimal base from the recipe.
   - Run `python3 -m pytest -q`.
2. **Plan.** Run `images/node-bootc/build.sh --print`. It must show the recipe commit, the manifest `minimal`, and pinned images.
3. **Build.**
   - Run `sudo images/node-bootc/build.sh --type raw --type qcow2 2>&1 | tee /tmp/dciab-bootc-build.log`, with `set -o pipefail`.
   - Record the time for each stage (base, node, disks).
   - Record the sizes of the base image, the node image (`podman image ls`), and each disk.
4. **Inspect the image without booting it.** Run `sudo podman run --rm localhost/dciab-node:v1.36.6 ...` and record:
   - the package count (`rpm -qa | wc -l`)
   - the `crio`, `kubelet` and `kubeadm` versions. CRI-O comes from its v1.36 stream, so record the exact version for pinning later.
   - `bootc container lint` passed (in the build log)
   - `/opt/cni/bin` is a symlink to `/var/lib/cni/bin`
5. **Make a Proxmox template.**
   - First confirm through the API that the chosen VMID doesn't exist. If it does, stop and ask.
   - Import `disk.raw` (or the qcow2) into the chosen VMID on `pve1` storage `pool0`, using whichever method this Proxmox supports: an API upload to `import` storage plus `import-from`, or `qm importdisk`.
   - Use the same VM settings as the task-0002 template: CPU `x86-64-v3`, virtio-scsi, guest agent on, SeaBIOS or UEFI as the image requires. Convert it to a template.
   - Write down the exact steps. They become the product's "publish a template" step later.
6. **Boot test.**
   1. Full-clone the template, give it a cloud-init drive with Trevor's SSH key and the test IP, and boot it on untagged `vmbr0`.
   2. Record the time to SSH.
   3. On the VM, check:
      - `bootc status`
      - `getenforce`
      - `systemctl is-active crio` is active; kubelet is installed and waiting (it starts on join)
      - `crictl info` works
      - the sysctls and modules from `rootfs/` are applied
      - cloud-init finished without errors (`cloud-init status --long`)
   4. Delete the test VM.
7. **Compare.** Build a table with one column for the bootc template and one for the task-0002 template (VMID 121). For the 0002 column, take the numbers from its report, or boot a clone of 121 for the package count and boot time. Rows:
   - build time
   - disk size
   - package count
   - time to SSH
   - container runtime
   - SELinux mode
   - how updates would work
   - how many local patches or workarounds each recipe needed
8. **Clean up.**
   - Keep the bootc template.
   - Remove `images/node-bootc/output/`, and delete local images you don't need.
   - `git status` must show only `handoff/`, `bundle.lock` and any `images/node-bootc/` edits.

## Stop and ask

- **Before step 5:** show Trevor the step 3–4 results and the import plan. Step 5 writes to his Proxmox.
- **If the base recipe fails to build twice with different fixes:** stop. Report the failure and what you tried.

## Report

Use the format in `handoff/README.md`. It must include:
- the step 7 table
- every edit you made under `images/node-bootc/`, with the reason for each
- the template-publishing steps from step 5
- your recommendation, with the deciding reasons

Record durable facts in `LAB.md`: the bootc template's VMID, and how Proxmox imports disks here.
