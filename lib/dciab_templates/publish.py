"""Publish the Proxmox VE node template through the Proxmox VE API (no SSH).

    upload    payload/nodes/proxmox/disk.qcow2 to storage `local` as content type `import`
              (local gains that type when it lacks it), with its sha256 for Proxmox VE to check
    create    the VM templateID on templateNode, named dciab-node-<sha256[:12]>, its scsi0
              imported from the upload onto spec.platform.proxmox.storage (import-from), with
              no cloud-init drive (CAPMOX attaches its own) and the disk's sha256 in its
              description
    template  convert it to a template; delete the upload

Idempotent: a template at templateID whose description holds this disk's sha256, and that
passes the same checks as a new one (verify), is kept. A VM of DCiaB's own at that ID that
isn't a template yet (a run stopped part-way) is finished; one with no disk that isn't running
is deleted and made again. Any other VM or template there is never touched without
replace=True, and never when it runs or lives on another node.

A VM to delete is deleted only once nothing that can be told beforehand would stop the new one:
the login can create a VM at templateID after the delete (Proxmox VE removes a VM's own ACLs,
/vms/<vmid>, when it destroys it: can_recreate), the storages are enabled and active, and the
upload succeeded. It is deleted right before the new VM is created. If creating it fails then,
templateID is left empty; if converting it fails, the new VM stays there and the next run
finishes it.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import quote

from dciab_hypervisors.post import PostError, PveApi
from dciab_validate.proxmox_privs import TEMPLATE_IMPORT_STORAGE, TEMPLATE_VM_PRIVILEGES

# The node template's hardware: what the node image is built and tested for
# (images/node-bootc). CAPMOX sets each clone's cores, memory, disk size and NIC.
TEMPLATE_SETTINGS = {
    "ostype": "l26",
    "cpu": "x86-64-v3",
    "cores": 2,
    "memory": 4096,
    "bios": "seabios",
    "scsihw": "virtio-scsi-single",
    "agent": "enabled=1",
    "serial0": "socket",
    "vga": "serial0",
    "boot": "order=scsi0",
    "tags": "dciab-node",
}
MARKER = "sha256: "


class PublishError(Exception):
    """The template can't be published. The message is safe to print."""


def template_name(sha: str) -> str:
    return f"dciab-node-{sha[:12]}"


def upload_name(sha: str) -> str:
    return f"{template_name(sha)}.qcow2"


def description(sha: str, ref: str) -> str:
    return f"DCiaB node template (dciab templates). Do not edit.\n{MARKER}{sha}\nfrom: {ref or 'the archive'}"


def has_marker(desc: str, sha: Optional[str] = None) -> bool:
    lines = [ln.strip() for ln in str(desc or "").splitlines()]
    if sha is None:
        return any(ln.startswith(MARKER) for ln in lines) and "dciab templates" in str(desc)
    return f"{MARKER}{sha}" in lines


def vm_settings(doc: dict, sha: str, ref: str) -> dict:
    px = doc["spec"]["platform"]["proxmox"]
    net = f"virtio,bridge={px['bridge']}" + (f",tag={px['vlanTag']}" if px.get("vlanTag") else "")
    return dict(TEMPLATE_SETTINGS, vmid=px["templateID"], name=template_name(sha), net0=net,
                scsi0=f"{px['storage']}:0,import-from={TEMPLATE_IMPORT_STORAGE}:import/{upload_name(sha)},discard=on,ssd=1",
                description=description(sha, ref))


def _try(fn):
    try:
        return fn()
    except PostError as exc:
        if "HTTP 404" in str(exc) or "HTTP 500" in str(exc):
            return None
        raise


def existing(api: PveApi, node: str, vmid: int) -> Optional[dict]:
    """The VM at vmid anywhere in the cluster: {node, template, name, status, config}, or None."""
    rows = api.get("/cluster/resources?type=vm") or []
    row = next((r for r in rows if isinstance(r, dict) and int(r.get("vmid", -1)) == vmid), None)
    if row is None:
        return None
    info = {"node": row.get("node"), "template": int(row.get("template") or 0), "name": row.get("name"),
            "status": row.get("status"), "config": {}}
    if row.get("node") == node:
        conf = _try(lambda: api.get(f"/nodes/{quote(node)}/qemu/{vmid}/config")) or {}
        info["config"] = conf
        if conf:
            # The node's own config is current; /cluster/resources lags (pvestatd), and right
            # after a run it still shows a new template as a VM with no name.
            info["template"] = int(conf.get("template") or 0)
            info["name"] = conf.get("name") or info["name"]
    return info


def _usable(store: dict) -> bool:
    """Enabled and active on the node, as /nodes/<node>/storage says (a disabled storage is
    listed too, with enabled 0)."""
    return str(store.get("enabled", "")) == "1" and str(store.get("active", "")) == "1"


def can_recreate(api: PveApi, vmid: int) -> List[str]:
    """Why the login couldn't create the new VM at vmid once the VM there is deleted, or []: a
    delete is never started that would leave templateID empty for a reason known beforehand.

    Proxmox VE removes the VM's own ACLs (/vms/<vmid>, and its pool membership) when it destroys
    the VM. So the template token of docs/PROXMOX.md, whose VM privileges are on /vms/<vmid>
    only, would lose them with the delete: creating needs them on /vms, propagated (root@pam
    has them)."""
    why: List[str] = []
    try:
        perms = (api.get("/access/permissions?path=/vms") or {}).get("/vms") or {}
    except (PostError, AttributeError):
        perms = None
    if perms is None:
        why.append("its privileges on /vms can't be read")
    else:
        lacking = [p for p in TEMPLATE_VM_PRIVILEGES if str(perms.get(p, "0")) not in ("1", "True", "true")]
        if lacking:
            why.append(f"it holds {', '.join(lacking)} on /vms/{vmid} only through that VM's own ACL, which Proxmox "
                       "VE removes with the VM (not on /vms, propagated)")
    return why


def check_storage(api: PveApi, node: str, doc: dict, log: Callable[[str], None]) -> None:
    px = doc["spec"]["platform"]["proxmox"]
    stores = {s.get("storage"): s for s in (api.get(f"/nodes/{quote(node)}/storage") or []) if isinstance(s, dict)}
    want = stores.get(px["storage"])
    if want is None or "images" not in str(want.get("content") or "").split(","):
        raise PublishError(f"{node} has no storage {px['storage']} that holds VM disks (spec.platform.proxmox.storage)")
    if not _usable(want):
        raise PublishError(f"storage {px['storage']} on {node} isn't enabled and active (spec.platform.proxmox.storage)"
                           "; nothing was changed")
    up = stores.get(TEMPLATE_IMPORT_STORAGE)
    if up is None:
        raise PublishError(f"{node} has no storage {TEMPLATE_IMPORT_STORAGE}, where the node disk is uploaded")
    if not _usable(up):
        raise PublishError(f"storage {TEMPLATE_IMPORT_STORAGE} on {node}, where the node disk is uploaded, isn't "
                           "enabled and active; nothing was changed")
    content = [c for c in str(up.get("content") or "").split(",") if c]
    if "import" not in content:
        api.put(f"/storage/{TEMPLATE_IMPORT_STORAGE}", {"content": ",".join(content + ["import"])})
        log(f"storage {TEMPLATE_IMPORT_STORAGE} now takes the content type import (disk images to import)")


def publish(api: PveApi, node: str, doc: dict, disk: Path, sha: str, ref: str, log: Callable[[str], None],
            replace: bool = False, timeout: float = 3600.0, sleep: Callable[[float], None] = time.sleep) -> str:
    """Publish the template; returns "kept", "finished" or "published"."""
    px = doc["spec"]["platform"]["proxmox"]
    vmid = int(px["templateID"])
    q = f"/nodes/{quote(node)}/qemu"

    def wait(upid, what):
        api.wait_task(node, upid, timeout=timeout, sleep=sleep, what=what)

    have = existing(api, node, vmid)
    doomed = None           # (where, what, ours) of a VM to delete, once the new disk is uploaded
    if have is not None:
        desc = str(have["config"].get("description") or "")
        where = f"VM {vmid} ({have.get('name') or 'no name'}) on {have['node']}"
        if have["node"] != node:
            raise PublishError(f"{where}: templateID is taken on another node than templateNode {node}; choose "
                               "another templateID. Nothing was changed")
        unsound = problems(have["config"], sha) if have["template"] and has_marker(desc, sha) else None
        if unsound == []:
            log(f"template {vmid} on {node} holds this disk (sha256 {sha[:12]}...): kept")
            return "kept"
        if unsound and not replace:
            raise PublishError(f"{where} is this disk's template, but {'; '.join(unsound)}. Nothing was changed. "
                               "Pass --replace to delete it and publish it again (never while it runs), or fix it "
                               "by hand")
        ours_half = not have["template"] and has_marker(desc, sha) and have["config"].get("scsi0")
        if ours_half:
            log(f"{where} is this disk's, made by an earlier run that stopped before the template step: finishing it")
            wait(api.post(f"{q}/{vmid}/template"), f"converting VM {vmid} to a template")
            verify(api, node, vmid, sha)
            log(f"template {vmid} ({template_name(sha)}) on {node}: done")
            return "finished"
        ours_broken = not have["template"] and has_marker(desc, sha) and have.get("status") != "running"
        what = ("this disk's, part-made with no disk" if ours_broken else
                f"this disk's template, but {'; '.join(unsound)}" if unsound else
                "a template of another DCiaB node disk" if has_marker(desc) else
                "a template DCiaB didn't make" if have["template"] else "a VM DCiaB didn't make")
        if ours_broken:
            replace = True          # DCiaB's own, for this disk, with no disk: made again
        if not replace:
            raise PublishError(f"{where} is {what}, not this disk's template (sha256 {sha[:12]}...). Nothing was "
                               "changed. Choose another templateID, or pass --replace to delete it and publish there "
                               "(its clones keep running: CAPMOX makes full clones)")
        if have.get("status") == "running":
            raise PublishError(f"{where} is running: --replace never deletes a running VM. Stop it first if it may go")
        blocked = can_recreate(api, vmid)
        if blocked:
            fix = ("Choose a free templateID" if all(b.startswith("datacenter.cfg") for b in blocked) else
                   f"Either delete VM {vmid} yourself, then set the template token's ACL on /vms/{vmid} again (the "
                   "pveum acl modify line in docs/PROXMOX.md) and run again; or choose a free templateID with that "
                   "ACL; or, for this replacement only, grant the token the DCiaBTemplateVM role on /vms, propagated")
            raise PublishError(
                f"{where} is {what}, and would be deleted, but this login couldn't create the new template there "
                f"afterwards: {'; and '.join(blocked)}. Nothing was changed. {fix} (docs/TEMPLATES.md)")
        doomed = (where, what, ours_broken)

    # Everything that can fail before the new template exists comes before any delete: the
    # storage checks and the upload (Proxmox VE checks its sha256). A bad storage or a failed
    # upload leaves the VM at templateID as it was.
    check_storage(api, node, doc, log)
    name = upload_name(sha)
    volid = f"{TEMPLATE_IMPORT_STORAGE}:import/{name}"
    content = f"/nodes/{quote(node)}/storage/{TEMPLATE_IMPORT_STORAGE}/content"
    listed = api.get(f"{content}?content=import") or []
    if any(isinstance(v, dict) and v.get("volid") == volid for v in listed):
        log(f"deleting {volid}, left by an earlier run")
        wait(api.delete(f"{content}/{quote(volid, safe='')}"), f"deleting {volid}")
    size = disk.stat().st_size
    log(f"uploading {disk} ({size / (1 << 30):.2f} GiB) to {volid}")
    with open(disk, "rb") as fh:
        upid = api.upload(f"/nodes/{quote(node)}/storage/{TEMPLATE_IMPORT_STORAGE}/upload",
                          {"content": "import", "checksum-algorithm": "sha256", "checksum": sha}, name, fh, size,
                          timeout=timeout)
    try:
        wait(upid, f"the upload of {name}")
        if doomed is not None:
            where, what, ours = doomed
            log(f"{'' if ours else '--replace: '}deleting {where} ({what})")
            wait(api.delete(f"{q}/{vmid}?purge=1&destroy-unreferenced-disks=1"), f"deleting VM {vmid}")
        log(f"creating VM {vmid} ({template_name(sha)}) on {node}, its disk imported into {px['storage']}")
        wait(api.post(q, vm_settings(doc, sha, ref)), f"creating VM {vmid}")
        wait(api.post(f"{q}/{vmid}/template"), f"converting VM {vmid} to a template")
    finally:
        try:
            wait(api.delete(f"{content}/{quote(volid, safe='')}"), f"deleting {volid}")
            log(f"deleted the upload {volid}")
        except PostError as exc:
            log(f"couldn't delete the upload {volid} ({exc}); delete it by hand")
    verify(api, node, vmid, sha)
    log(f"template {vmid} ({template_name(sha)}) on {node}: published")
    return "published"


DRIVE = re.compile(r"^(ide|sata|scsi|virtio)\d+$")


def cloudinit_drives(conf: dict) -> List[str]:
    """The drive entries (ide/sata/scsi/virtio<N>) whose volume is a cloud-init volume:
    <storage>:cloudinit as given at create, or vm-<id>-cloudinit as Proxmox VE names it."""
    out = []
    for key, value in conf.items():
        if not DRIVE.match(str(key)):
            continue
        volume = str(value).split(",", 1)[0]
        name = volume.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if name == "cloudinit" or re.fullmatch(r"(vm|base)-\d+-cloudinit(\.\w+)?", name):
            out.append(key)
    return sorted(out)


def problems(conf: dict, sha: str) -> List[str]:
    """What keeps the VM config `conf` from being this disk's node template ([] when nothing)."""
    out: List[str] = []
    if str(conf.get("template") or "0") != "1":
        out.append("it isn't a template")
    if not conf.get("scsi0"):
        out.append("it has no scsi0 disk")
    if conf.get("ide2") or cloudinit_drives(conf):
        out.append("it has a cloud-init drive (CAPMOX attaches its own, and a second one wins over it)")
    if not has_marker(str(conf.get("description") or ""), sha):
        out.append("its description doesn't hold the disk's sha256")
    return out


def verify(api: PveApi, node: str, vmid: int, sha: str) -> None:
    found = problems(api.get(f"/nodes/{quote(node)}/qemu/{vmid}/config") or {}, sha)
    if found:
        raise PublishError(f"template {vmid} on {node}: " + "; ".join(found))


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def node_disk(payload: Path) -> tuple:
    """(disk path, sha256, ref) of the Proxmox VE node disk payload/nodes/nodes.yaml lists."""
    import yaml
    listing = payload / "nodes" / "nodes.yaml"
    if not listing.is_file():
        raise PublishError(f"{listing}: no such file. This archive carries no node disks (build/archive.py nodes)")
    try:
        disks = (yaml.safe_load(listing.read_text()) or {}).get("disks") or []
    except yaml.YAMLError as exc:
        raise PublishError(f"{listing}: not valid YAML ({exc})") from None
    entry = next((d for d in disks if isinstance(d, dict) and d.get("platform") == "proxmox"), None)
    if entry is None:
        raise PublishError(f"{listing} lists no Proxmox VE disk: this archive was built before it was published")
    rel = str(entry.get("file") or "")
    disk = (payload / rel).resolve()
    if os.path.isabs(rel) or ".." in Path(rel).parts or not str(disk).startswith(str(payload.resolve()) + os.sep):
        raise PublishError(f"{listing}: {rel!r} isn't a path inside the payload")
    sha = str(entry.get("sha256") or "")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise PublishError(f"{listing}: the Proxmox VE disk has no sha256")
    if not disk.is_file():
        raise PublishError(f"{disk}: no such file (listed in {listing})")
    return disk, sha, str(entry.get("ref") or "")
