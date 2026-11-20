# Node images

Cluster nodes are cloned from a VM template that already has the OS, containerd, kubelet and kubeadm at the Kubernetes version pinned in `bundle.lock`. CAPI clones the template and kubeadm joins the node; nothing is installed at boot. This directory builds those templates with [Kubernetes image-builder](https://image-builder.sigs.k8s.io/), the SIG Cluster Lifecycle tool that the CAPI providers' own images come from.

| Platform | Status | Output |
| --- | --- | --- |
| Proxmox VE | Rocky Linux 10 target (this directory); first build is handoff task 0002 | VM template `rocky10-kube-<k8s version>` on a Proxmox node |
| vSphere | Not yet | OVA / template |
| Metal3 | Not yet | Raw image served by Ironic |

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
- **Disk format.** It's `raw` by default, which every Proxmox storage accepts. Upstream image-builder defaults to `qcow2`, which only file storage (dir, NFS, CIFS) supports; ZFS, LVM, LVM-thin and Ceph RBD refuse it. Set `DCIAB_DISK_FORMAT=qcow2` only for file storage, if you want thin qcow2 files.
- **CPU type.** Rocky 10 needs an x86-64-v3 CPU. The build VM uses `x86-64-v3`, and so must the VMs cloned from the template.
- **Template name.** It's `rocky10-kube-<k8s version>`, the name the site config examples use. CAPMOX picks the template by VMID (`proxmox.templateID` in the cluster values). Set `DCIAB_TEMPLATE_VMID` to choose one.

## Building a Proxmox template

Needs:
- podman or docker
- python3 with PyYAML
- network access to the Proxmox API and the internet
- a bridge for the build VM. Its network needs **DHCP**, and the VM must reach this machine (Packer serves the kickstart over HTTP on a random port from 8000 to 9000) and the internet (packages).

1. Create a Proxmox user and API token with the privileges in image-builder's [Proxmox guide](https://image-builder.sigs.k8s.io/capi/providers/proxmox): `Datastore.*`, `SDN.*`, `Sys.AccessNetwork`, `Sys.Audit` and `VM.*` on `/`, propagated. See [Proxmox token permissions](#proxmox-token-permissions) for where to grant them.
2. Copy `proxmox.env.example` outside the repo, fill it in, and `chmod 600` it.
3. Check what will be built, then build:

   ```bash
   images/node/build.sh ~/.dciab/proxmox-build.env --print-vars   # prints the base image and Packer variables; builds nothing
   images/node/build.sh ~/.dciab/proxmox-build.env
   ```

   A build takes roughly 20–40 minutes: ISO upload, install, packages, conversion to a template.

The result is a template on `PROXMOX_NODE`. Record its VMID and node: cluster values need `proxmox.templateID` and `proxmox.templateNode`.

## Proxmox token permissions

A Proxmox API token with **privilege separation** (the default, `privsep=1`) gets only the permissions that both it and its user have. If you grant a role to the token alone, the user lacks it, so the token effectively has nothing. Proxmox reports no error: `/access/permissions` comes back as `{}`, and list calls return empty results.

Use one of these:
- **Recommended for a dedicated build user:** grant the role to the user, and create the token with `privsep=0`, so it inherits the user's permissions.
- Keep `privsep=1`, and grant the same role to **both** the user and the token.

CAPMOX credentials for clusters work the same way.

## Changing versions

- **Kubernetes:** bump `kubernetes.version` in `bundle.lock` and rebuild. The new template gets a new name. A cluster moves to it with a Kubernetes upgrade (`kubernetesVersion` plus `proxmox.templateID`). Classes don't change for that.
- **Rocky point release:** set `nodeImages.rocky10.isoURL` to the versioned path (`.../pub/rocky/10.N/isos/x86_64/Rocky-10.N-x86_64-minimal.iso`), and `isoSHA256` from the `CHECKSUM` file next to it. Superseded releases move to `dl.rockylinux.org/vault/rocky/`.
- **image-builder:** bump `nodeImages.imageBuilder.version` and `image`, then resolve the new digest (`skopeo inspect docker://<image> | jq -r .Digest`). Check the overlay still matches upstream's Rocky 9 / CentOS 10 files.

## Troubleshooting

- **The build VM never gets the kickstart:** check DHCP on the bridge, and that the VM can reach this machine on ports 8000–9000 (`firewall-cmd --add-port=8000-9000/tcp` on a Rocky host).
- **`unsupported format 'qcow2'` when Packer creates the VM:** the storage is block storage. Leave `DCIAB_DISK_FORMAT` empty (it defaults to `raw`).
- **Calls succeed but return nothing, or Packer can't see nodes or storage:** see [Proxmox token permissions](#proxmox-token-permissions).
- **Kernel panic or "CPU not supported" at boot:** the CPU type is older than x86-64-v3.
- **Packer waits for SSH forever:** open the VM console in Proxmox; the installer shows kickstart errors there.
## Known limits

- **Build on a trusted network.** Upstream's Proxmox template sets `insecure_skip_tls_verify`, so the Proxmox certificate isn't checked. The kickstart, with the build user's one-time password, is served over plain HTTP during the build.
- **Hardening is still to come.** The template ships with SELinux permissive and no host firewall, as upstream image-builder's EL images do. Enforcing SELinux and a firewall with Kubernetes rules are planned.
- **Run one build at a time per checkout.** Builds share `images/node/.build/` and the local image tag.
