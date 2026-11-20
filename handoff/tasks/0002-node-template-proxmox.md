---
id: "0002"
title: Build the Rocky 10 node template on Proxmox
status: blocked
runner: workstation
depends: []
report: handoff/reports/0002-node-template-proxmox.md
may_change: [bundle.lock]
---

## Goal

Build the first DCiaB node template on Trevor's Proxmox lab with `images/node/build.sh`: Rocky Linux 10 with Kubernetes v1.36.6. A real cluster test (a later task) clones nodes from it. Read `images/node/README.md` first: it explains the build and lists the requirements.

This is the first run of the Rocky 10 target, which DCiaB adds on top of upstream image-builder. A failure in the kickstart or in Ansible is a useful result. Report it rather than patching the overlay.

**Out of scope:** using the template in a cluster, vSphere or Metal3 images, and anything under `charts/`.

## Needs

- podman or docker, and python3 with PyYAML and pytest (`dnf install -y python3-pyyaml`, then `pip install --user -r requirements-dev.txt`)
- skopeo (`dnf install -y skopeo`), or another way to read an image digest
- From Trevor (ask in one message):
  - A Proxmox API token for a user with the privileges in image-builder's Proxmox guide. Show him the list, and the "Proxmox token permissions" section of `images/node/README.md`: grant the user and set the token to `privsep=0`.
  - Which node builds the template, the ISO storage, the disk storage and its type (lvm, lvm-thin, zfspool, rbd, dir, ...), and the bridge and VLAN.
  - Whether that network has DHCP, and whether VMs on it can reach this workstation on ports 8000–9000 and the internet.
  - A VMID for the template, or let Proxmox choose.

## Steps

1. **Resolve the pins.** You have network access the planner doesn't.
   - In `bundle.lock` → `nodeImages`:
     - `imageBuilder.digest`: the digest of `imageBuilder.image`, for example `skopeo inspect docker://registry.k8s.io/scl-image-builder/cluster-node-image-builder-amd64:v0.1.55 | jq -r .Digest`. It looks like `sha256:<64 hex>`.
     - `rocky10.isoURL`: the newest Rocky 10 point release's **minimal** ISO, with the versioned path, e.g. `https://download.rockylinux.org/pub/rocky/10.N/isos/x86_64/Rocky-10.N-x86_64-minimal.iso`.
     - `rocky10.isoSHA256`: the ISO's sha256 from the `CHECKSUM` file in that directory.
   - Run `python3 -m pytest -q`. It must pass.
   - Commit this on the task branch. It's the only change allowed outside `handoff/`.
2. **Write the env file.** Copy `images/node/proxmox.env.example` to `~/.dciab/proxmox-build.env`, fill it in with Trevor's answers, and `chmod 600` it. Set `DCIAB_STORAGE_POOL_TYPE` when the storage isn't lvm.
3. **Build.** First note whether a VM named `rocky10-kube-v1.36.6` (or the chosen VMID) already exists. If one does, stop and ask Trevor. Then run `set -o pipefail; images/node/build.sh ~/.dciab/proxmox-build.env 2>&1 | tee /tmp/dciab-node-build.log; echo "exit=$?"`. Expected: Packer finishes with a template named `rocky10-kube-v1.36.6`. It takes about 20–40 minutes.
4. **Check the template.** Using the Proxmox API (or ask Trevor for `qm config <vmid>`), confirm:
   - the VM is the one this build created, and is a template
   - the CPU type is x86-64-v3 or newer
   - the disk size and the qemu-guest-agent setting
5. **Clean up the build host:** `rm -rf images/node/.build`. Keep the template.

## Stop and ask

- **Before step 3:** show Trevor the env file with the token redacted, and the output of `images/node/build.sh ~/.dciab/proxmox-build.env --print-vars` (the base image and Packer variables; nothing is built). Get a go-ahead: step 3 creates a VM and uploads an ISO to his storage.
- **If step 3 fails:** don't edit `images/node/`. If Trevor agrees, retry once with a different setting in the env file only (`DCIAB_CPU_TYPE`, `DCIAB_STORAGE_POOL_TYPE`). Otherwise report `blocked`. Delete any half-built VM that Packer left behind, after asking Trevor.

## Report

Use the format in `handoff/README.md`. Under **Artifacts**, give:
- the template name, VMID, node and storage
- the Rocky point release
- the image-builder digest

From the build log, also give the containerd, runc, kubelet and kubeadm versions Ansible installed.

If the build failed, attach the last 150 lines of the log, with secrets removed, as `handoff/reports/0002-node-template-proxmox/build-tail.log`. Say which stage failed: ISO upload, boot or kickstart, SSH, Ansible, or conversion to a template.
