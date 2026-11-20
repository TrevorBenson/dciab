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
- Node template: `rocky10-kube-v1.36.6`, VMID 121 on `pve1`, storage `pool0`, with a 20G disk. Built by task 0002: Rocky 10.3, Kubernetes 1.36.6, containerd 2.3.2, runc 1.4.3. Cluster values use `proxmox.templateID: 121` and `templateNode: pve1`.
- Networks on `vmbr0`:
  - **Untagged:** the network the other VMs use. It can reach the workstation, so use it for node image builds: Packer's kickstart server runs on the workstation.
  - **VLAN 150 (the "k8s VLAN"):** for deployed cluster nodes. DHCP and routing come from the OPNsense VM (VMID 100). It does **not** route back to the workstation, so builds on it hang at "Waiting for SSH".
- The workstation is on 192.168.186.0/24. Its firewalld already allows TCP 1025–65535.
