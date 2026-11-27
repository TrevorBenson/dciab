"""The exact Proxmox VE privileges DCiaB's API tokens need, by ACL path (Proxmox VE 9): the
CAPMOX token, the Proxmox CSI plugin's own token (spec.storage.block proxmoxCSI), and the token
`dciab templates` publishes the node template with (spec.platform.proxmox.templateImport).

One source of truth: docs/PROXMOX.md is written from it (tests check they agree), and
`dciab validate --live` compares the token's effective privileges with it.

The token belongs to a dedicated user (e.g. capmox@pve) and is privilege-separated
(privsep=1): its privileges are what both the user's and the token's ACLs grant, so each
ACL below is set for both.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple


class Grant(NamedTuple):
    role: str
    path: str
    propagate: bool
    privileges: Tuple[str, ...]
    why: str


# CAPMOX uploads each machine's bootstrap data as an ISO to this storage
# (local:iso/user-data-<vmid>.iso) and deletes it with the machine.
BOOTSTRAP_ISO_STORAGE = "local"

VM_PRIVILEGES = (
    "VM.Allocate",            # create the clone, delete machines
    "VM.Audit",               # read VM config and status
    "VM.Clone",               # clone the template
    "VM.Config.CDROM",        # attach the bootstrap ISO
    "VM.Config.CPU",
    "VM.Config.Cloudinit",
    "VM.Config.Disk",         # resize the root disk to diskGiB
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",      # the NIC on the bridge and VLAN
    "VM.Config.Options",      # name, tags, boot order, agent settings
    "VM.GuestAgent.Audit",    # read-only agent queries (addresses, info)
    "VM.GuestAgent.Unrestricted",  # guest-exec: CAPMOX reads cloud-init's status this way
    "VM.PowerMgmt",           # start, stop, reboot
)


def grants(px: dict) -> List[Grant]:
    """Every ACL the token needs for spec.platform.proxmox: one role per path, holding only
    the privileges that apply there."""
    out = [
        Grant("DCiaBNodes", "/nodes", True, ("Sys.Audit",),
              "see the nodes, their status and free resources, to place machines"),
        Grant("DCiaBVMs", "/vms", True, VM_PRIVILEGES,
              "clone the template, configure, start, stop and delete machines"),
    ]
    disks = ("Datastore.AllocateSpace", "Datastore.Audit")
    isos = ("Datastore.Allocate", "Datastore.AllocateTemplate", "Datastore.Audit")
    if px["storage"] == BOOTSTRAP_ISO_STORAGE:
        out.append(Grant("DCiaBStorage", f"/storage/{px['storage']}", False, tuple(sorted(set(disks + isos))),
                         "the machines' disks, and each machine's bootstrap-data ISO"))
    else:
        out.append(Grant("DCiaBStorage", f"/storage/{px['storage']}", False, disks, "the machines' disks"))
        out.append(Grant("DCiaBBootstrapISO", f"/storage/{BOOTSTRAP_ISO_STORAGE}", False, isos,
                         "upload and delete each machine's bootstrap-data ISO"))
    sdn = f"/sdn/zones/localnetwork/{px['bridge']}"
    if px.get("vlanTag"):
        sdn += f"/{px['vlanTag']}"
    out.append(Grant("DCiaBNetwork", sdn, False, ("SDN.Use",), "attach machines' NICs to the bridge (and VLAN)"))
    return out


# The Proxmox CSI plugin's token: volumes are VM disks it creates on one storage and attaches
# to the node VMs. Nothing else: it can't create, start or stop VMs.
CSI_VM_PRIVILEGES = (
    "VM.Audit",               # find each node's VM and read its disks
    "VM.Config.Disk",         # attach, detach and resize volumes as VM disks
)
CSI_STORAGE_PRIVILEGES = (
    "Datastore.Allocate",     # delete volumes (it also allows deleting other volumes on this storage)
    "Datastore.AllocateSpace",  # create and resize volumes
    "Datastore.Audit",        # see the storage and its free space (capacity tracking)
)


def csi_grants(csi: dict) -> List[Grant]:
    """Every ACL the Proxmox CSI plugin's token needs for proxmoxCSI (its storage)."""
    return [
        Grant("DCiaBCSIVMs", "/vms", True, CSI_VM_PRIVILEGES,
              "attach and detach volumes as disks of the node VMs"),
        Grant("DCiaBCSIStorage", f"/storage/{csi['storage']}", False, CSI_STORAGE_PRIVILEGES,
              "create, resize and delete volumes on the storage"),
    ]


# `dciab templates`: upload the node disk to `local` (content type import), create the template
# VM at templateID from it on the storage, convert it to a template, delete the upload.
TEMPLATE_IMPORT_STORAGE = BOOTSTRAP_ISO_STORAGE
TEMPLATE_VM_PRIVILEGES = (
    "VM.Allocate",            # create the VM, make it a template, delete it with --replace
    "VM.Audit",               # read its config and status (is it ours, is it a template)
    "VM.Config.CPU",
    "VM.Config.Disk",         # the disk imported from the upload
    "VM.Config.HWType",       # SCSI controller, BIOS, serial console
    "VM.Config.Memory",
    "VM.Config.Network",      # its NIC on the bridge
    "VM.Config.Options",      # name, description (the disk's sha256), tags, boot order, agent
)


def template_grants(px: dict) -> List[Grant]:
    """Every ACL the node template's token needs for spec.platform.proxmox: one VM ID, the
    template's storage, the upload storage and the bridge."""
    out = [
        Grant("DCiaBTemplateNodes", "/nodes", True, ("Sys.Audit",), "see the node, its storage and its tasks"),
        Grant("DCiaBTemplateVM", f"/vms/{px['templateID']}", False, TEMPLATE_VM_PRIVILEGES,
              "create the template at templateID, and only there"),
    ]
    disks = ("Datastore.AllocateSpace", "Datastore.Audit")
    upload = ("Datastore.Allocate", "Datastore.AllocateTemplate", "Datastore.Audit")
    if px["storage"] == TEMPLATE_IMPORT_STORAGE:
        out.append(Grant("DCiaBTemplateStorage", f"/storage/{px['storage']}", False, tuple(sorted(set(disks + upload))),
                         "the template's disk, and the uploaded node disk"))
    else:
        out.append(Grant("DCiaBTemplateStorage", f"/storage/{px['storage']}", False, disks, "the template's disk"))
        out.append(Grant("DCiaBTemplateUpload", f"/storage/{TEMPLATE_IMPORT_STORAGE}", False, upload,
                         "upload the node disk (content type import), allow that type, delete the upload"))
    sdn = f"/sdn/zones/localnetwork/{px['bridge']}"
    if px.get("vlanTag"):
        sdn += f"/{px['vlanTag']}"
    out.append(Grant("DCiaBTemplateNetwork", sdn, False, ("SDN.Use",), "the template's NIC on the bridge (and VLAN)"))
    return out


# Privileges that would let the token change who can do what, or the hosts themselves.
TOO_BROAD = ("Permissions.Modify", "User.Modify", "Sys.Modify", "Sys.PowerMgmt", "Sys.Console",
             "Realm.Allocate", "Group.Allocate")


def compare(want: List[Grant], have: Dict[str, Dict[str, int]],
            root: Optional[Dict[str, int]] = None) -> Tuple[List[str], List[str]]:
    """(missing, too broad): per path, what's missing; what the token can do that it shouldn't."""
    missing = []
    for g in want:
        privs = have.get(g.path) or {}
        # The API maps a held privilege to its ACL's propagate flag: 1, or 0 when the grant
        # does not propagate. Held means present, not truthy.
        lacking = [p for p in g.privileges if p not in privs]
        if lacking:
            missing.append(f"{g.path}: {', '.join(lacking)}")
    broad = []
    for path, privs in sorted({**have, **({"/": root} if root else {})}.items()):
        extra = [p for p in TOO_BROAD if p in (privs or {})]
        if extra:
            broad.append(f"{path}: {', '.join(extra)}")
    return missing, broad


def pveum_commands(px: dict, user: str, token: str, want: Optional[List[Grant]] = None,
                   comment: str = "DCiaB: CAPMOX") -> List[str]:
    """The pveum commands that create the roles, the user, the token and the ACLs
    (want: the grants; default the CAPMOX token's for px)."""
    want = grants(px) if want is None else want
    tokenid = f"{user}!{token}"
    cmds = [f"pveum role add {g.role} --privs \"{','.join(g.privileges)}\"" for g in want]
    cmds += [f"pveum user add {user} --comment \"{comment}\"",
             f"pveum user token add {user} {token} --privsep 1"]
    for g in want:
        cmds.append(f"pveum acl modify {g.path} --users {user} --tokens '{tokenid}' --roles {g.role}"
                    f" --propagate {1 if g.propagate else 0}")
    return cmds
