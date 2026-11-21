# Lab environment

Facts about Trevor's lab that tasks rely on. This is test context, not product documentation: DCiaB docs must work for any site and never refer to this file. Runners update it in their report PR when they learn something durable. Planners read it before writing a task.

Never put secrets here: no token secrets, passwords or keys.

## Workstation (runner host)

- Fedora Linux 43, with Docker and Podman; kind uses Docker.
- kind, helm and kubectl are installed at the versions pinned in `bundle.lock`.
- A long-lived kind cluster `dciab-e2e` may exist from earlier runs. Tasks that need a fresh one must ask before deleting it.

## Proxmox

- Proxmox VE 9.2.3, node `pve1`.
- Storage:
  - `pool0` (zfspool) and `local-lvm` (lvm-thin): block storage, so `raw` disks only.
  - `local` (dir): ISOs. A Rocky 10.3 minimal ISO is already uploaded there.
- API access is user `claude@pam` with token `claude-dciab`, which has privilege separation off (`privsep=0`). The roles are granted to the user, not the token: on this install, roles granted to a privilege-separated token alone gave it nothing. A custom `ImageBuilder` role adds the image-builder privileges to the user's earlier CAPMOX grants.
- Node template: `rocky10-kube-v1.36.6`, VMID 121 (**deleted in a lab cleanup on 2026-11-20; it must be rebuilt before use**) on `pve1`, storage `pool0`, with a 20G disk. Built by task 0002: Rocky 10.3, Kubernetes 1.36.6, containerd 2.3.2, runc 1.4.3. Cluster values use `proxmox.templateID: 121` and `templateNode: pve1`.
- Networks on `vmbr0`:
  - **Untagged:** the network the other VMs use. It can reach the workstation, so use it for node image builds: Packer's kickstart server runs on the workstation.
  - **VLAN 150 (the "k8s VLAN"):** for deployed cluster nodes. DHCP and routing come from the OPNsense VM (VMID 100). It does **not** route back to the workstation, so builds on it hang at "Waiting for SSH".
- The workstation is on 192.168.186.0/24. Its firewalld already allows TCP 1025–65535.
- Bootc node template: `rocky10-bootc-kube-v1.36.6`, VMID 122 on `pve1`, storage `pool0` (task 0004): Rocky 10.3, CRI-O 1.36.9, Kubernetes 1.36.6, a 20 GiB disk, SeaBIOS, a cloud-init drive. Its root filesystem does not grow past about 8.5 GiB yet (see the 0004 report).
- Importing a disk image: `local` has no `import` content type, so copy the qcow2 to the node (`scp` to `/var/tmp`) and run `qm importdisk <vmid> <file> pool0` over SSH as root, using the diagnostic key.
- On `pve1`, `qm guest exec` is disabled, so a guest-agent command cannot run inside a VM; use SSH.
- `sudo` on the workstation needs a password, so the runner cannot run root builds. The bootc image stages build rootless; only the bootc-image-builder disk step needs root, which Trevor runs.
- VMID 122 is the bootc template, so a clone for tests uses a separate VMID (0004 used 123, then deleted it).
