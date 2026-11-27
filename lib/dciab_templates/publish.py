"""Publish the Proxmox VE node template through the Proxmox VE API (no SSH).

    upload    payload/nodes/proxmox/disk.qcow2 to storage `local` as content type `import`
              (local gains that type when it lacks it), with its sha256 for Proxmox VE to check
    create    the VM templateID on templateNode, named dciab-node-<sha256[:12]>, its scsi0
              imported from the upload onto spec.platform.proxmox.storage (import-from), with
              no cloud-init drive (CAPMOX attaches its own) and the disk's sha256 in its
              description
    template  convert it to a template; delete the upload

Idempotent: a template at templateID whose description holds this disk's sha256 is kept. A VM
of DCiaB's own at that ID that isn't a template yet (a run stopped part-way) is finished. Any
other VM or template there is never touched without replace=True, and never when it runs or
lives on another node.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import quote

from dciab_hypervisors.post import PostError, PveApi
from dciab_validate.proxmox_privs import TEMPLATE_IMPORT_STORAGE

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
        info["config"] = _try(lambda: api.get(f"/nodes/{quote(node)}/qemu/{vmid}/config")) or {}
    return info


def check_storage(api: PveApi, node: str, doc: dict, log: Callable[[str], None]) -> None:
    px = doc["spec"]["platform"]["proxmox"]
    stores = {s.get("storage"): s for s in (api.get(f"/nodes/{quote(node)}/storage") or []) if isinstance(s, dict)}
    want = stores.get(px["storage"])
    if want is None or "images" not in str(want.get("content") or "").split(","):
        raise PublishError(f"{node} has no storage {px['storage']} that holds VM disks (spec.platform.proxmox.storage)")
    up = stores.get(TEMPLATE_IMPORT_STORAGE)
    if up is None:
        raise PublishError(f"{node} has no storage {TEMPLATE_IMPORT_STORAGE}, where the node disk is uploaded")
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
    if have is not None:
        desc = str(have["config"].get("description") or "")
        where = f"VM {vmid} ({have.get('name') or 'no name'}) on {have['node']}"
        if have["node"] != node:
            raise PublishError(f"{where}: templateID is taken on another node than templateNode {node}; choose "
                               "another templateID. Nothing was changed")
        if have["template"] and has_marker(desc, sha):
            log(f"template {vmid} on {node} holds this disk (sha256 {sha[:12]}...): kept")
            return "kept"
        ours_half = not have["template"] and has_marker(desc, sha) and have["config"].get("scsi0")
        if ours_half:
            log(f"{where} is this disk's, made by an earlier run that stopped before the template step: finishing it")
            wait(api.post(f"{q}/{vmid}/template"), f"converting VM {vmid} to a template")
            verify(api, node, vmid, sha)
            log(f"template {vmid} ({template_name(sha)}) on {node}: done")
            return "finished"
        ours_broken = not have["template"] and has_marker(desc, sha) and have.get("status") != "running"
        what = ("this disk's, part-made with no disk" if ours_broken else
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
        log(f"{'--replace: ' if not ours_broken else ''}deleting {where} ({what})")
        wait(api.delete(f"{q}/{vmid}?purge=1&destroy-unreferenced-disks=1"), f"deleting VM {vmid}")

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
                          {"content": "import", "checksum": sha, "checksum-algorithm": "sha256"}, name, fh, size,
                          timeout=timeout)
    try:
        wait(upid, f"the upload of {name}")
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


def verify(api: PveApi, node: str, vmid: int, sha: str) -> None:
    conf = api.get(f"/nodes/{quote(node)}/qemu/{vmid}/config") or {}
    problems: List[str] = []
    if str(conf.get("template") or "0") != "1":
        problems.append("it isn't a template")
    if not conf.get("scsi0"):
        problems.append("it has no scsi0 disk")
    if conf.get("ide2") or any(str(v).find("cloudinit") >= 0 for v in conf.values()):
        problems.append("it has a cloud-init drive (CAPMOX attaches its own, and a second one wins over it)")
    if not has_marker(str(conf.get("description") or ""), sha):
        problems.append("its description doesn't hold the disk's sha256")
    if problems:
        raise PublishError(f"template {vmid} on {node}: " + "; ".join(problems))


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
