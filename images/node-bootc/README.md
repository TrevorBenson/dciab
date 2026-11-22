# Node images in image mode (bootc)

DCiaB's node image recipe. A node is an OCI image built from a Containerfile, then turned into disk images. It replaces the image-builder recipe in [`images/node/`](../node/README.md), which stays until a cluster passes on a bootc template. It was chosen because:

- **CRI-O.** It's the preferred runtime (see the roadmap backlog). image-builder's playbook always installs containerd and pre-pulls images through it, so switching runtimes there means working against the tool.
- **OCI-native.** The node image is a container image. It can be mirrored into the site registry like any other image, and the same image yields Proxmox/Ironic raw, qcow2 and vSphere vmdk disks.
- **Smaller and declarative.** It starts from the minimal manifest: bootc, systemd, kernel and dnf. Everything else is in the Containerfile.

## How it builds

1. **Base:** Rocky Linux 10 bootc, built from the RESF SIG/Containers [rocky-bootc](https://git.resf.org/sig_containers/rocky-bootc) recipe (branch `r10`, commit pinned in `bundle.lock`), using the `minimal` manifest. `nodeImages.bootc.baseImage` can point at a prebuilt base, pinned by digest, instead.
2. **Node:** `Containerfile` adds CRI-O (from its Kubernetes-minor stream), kubelet, kubeadm and kubectl at the `bundle.lock` Kubernetes version, cloud-init, both guest agents (qemu-guest-agent and open-vm-tools, each starting only on its own hypervisor) and chrony, plus `rootfs/`:
   - kernel modules and sysctls for Kubernetes
   - CRI-O defaults: systemd cgroups, and CNI plugin directories under `/var`
   - the kubelet volume plugin directory under `/var`
   - `dciab-growroot.service`, which grows the root filesystem to the disk size at boot
3. **Disks:** bootc-image-builder converts the node image (`--type raw`, `qcow2`, `vmdk`, ...).

```console
$ images/node-bootc/build.sh --print                         # pins and commands; builds nothing
$ sudo images/node-bootc/build.sh --type raw --type qcow2
$ sudo images/node-bootc/build.sh --variant metal --type raw  # Metal3
$ sudo images/node-bootc/build.sh --upgrade-from             # the upgrade gate's starting image
```

Each disk gets a `<disk>.sha256sum` file next to it, the name Metal3 looks for.

## Platforms

One recipe, two variants:

| Platform | Variant | Disk | How it's used |
| --- | --- | --- | --- |
| Proxmox VE | `vm` (default) | `raw` for block storage (ZFS, LVM), or `qcow2` for file storage | A template the Proxmox class clones. No cloud-init drive on the template (below). |
| vSphere | `vm` | `vmdk` (stream-optimized) | Imported as a VM's disk, then the VM is marked as a template the vSphere class clones. CAPV learns each node's address from VMware Tools, and cloud-init's VMware datasource reads the bootstrap data from guestinfo. |
| Metal3 | `metal` | `raw` (Ironic writes it straight to disk) | Served by Ironic over HTTPS with its `.sha256sum`; `dciab cluster-values` points the Metal3 class's `image` values at both. Ironic hands cloud-init a config drive. |

The `metal` variant adds the rest of the kernel's drivers (`kernel-modules` and `kernel-modules-extra`, matching the image's kernel), `linux-firmware`, and an initramfs built for any hardware, so servers find their NICs and the root disk behind any storage controller. VMs don't need these, so the `vm` variant stays small. Its tag is `dciab-node:<version>-metal`.

## Upgrade gate image

`--upgrade-from` builds the node image at `kubernetes.upgradeFrom` in `bundle.lock`: one minor below `kubernetes.version`, with the CRI-O stream of that minor. It is the starting point for testing a Kubernetes upgrade to the bundle's version, which each roadmap phase's gate requires. It is a test image: the archive never ships it.

Needs root: the base recipe composes the OS with nested containers (`--cap-add=all`, `/dev/fuse`), and bootc-image-builder reads root's container storage. Disks land in `images/node-bootc/output/`, which is never committed.

## Image mode details that matter for Kubernetes

- `/usr` and `/opt` are read-only; `/etc` and `/var` are writable.
  - CNI plugins that install themselves at boot (Cilium) write to `/opt/cni/bin`, so the image points that at `/var/lib/cni/bin`, and CRI-O looks there.
  - kubelet's FlexVolume directory moves to `/var/lib/kubelet/volumeplugins`.
- The ClusterClasses write registry mirror files for both containerd and CRI-O; CRI-O reads `/etc/containers/registries.conf.d`.
- **Publishing on Proxmox: no cloud-init drive on the template.** CAPMOX attaches its own NoCloud ISO (`ide0`) to each clone. A Proxmox cloud-init drive on the template (`--ide2 <storage>:cloudinit`) is a second NoCloud source that wins, so the node never gets its address or kubeadm data.
- **Guest agent: `guest-exec` allowed.** CAPMOX reads `cloud-init status` through the guest agent (`guest-exec`, `guest-exec-status`). Rocky's `/etc/sysconfig/qemu-ga` allow-list omits them, so the image adds them. The agent can't limit which commands guest-exec runs, so anyone with Proxmox API access to a node VM can run commands in it as root; limit Proxmox API access accordingly.
- **Thin templates.** bootc-image-builder's disks are 10 GiB with an 8.5 GiB root, its default and its minimum: a root size in its config can raise that, not lower it. The node image is about 1.3 GiB, so publish the template at the size it was built, without growing it. Disk images are sparse and the template's volume stays thin, so the space actually used is about the size of the OS (about 1 GiB). Nodes need at least 25 GiB and grow at clone time.
- **Root filesystem growth.** The disk gets grown to the cluster's `diskGiB`, but the image's root partition keeps the size bootc-image-builder gave it, and cloud-init's resize looks at `/` (a composefs overlay) instead of the real filesystem. `dciab-growroot.service` grows the partition behind `/sysroot` with `growpart`, then the XFS filesystem through its read-write mount. cloud-init's own growpart and resize are turned off.
