# Node images in image mode (bootc) — under evaluation

An alternative to the image-builder recipe in [`images/node/`](../node/README.md): a node is an OCI image built from a Containerfile, then turned into disk images. DCiaB will keep one recipe. This one is being evaluated because:

- **CRI-O.** It's the preferred runtime (see the roadmap backlog). image-builder's playbook always installs containerd and pre-pulls images through it, so switching runtimes there means working against the tool.
- **OCI-native.** The node image is a container image. It can be mirrored into the site registry like any other image, and the same image yields Proxmox/Ironic raw, qcow2 and vSphere vmdk disks.
- **Smaller and declarative.** It starts from the minimal manifest: bootc, systemd, kernel and dnf. Everything else is in the Containerfile.

## How it builds

1. **Base:** Rocky Linux 10 bootc, built from the RESF SIG/Containers [rocky-bootc](https://git.resf.org/sig_containers/rocky-bootc) recipe (branch `r10`, commit pinned in `bundle.lock`), using the `minimal` manifest. `nodeImages.bootc.baseImage` can point at a prebuilt base, pinned by digest, instead.
2. **Node:** `Containerfile` adds CRI-O (from its Kubernetes-minor stream), kubelet, kubeadm and kubectl at the `bundle.lock` Kubernetes version, cloud-init, qemu-guest-agent and chrony, plus `rootfs/`:
   - kernel modules and sysctls for Kubernetes
   - CRI-O defaults: systemd cgroups, and CNI plugin directories under `/var`
   - the kubelet volume plugin directory under `/var`
   - `dciab-growroot.service`, which grows the root filesystem to the disk size at boot
3. **Disks:** bootc-image-builder converts the node image (`--type raw`, `qcow2`, `vmdk`, ...).

```console
$ images/node-bootc/build.sh --print                         # pins and commands; builds nothing
$ sudo images/node-bootc/build.sh --type raw --type qcow2
```

Needs root: the base recipe composes the OS with nested containers (`--cap-add=all`, `/dev/fuse`), and bootc-image-builder reads root's container storage. Disks land in `images/node-bootc/output/`, which is never committed.

## Image mode details that matter for Kubernetes

- `/usr` and `/opt` are read-only; `/etc` and `/var` are writable.
  - CNI plugins that install themselves at boot (Cilium) write to `/opt/cni/bin`, so the image points that at `/var/lib/cni/bin`, and CRI-O looks there.
  - kubelet's FlexVolume directory moves to `/var/lib/kubelet/volumeplugins`.
- The ClusterClasses write registry mirror files for both containerd and CRI-O; CRI-O reads `/etc/containers/registries.conf.d`.
- **Root filesystem growth.** The disk gets grown to the cluster's `diskGiB`, but the image's root partition keeps the size bootc-image-builder gave it, and cloud-init's resize looks at `/` (a composefs overlay) instead of the real filesystem. `dciab-growroot.service` grows the partition behind `/sysroot` with `growpart`, then the XFS filesystem through its read-write mount. cloud-init's own growpart and resize are turned off.
