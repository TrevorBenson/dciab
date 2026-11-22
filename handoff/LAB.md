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
- Bootc node template: `rocky10-bootc-thin-kube-v1.36.6`, **VMID 124** on `pve1`, storage `pool0` (task 0006 rerun, built from `main` at `366b7bc`): Rocky 10.3, CRI-O 1.36.9, Kubernetes 1.36.6, a 10 GiB disk (8.5 GiB root; `config.toml`'s `minsize = "2 GiB"` did not shrink it), SeaBIOS, a cloud-init drive. The zvol is thin (`refreservation` none, about 1 GiB used). Its root grows to fill a larger disk at boot (a 25G clone gets a 23.5 GiB root). VMID 123, the first-run template with a 20G disk, still exists until Trevor deletes it; VMID 122 is gone. Use 124.
- Importing a disk image: `local` has no `import` content type, so copy the qcow2 to the node (`scp` to `/var/tmp`) and run `qm importdisk <vmid> <file> pool0` over SSH as root, using the diagnostic key.
- On `pve1`, `qm guest exec` is disabled, so a guest-agent command cannot run inside a VM; use SSH.
- `sudo` on the workstation needs a password, so the runner cannot run root builds. The bootc image stages build rootless; only the bootc-image-builder disk step needs root, which Trevor runs.
- A test clone uses a free VMID of its own (the runner may pick one) and is deleted afterwards.

## Bootstrap USB tests (task 0005)

- A Secure Boot VM on `pve1`: `qm create <id> --machine q35 --bios ovmf --efidisk0 pool0:1,efitype=4m,pre-enrolled-keys=1 --sata0 pool0:<GiB> …`. Proxmox 9.2.3 turns `pre-enrolled-keys=1` into `ms-cert=2023k`. For Secure Boot off, create the EFI disk with `pre-enrolled-keys=0`.
- A Ventoy stick image booted under that Secure Boot VM first shows `Verification failed: (0x1A) Security Violation`. Press OK, choose *Enroll key from disk*, pick `ENROLL_THIS_KEY_IN_MOKMANAGER.cer` on VTOYEFI, confirm, reboot. It needs someone at the Proxmox console.
- The Proxmox console scales poorly under Ventoy's grub2 mode (a thin black bar); pressing Enter still starts the installer.
- A large raw image can go straight into a VM's zvol without staging on `local` (about 38 GB free there): `zstd -c image | ssh root@pve1 'zstd -d | dd of=/dev/zvol/pool0/vm-<id>-disk-<n> bs=4M'`; 42 GiB took 1m 38s. Check with `head -c <bytes> /dev/zvol/... | sha256sum`.
