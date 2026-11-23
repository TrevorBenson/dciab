"""The exact Proxmox VE privileges the CAPMOX API token needs, by ACL path (Proxmox VE 9).

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


def pveum_commands(px: dict, user: str, token: str) -> List[str]:
    """The pveum commands that create the roles, the user, the token and the ACLs."""
    tokenid = f"{user}!{token}"
    cmds = [f"pveum role add {g.role} --privs \"{','.join(g.privileges)}\"" for g in grants(px)]
    cmds += [f"pveum user add {user} --comment \"DCiaB: CAPMOX\"",
             f"pveum user token add {user} {token} --privsep 1"]
    for g in grants(px):
        cmds.append(f"pveum acl modify {g.path} --users {user} --tokens '{tokenid}' --roles {g.role}"
                    f" --propagate {1 if g.propagate else 0}")
    return cmds
