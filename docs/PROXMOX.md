# Proxmox VE: the API token DCiaB uses

DCiaB and CAPMOX reach Proxmox VE only through its API, with one API token. Nothing DCiaB runs needs SSH to a Proxmox VE host. The token belongs to a dedicated user and is privilege-separated (`privsep=1`): its privileges are what both the user's and the token's ACLs grant, so each ACL is set for both.

This page lists exactly what the token needs on Proxmox VE 9. `dciab validate --live` reads the token's effective privileges (`GET /access/permissions`) and reports anything missing as an error, and anything that could change permissions or the hosts (`Permissions.Modify`, `User.Modify`, `Sys.Modify`, `Sys.PowerMgmt`, `Sys.Console`, `Realm.Allocate`, `Group.Allocate`) as a warning. Both come from `lib/dciab_validate/proxmox_privs.py`, which this page must match (a test checks it).

## Roles and ACLs

One role per path, holding only the privileges that apply there. `<storage>`, `<bridge>` and `<vlanTag>` are `spec.platform.proxmox.storage`, `.bridge` and `.vlanTag` (without a VLAN tag, the network path ends at the bridge).

| Role | Path | Propagate | Privileges | Why |
| --- | --- | --- | --- | --- |
| `DCiaBNodes` | `/nodes` | yes | `Sys.Audit` | see the nodes, their status and free resources, to place machines |
| `DCiaBVMs` | `/vms` | yes | `VM.Allocate`, `VM.Audit`, `VM.Clone`, `VM.Config.CDROM`, `VM.Config.CPU`, `VM.Config.Cloudinit`, `VM.Config.Disk`, `VM.Config.HWType`, `VM.Config.Memory`, `VM.Config.Network`, `VM.Config.Options`, `VM.GuestAgent.Audit`, `VM.GuestAgent.Unrestricted`, `VM.PowerMgmt` | clone the template, configure, start, stop and delete machines |
| `DCiaBStorage` | `/storage/<storage>` | no | `Datastore.AllocateSpace`, `Datastore.Audit` | the machines' disks |
| `DCiaBBootstrapISO` | `/storage/local` | no | `Datastore.Allocate`, `Datastore.AllocateTemplate`, `Datastore.Audit` | upload and delete each machine's bootstrap-data ISO |
| `DCiaBNetwork` | `/sdn/zones/localnetwork/<bridge>/<vlanTag>` | no | `SDN.Use` | attach machines' NICs to the bridge (and VLAN) |

When `<storage>` is `local`, `DCiaBStorage` and `DCiaBBootstrapISO` are one role on `/storage/local` with the privileges of both.

Notes:
- **Guest agent.** CAPMOX reads cloud-init's status inside each machine with the QEMU guest agent's `guest-exec`, which needs `VM.GuestAgent.Unrestricted` on Proxmox VE 9 (`VM.Monitor` was dropped). That privilege lets the token run commands inside machines it manages; it covers only `/vms`.
- **Bootstrap ISOs.** CAPMOX uploads each machine's bootstrap data as `local:iso/user-data-<vmid>.iso` and deletes it with the machine. Deleting needs `Datastore.Allocate` on `local`, which also allows deleting other volumes there. Keep templates and other images off `local` if that matters to the site.
- **No pool.** CAPMOX doesn't put machines in a pool, so the VM privileges propagate from `/vms`. They don't reach the hosts or other users.
- **The template.** Publishing a node template is a separate, one-time task with its own credentials. It isn't part of this token.

## Creating them

On any Proxmox VE node, as an administrator (once per cluster):

```console
pveum role add DCiaBNodes --privs "Sys.Audit"
pveum role add DCiaBVMs --privs "VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.GuestAgent.Audit,VM.GuestAgent.Unrestricted,VM.PowerMgmt"
pveum role add DCiaBStorage --privs "Datastore.AllocateSpace,Datastore.Audit"
pveum role add DCiaBBootstrapISO --privs "Datastore.Allocate,Datastore.AllocateTemplate,Datastore.Audit"
pveum role add DCiaBNetwork --privs "SDN.Use"
pveum user add capmox@pve --comment "DCiaB: CAPMOX"
pveum user token add capmox@pve dciab --privsep 1
pveum acl modify /nodes --users capmox@pve --tokens 'capmox@pve!dciab' --roles DCiaBNodes --propagate 1
pveum acl modify /vms --users capmox@pve --tokens 'capmox@pve!dciab' --roles DCiaBVMs --propagate 1
pveum acl modify /storage/<storage> --users capmox@pve --tokens 'capmox@pve!dciab' --roles DCiaBStorage --propagate 0
pveum acl modify /storage/local --users capmox@pve --tokens 'capmox@pve!dciab' --roles DCiaBBootstrapISO --propagate 0
pveum acl modify /sdn/zones/localnetwork/<bridge>/<vlanTag> --users capmox@pve --tokens 'capmox@pve!dciab' --roles DCiaBNetwork --propagate 0
```

Put the token secret (shown once) where `spec.platform.proxmox.credentials.tokenSecret` points, with `tokenID: capmox@pve!dciab`. Then check it:

```console
$ ./dciab validate dciab.yaml --live
```

`dciab hypervisors` will create the same roles, user, token and ACLs through the API after installing a host (post-install, not built yet).
