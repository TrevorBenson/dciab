# Lab environment

Facts about Trevor's lab that tasks rely on. This is test context, not product documentation: DCiaB docs must work for any site and never refer to this file. Runners update it in their report PR when they learn something durable. Planners read it before writing a task.

Never put secrets here: no token secrets, passwords or keys.

## Workstation (runner host)

- Fedora Linux 43, with Docker and Podman; kind uses Docker.
- kind, helm and kubectl are installed at the versions pinned in `bundle.lock`.
- A long-lived kind cluster `dciab-e2e` may exist from earlier runs. Tasks that need a fresh one must ask before deleting it.

## Files outside the repo

Credentials and task files live under `~/.dciab/` on the workstation (directories mode 700, files 600), never in the repo:

- **Credentials, by purpose:** `~/.dciab/<purpose>/<credential>`. They outlive any one task, and cleanup never removes them. For example:
  - `~/.dciab/lab-runner/github-token`: the lab runner's GitHub registration token (task 0011)
  - `~/.dciab/proxmox/<token-name>.token` for later Proxmox tokens, such as task 0008's least-privilege token
- **Task scratch, by task:** `~/.dciab/NNNN/` holds values files, kubeconfigs, CAs and logs for one task. The task's cleanup step says what to keep.
- **Older location:** the existing CAPMOX credentials stay in `~/.dciab-e2e/proxmox.env` (see Proxmox below) until a task moves them.

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
- Bootc node template: `rocky10-bootc-iscsi-kube-v1.36.6`, **VMID 130** on `pve1`, storage `pool0` (task 0006 run 5, `main` at `11cebc3`): Rocky 10.3, CRI-O 1.36.9, Kubernetes 1.36.6, iSCSI initiator (`iscsiadm` 6.2.1.11, `iscsid` enabled) and NFS client for Longhorn, a 10 GiB disk (8.5 GiB root), SeaBIOS, **no cloud-init drive** (add one to hand-made clones only). Thin (1017 MiB used, `refreservation` none). Root grows at boot (25G clone gets 23.5 GiB). `qm guest exec <vm> -- cloud-init status` works (`status: done`, exit code 2 when `degraded`). Templates 122, 123, 124, 127 and 129 are deleted. Use 130.
- Lab runner VM: **VMID 120** `dciab-lab-runner` on `pve1` (pool0, 100G, 8 vCPU x86-64-v3, 16 GiB, tag `dciab-lab`, on boot), **192.168.186.73/24**, user `rocky`. Runs the `images/lab-runner` bootc image (Rocky 10, SELinux Enforcing) as an ephemeral JIT runner for `TrevorBenson/dciab` with labels `self-hosted,linux,x64,dciab-lab`, main only. Bootstrapped from the workstation (local `sudo images/lab-runner/build.sh`, `scp` + `qm importdisk` as in the import note above); the registration token is `/etc/dciab-lab-runner/token` (root, 0600) and its expiry is Trevor's to manage. Runner config is in `/etc/dciab-lab-runner/` on the VM. The admin SSH key is `~/.dciab/lab-runner/admin_key` on the workstation (user `rocky`; use a private known_hosts file: `~/.ssh/known_hosts` has a stale key for this address). The token is a fine-grained PAT for this repository only (Administration: write); **expiry: not known to the runner, Trevor to record here**. To rotate it, replace `~/.dciab/lab-runner/github-token` and `/etc/dciab-lab-runner/token` on the VM (root, 0600, written over stdin), then `systemctl restart dciab-lab-runner`. Smoke test passed twice from `main` (the second run proves the between-job cleanup) and a dispatch from a branch was refused by the job-started hook.
- Importing a disk image: `local` has no `import` content type, so copy the qcow2 to the node (`scp` to `/var/tmp`) and run `qm importdisk <vmid> <file> pool0` over SSH as root, using the diagnostic key.
- `qm guest exec` works on template 130: the image allows `guest-exec` and `guest-exec-status` and loads a narrow SELinux module (`dciab-qemu-ga`) so the agent can run `cloud-init status`. Earlier templates (123, 124, 127) failed that; they are gone. CAPMOX 0.9.2 treats a non-zero exit code as failed, and `cloud-init status` exits 2 for `degraded done`.
- **Root builds run in CI on the lab runner,** not on the workstation: the Rocky bootc base and the lab runner image and disk (`lab-runner-image.yml`), and the node image and its qcow2 (`lab-template.yml`). Tasks consume what those workflows publish.
- **Not in CI yet:** the bootstrap appliance ISO (`images/bootstrap-bootc/build.sh`) and the USB image (`images/usb/build.sh`), which task 0005 needs (#123). Both scripts run as root. Trevor runs them, or the runner does when Trevor has allowed passwordless `sudo` for that run. Once a lab workflow builds them, this line goes.
- A test clone uses a free VMID of its own (the runner may pick one) and is deleted afterwards.

## Proxmox API tokens by purpose (created 2026-11-23)

One user and privilege-separated token per purpose, each with only the privileges its job needs (ACLs on both the user and the token, as `docs/PROXMOX.md`). Secrets live in `~/.dciab/proxmox/<purpose>.env` (directory 700, files 600: `PVE_URL`, `PVE_TLS_FINGERPRINT`, `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET`), never in Git. Copy into GitHub Environment secrets with the same names when the workflows exist.

| Purpose | Token ID | Privileges | Use |
| --- | --- | --- | --- |
| capmox | `capmox@pve!dciab` | exactly `docs/PROXMOX.md` | CAPMOX and `dciab mgmt`/clusters |
| publisher | `dciab-publisher@pve!ci` | the same as capmox, a separate identity | publish node templates, test clones (CI `lab-write`) |
| janitor | `dciab-janitor@pve!ci` | `VM.Audit,VM.Allocate,VM.PowerMgmt` on `/vms`; delete on `/storage/local`; free space on `/storage/pool0` | remove leftover CI VMs and `user-data-*.iso` |
| audit | `dciab-audit@pve!ci` | `PVEAuditor` on `/` | `validate --live`, smoke jobs (CI `lab-read`) |
| netadmin | `dciab-netadmin@pve!setup` | `PVESDNAdmin` on `/sdn`; `Sys.Audit,Sys.Modify` on `/nodes`; `PVEPoolAdmin` on `/pool` | one-time network setup (SDN, EVPN, pools); **expires 2026-12-23**; not for CI |

GitHub Environments (created 2026-11-23, deployable from `main` only): **`lab-read`** (no reviewers) holds the audit token as secrets `PVE_TOKEN_ID` and `PVE_TOKEN_SECRET`; **`lab-write`** (required reviewer: TrevorBenson) holds the publisher token as `PVE_TOKEN_ID`/`PVE_TOKEN_SECRET`, the capmox token as `CAPMOX_TOKEN_ID`/`CAPMOX_TOKEN_SECRET` and the janitor token as `JANITOR_TOKEN_ID`/`JANITOR_TOKEN_SECRET`. Both have the variables `PVE_URL` and `PVE_TLS_FINGERPRINT`. `PVE_URL` is `pve1`'s Tailscale name; the lab runner VM must resolve it, or the variable must change to the LAN address. The netadmin token is in no environment.

The lab CI workflows that use these tokens are described in `handoff/lab/README.md`; their addresses and ID ranges are in `handoff/lab/network.yaml`. **One-time prerequisite:** the `local` storage needs the content type `import` for the API template publish (`pvesm set local --content backup,vztmpl,iso,snippets,import`); it is not set yet.

Roles created: `DCiaBNodes`, `DCiaBVMs`, `DCiaBStorage`, `DCiaBBootstrapISO`, `DCiaBNetwork`, `DCiaBJanitorVMs`, `DCiaBJanitorISO`, `DCiaBNetAdmin`. Other users and tokens on this Proxmox (`claude@pam`, `tofu@pve`, `illuminatus@*`, `root@pam`) were not touched. Not yet granted: datacenter firewall edits (`Sys.Modify` on `/`); add them to netadmin when that task needs them.

## Bootstrap USB tests (task 0005)

- A Secure Boot VM on `pve1`: `qm create <id> --machine q35 --bios ovmf --efidisk0 pool0:1,efitype=4m,pre-enrolled-keys=1 --sata0 pool0:<GiB> …`. Proxmox 9.2.3 turns `pre-enrolled-keys=1` into `ms-cert=2023k`. For Secure Boot off, create the EFI disk with `pre-enrolled-keys=0`.
- A Ventoy stick image booted under that Secure Boot VM first shows `Verification failed: (0x1A) Security Violation`. Press OK, choose *Enroll key from disk*, pick `ENROLL_THIS_KEY_IN_MOKMANAGER.cer` on VTOYEFI, confirm, reboot. It needs someone at the Proxmox console.
- The Proxmox console scales poorly under Ventoy's grub2 mode (a thin black bar); pressing Enter still starts the installer.
- A large raw image can go straight into a VM's zvol without staging on `local` (about 38 GB free there): `zstd -c image | ssh root@pve1 'zstd -d | dd of=/dev/zvol/pool0/vm-<id>-disk-<n> bs=4M'`; 42 GiB took 1m 38s. Check with `head -c <bytes> /dev/zvol/... | sha256sum`.


## CAPMOX on this Proxmox (task 0003)

- The node network for a cluster is untagged `vmbr0`: nodes `192.168.186.200-.209/24`, gateway `.1`, API VIP `.199`, DNS `192.168.186.1`, pod CIDR `10.250.0.0/16`, service CIDR `10.251.0.0/16`. The workstation reaches all of it.
- The CAPMOX credentials Secret (`<cluster>-proxmox-credentials`: keys `url`, `token`, `secret`) is built from `~/.dciab/proxmox/capmox.env` (`PVE_URL`, `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET`, `PVE_TLS_FINGERPRINT`); the older `~/.dciab-e2e/proxmox.env` (the broad `claude@pam!claude-dciab` token) still exists and should be retired once nothing reads it; the URL host is `pve1`'s Tailscale name.
- CAPMOX 0.9.2 uploads its own cloud-init ISO (`local:iso/user-data-<vmid>.iso`, label `cidata`) and attaches it at `ide0`. A node template that also carries Proxmox's cloud-init drive on `ide2` makes the guest read `ide2` instead, so the node gets DHCP and never gets its bootstrap data.
- A management cluster kept for days goes stale against `main`'s classes, and `helm upgrade classes` can fail on immutable machine templates; start the management cluster fresh for each run.
