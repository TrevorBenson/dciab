# Node images (image-builder)

> DCiaB is moving to the image-mode recipe in [`images/node-bootc/`](../node-bootc/README.md) (Rocky 10 bootc with CRI-O). This recipe stays until a cluster passes on a bootc template.

Cluster nodes are cloned from a VM template that already has the OS, containerd, kubelet and kubeadm at the Kubernetes version pinned in `bundle.lock`. CAPI clones the template and kubeadm joins the node; nothing is installed at boot. This directory builds those templates with [Kubernetes image-builder](https://image-builder.sigs.k8s.io/), the SIG Cluster Lifecycle tool that the CAPI providers' own images come from.

| Platform | Status | Output |
| --- | --- | --- |
| Proxmox VE | Rocky Linux 10 target | VM template `rocky10-kube-<k8s version>` on a Proxmox node |
| vSphere | Not yet | OVA / template |
| Metal3 | Not yet | Raw image served by Ironic |

image-builder itself can build for many more targets, such as local QEMU images and vSphere OVAs. DCiaB pins and supports only the targets in the table.

**Built images are never committed.** The repo holds only the recipe: the overlay files, the `Containerfile` and `build.sh`. Templates live on the hypervisor. Later, the archive build will carry images as files next to the archive, not in Git.

## How it works

```text
bundle.lock ──► build.sh ──► .build/dciab-node.json (versions, ISO pin; no secrets)
                   │
                   ├─► podman/docker build: image-builder (pinned digest) + overlay/  ─► local image
                   └─► run it with --network host and your proxmox.env:
                         make build-proxmox-rockylinux-10
                           Packer: upload the Rocky ISO, boot a VM, kickstart over HTTP,
                           Ansible installs containerd + Kubernetes, convert the VM to a template
```

- **Versions come from `bundle.lock`.** Kubernetes comes from `kubernetes.version`. The image-builder container (pinned by digest) and the Rocky 10 ISO (URL and sha256) come from `nodeImages`. `build.sh` refuses to run while a pin is empty.
- **Why there is an overlay.** Upstream image-builder supports Proxmox with Rocky 9, and EL10 only on other providers (CentOS Stream 10 on QEMU). `overlay/` adds a Rocky 10 Proxmox target: a var file (`rockylinux-10.json`) and a kickstart (`http/10/ks.cfg.tmpl`). Both are adapted from upstream's Rocky 9 Proxmox and CentOS 10 files. If upstream adds Rocky 10 for Proxmox, drop the overlay and use the upstream target.
- **Disk format.** `raw` by default (`DCIAB_DISK_FORMAT` overrides it). Proxmox block storage (ZFS, LVM, LVM-thin, Ceph RBD) accepts only `raw`; `qcow2`, upstream image-builder's default, needs file storage.
- **Upstream fixes.** `patches/` holds small fixes to the pinned image-builder, applied when the container is built. `goss_vars_growpart.py` corrects one Goss check: for Rocky on Proxmox builds, it expected a package named `cloud-utils`, which no Rocky release has; the installed package is `cloud-utils-growpart`. Each patch says when it can be dropped.
- **CPU type.** Rocky 10 needs an x86-64-v3 CPU. The build VM uses `x86-64-v3`, and so must the VMs cloned from the template.
- **Template name.** It's `rocky10-kube-<k8s version>`, the name the site config examples use. CAPMOX picks the template by VMID (`proxmox.templateID` in the cluster values). Set `DCIAB_TEMPLATE_VMID` to choose one.

## Building a Proxmox template

What the build needs:
- podman or docker, and python3 with PyYAML
- access to the Proxmox API and the internet
- a Proxmox API token with the privileges in image-builder's [Proxmox guide](https://image-builder.sigs.k8s.io/capi/providers/proxmox). A token with privilege separation only gets the privileges that both it and its user hold.
- a network for the build VM that has DHCP and can reach the machine running the build. Packer serves the kickstart over HTTP on a port from 8000 to 9000.

Copy `proxmox.env.example` outside the repo, fill it in, and make it readable only by you (`build.sh` checks it is mode 600). Then:

```bash
images/node/build.sh <env file> --print-vars   # prints the base image and Packer variables; builds nothing
images/node/build.sh <env file>
```

The result is a template on `PROXMOX_NODE`. Record its VMID and node: cluster values need `proxmox.templateID` and `proxmox.templateNode`.

## Changing versions

- **Kubernetes:** bump `kubernetes.version` in `bundle.lock` and rebuild. The new template gets a new name. A cluster moves to it with a Kubernetes upgrade (`kubernetesVersion` plus `proxmox.templateID`). Classes don't change for that.
- **Rocky point release:** set `nodeImages.rocky10.isoURL` to the versioned path (`.../pub/rocky/10.N/isos/x86_64/Rocky-10.N-x86_64-minimal.iso`), and `isoSHA256` from the `CHECKSUM` file next to it. Superseded releases move to `dl.rockylinux.org/vault/rocky/`.
- **image-builder:** bump `nodeImages.imageBuilder.version` and `image`, then resolve the new digest (`skopeo inspect docker://<image> | jq -r .Digest`). Check that the overlay still matches upstream's Rocky 9 and CentOS 10 files, and that each patch in `patches/` still applies or is no longer needed.

## Troubleshooting

- **The build VM never gets the kickstart:** the VM has no DHCP lease, or can't reach the build machine on ports 8000–9000.
- **`unsupported format 'qcow2'`:** the storage doesn't support that disk format (see Disk format above).
- **API calls succeed but return nothing:** the token lacks privileges. With privilege separation on, the token needs them granted as well as its user.
- **Kernel panic or "CPU not supported" at boot:** the CPU type is older than x86-64-v3.
- **Packer waits for SSH forever:** open the VM console in Proxmox; the installer shows kickstart errors there.

## Known limits

- **Build on a trusted network.** Upstream's Proxmox template sets `insecure_skip_tls_verify`, so the Proxmox certificate isn't checked. The kickstart, with the build user's one-time password, is served over plain HTTP during the build.
- **Hardening is still to come.** The template ships with SELinux permissive and no host firewall, as upstream image-builder's EL images do. Enforcing SELinux and a firewall with Kubernetes rules are planned.
- **Run one build at a time per checkout.** Builds share `images/node/.build/` and the local image tag.
