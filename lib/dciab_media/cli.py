"""dciab media: copy the archive to the work disk, and put the bootstrap's heavy state there.

    dciab media <config> (--key PUB | --checksums-only) [--source DIR] [--cosign BIN]
                [--isos-from DIR] [--until STEP] [--print]

Run as root on the bootstrap host (--print excepted). Steps, in order; each is safe to
re-run, and a finished run changes nothing:
  target   the work directory from spec.bootstrap.workDisk:
             path: that directory (it must exist, on a local filesystem)
             disk: WORK_DIR_APPLIANCE on the appliance, which must sit on the disk
                   matching the rootDeviceHints (the appliance installed its root there)
           and enough free space for what is left to copy, plus a margin
  state    <work>/state, dciab's state folder (the CA, sealed passwords, kubeconfigs): state
           an earlier step left next to the config (.dciab/state), or where a link there
           points, is moved there and the link left or repointed, so a command run without
           --state finds the same state (a missing link is put back); across filesystems by
           a synced copy to state.partial, renamed into place; refused while containers run,
           when both folders hold state, or when a state.partial is left
  copy     the archive (--source; default the one this dciab runs from, or on the
           appliance the stick's USB_MOUNT/dciab-<VERSION>) to
           <work>/dciab-<VERSION>/: only the files MANIFEST lists, plus MANIFEST and its
           signature; a file already there with the right size and sha256 is skipped;
           others are written to a temporary name, checked, then renamed into place
  verify   dciab verify-archive on the copy: the signature with --key, then checksums
  storage  podman's storage for root (graphroot) moved to <work>/containers with a
           drop-in in /etc/containers/storage.conf.d, unless it's there already;
           refused while containers are running
  isos     <work>/isos, with the installer ISO bundle.lock pins for the site's hypervisors
           (hypervisors.<spec.hypervisors.type>): from the archive's payload/isos/ if it
           carries them, else from spec.hypervisors.installerFile (ESXi) or --isos-from
           (default: the stick's isos/, USB_MOUNT/isos); each is checked against its
           bundle.lock sha256 before it's placed
  preload  the Day 0 images (CoreDNS, Caddy, chrony, Zot, the pve-assistant) loaded once into
           root's storage from the copy's payload, each checked against its bundle.lock digest
           before it gets the pinned name, so dciab services finds them there

Then it prints the commands that run the next steps from the copy, with their state on
the work disk (--state <work>/state) and services and hypervisors given the ISOs the isos
step placed (--isos <work>/isos). On the appliance they name the config by the stable
SITE path (/var/lib/dciab/site/...), never the versioned folder it links to, which the
next boot's sync replaces.

Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import fcntl
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

import yaml

from dciab_archive import verify as verify_mod
from dciab_media import preload
from dciab_state import SITE

STEPS = ["target", "state", "copy", "verify", "storage", "isos", "preload"]
# On the appliance, the root filesystem is on workDisk.disk; dciab's state lives under /var.
WORK_DIR_APPLIANCE = Path("/var/lib/dciab/work")
STORAGE_DROPIN = Path("/etc/containers/storage.conf.d/50-dciab-media.conf")
# The appliance mounts the stick here (images/bootstrap-bootc, var-mnt-dciab.mount).
USB_MOUNT = Path("/var/mnt/dciab")
HOST_LOCK = Path("/run/lock/dciab-media.lock")
GiB = 1 << 30
# Free space required beyond what is left to copy: 5% of the archive, at least 2 GiB.
MARGIN_FRACTION = 0.05
MARGIN_MIN = 2 * GiB
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p", "afs", "ceph", "glusterfs",
              "fuse.glusterfs", "lustre", "davfs", "fuse.rclone"}
TMP_SUFFIX = ".dciab-media-tmp"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class MediaError(Exception):
    """A step can't go on; the message says why and what to do."""


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------- target

def mount_of(path: Path, mounts_text: str) -> Dict[str, str]:
    """The /proc/mounts entry holding path (the longest mount point above it)."""
    target = os.path.realpath(path)
    best: Dict[str, str] = {}
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if target == mnt or target.startswith(mnt.rstrip("/") + "/") or mnt == "/":
            if len(mnt) >= len(best.get("mountpoint", "")):
                best = {"source": parts[0], "mountpoint": mnt, "fstype": parts[2]}
    if not best:
        raise MediaError(f"{path}: no mount found for it in /proc/mounts")
    return best


def nearest_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def read_mounts() -> str:
    return Path("/proc/mounts").read_text()


def disk_of(source: str, runner: Runner = run) -> List[str]:
    """The whole disks under a block device (a partition, LVM volume or RAID), by lsblk."""
    out = runner(["lsblk", "--json", "--inverse", "-o", "PATH,TYPE", source]).stdout
    found: List[str] = []

    def walk(devs):
        for d in devs or []:
            if d.get("type") == "disk":
                found.append(d["path"])
            walk(d.get("children"))
    walk(json.loads(out).get("blockdevices"))
    return sorted(set(found))


def resolve_target(doc: dict, mounts_text: str, runner: Runner = run,
                   list_disks: Optional[Callable[[], List[Dict[str, object]]]] = None) -> Path:
    try:
        work = doc["spec"]["bootstrap"]["workDisk"]
    except (KeyError, TypeError):
        raise MediaError("the config has no spec.bootstrap.workDisk: set path: (a directory on a host you "
                         "provide) or disk: (the disk the appliance installed onto)") from None
    if not isinstance(work, dict):
        raise MediaError("spec.bootstrap.workDisk must be a mapping with path: or disk: (run dciab validate)")
    if "path" in work:
        if not isinstance(work["path"], str):
            raise MediaError("spec.bootstrap.workDisk.path must be an absolute directory")
        path = Path(work["path"])
        if not path.is_absolute() or not path.is_dir():
            raise MediaError(f"spec.bootstrap.workDisk.path {path}: not an existing directory; create it "
                             "(on a local disk with room for the archive) and run again")
        m = mount_of(path, mounts_text)
        if m["fstype"] in NETWORK_FS:
            raise MediaError(f"{path} is on a {m['fstype']} filesystem ({m['source']}); the work disk must be "
                             "local storage")
        return path.resolve()
    if "disk" in work:
        from dciab_bootstrap_host import install
        try:
            hints = install.hints_from(doc)
            disk = install.match_disk(hints, (list_disks or install.disks)())
        except install.InstallError as exc:
            raise MediaError(str(exc)) from None
        base = WORK_DIR_APPLIANCE.parent
        if not base.is_dir():
            raise MediaError(f"{base} doesn't exist: workDisk.disk is for the DCiaB appliance, which keeps "
                             "its state there. On a host you provide, set workDisk.path instead")
        m = mount_of(nearest_existing(WORK_DIR_APPLIANCE), mounts_text)
        disks = disk_of(m["source"], runner) if m["source"].startswith("/dev/") else []
        want = os.path.realpath(str(disk["path"]))
        if want not in [os.path.realpath(d) for d in disks]:
            raise MediaError(f"{WORK_DIR_APPLIANCE} is on {m['source']} ({', '.join(disks) or 'no disk'}), not on "
                             f"{disk['path']}, the disk matching workDisk.disk. dciab media doesn't partition "
                             "or format disks: install the appliance onto that disk first")
        return WORK_DIR_APPLIANCE
    raise MediaError("spec.bootstrap.workDisk needs path: or disk:")


# ---------------------------------------------------------------- copy

class Plan(NamedTuple):
    source: Path
    dest: Path
    files: Dict[str, str]      # relative path -> sha256 ('' for MANIFEST and its signature)
    to_copy: List[str]
    total_bytes: int
    copy_bytes: int


def archive_version(source: Path) -> str:
    vf = source / "VERSION"
    if not vf.is_file():
        raise MediaError(f"{source}: no VERSION file; is it an unpacked DCiaB archive? (--source)")
    v = vf.read_text().strip()
    if not v or "/" in v or v.startswith("."):
        raise MediaError(f"{vf}: not a usable version: {v!r}")
    return v


def archive_files(source: Path) -> Dict[str, str]:
    mf = source / verify_mod.MANIFEST
    if not mf.is_file():
        raise MediaError(f"{source}: no {verify_mod.MANIFEST}; dciab media copies a packaged archive (--source)")
    try:
        files = verify_mod.parse_manifest(mf.read_text())
    except verify_mod.VerifyError as exc:
        raise MediaError(f"{mf}: {exc}") from None
    files[verify_mod.MANIFEST] = ""
    if (source / verify_mod.SIGNATURE).is_file():
        files[verify_mod.SIGNATURE] = ""
    return files


def same_file(src: Path, dst: Path, sha: str) -> bool:
    """dst is already a correct copy: same size, and the sha256 the MANIFEST lists (or src's)."""
    try:
        if not dst.is_file() or dst.is_symlink():
            return False
        ds, ss = dst.stat(), src.stat()
        if ds.st_size != ss.st_size or (ds.st_mode & 0o7777) != (ss.st_mode & 0o7777):
            return False
    except OSError:
        return False
    return verify_mod.sha256_file(dst) == (sha or verify_mod.sha256_file(src))


def make_plan(source: Path, work: Path) -> Plan:
    source = source.resolve()
    dest = work / f"dciab-{archive_version(source)}"
    files = archive_files(source)
    total = copy = 0
    to_copy: List[str] = []
    for rel, sha in sorted(files.items()):
        src = source / rel
        if not src.is_file():
            raise MediaError(f"{src}: listed in {verify_mod.MANIFEST} but missing from the source")
        size = src.stat().st_size
        total += size
        if dest.resolve() == source or same_file(src, dest / rel, sha):
            continue
        to_copy.append(rel)
        copy += size
    return Plan(source, dest, files, to_copy, total, copy)


def check_space(plan: Plan, work: Path, disk_usage=None, extra: int = 0) -> int:
    """Free bytes needed: what is left to copy (plus extra, the state the state step copies
    across filesystems) and a margin. Raises if there's less."""
    copy = plan.copy_bytes + extra
    need = copy + max(MARGIN_MIN, int(plan.total_bytes * MARGIN_FRACTION)) if copy else 0
    free = (disk_usage or shutil.disk_usage)(str(work)).free
    if free < need:
        raise MediaError(f"{work}: {free / GiB:.1f} GiB free, needs {need / GiB:.1f} GiB "
                         f"({copy / GiB:.1f} GiB to copy plus a margin)")
    return need


def copy_one(src: Path, dst: Path, sha: str) -> None:
    """Copy to a temporary name beside dst, check the sha256, keep the mode, rename into place."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()  # a leftover from an interrupted run; unlink never follows a link
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        h = verify_mod.hashlib.sha256()
        with src.open("rb") as fi, os.fdopen(fd, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if sha and h.hexdigest() != sha:
            raise MediaError(f"{src}: sha256 {h.hexdigest()} doesn't match {verify_mod.MANIFEST} ({sha}); "
                             "the source is damaged")
        shutil.copymode(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def check_no_links(base: Path, rel: str) -> None:
    """No directory between base and base/rel may be a symlink: root must not write elsewhere."""
    cur = base
    for part in Path(rel).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise MediaError(f"{cur} is a symlink; remove it and run again")


def do_copy(plan: Plan) -> int:
    if plan.dest.is_symlink():
        raise MediaError(f"{plan.dest} is a symlink; remove it and run again")
    plan.dest.mkdir(parents=True, exist_ok=True)
    for n, rel in enumerate(plan.to_copy, 1):
        say(f"  [{n}/{len(plan.to_copy)}] {rel}")
        check_no_links(plan.dest, rel)
        copy_one(plan.source / rel, plan.dest / rel, plan.files[rel])
    return len(plan.to_copy)


def do_verify(dest: Path, key: Optional[Path], cosign: str, runner: Runner = verify_mod.run) -> None:
    try:
        problems = verify_mod.verify_dir(dest, key, cosign, runner)
    except verify_mod.VerifyError as exc:
        raise MediaError(f"verify {dest}: {exc}") from None
    if problems:
        raise MediaError(f"{dest} does NOT match its {verify_mod.MANIFEST}; not going on (files left by an "
                         f"earlier build of the same version: remove {dest} and run again):\n  "
                         + "\n  ".join(problems[:20]) + (f"\n  ... {len(problems) - 20} more" if len(problems) > 20 else ""))


# ---------------------------------------------------------------- storage

DEFAULT_GRAPHROOT = "/var/lib/containers/storage"


def dropin_text(graphroot: Path) -> str:
    if any(c in str(graphroot) for c in '"\\') or any(ord(c) < 0x20 or ord(c) == 0x7f for c in str(graphroot)):
        raise MediaError(f"{graphroot!r}: the work directory's path can't contain quotes, backslashes or "
                         "control characters")
    return ("# Written by dciab media: root's container storage on the work disk.\n"
            f"[storage]\ngraphroot = \"{graphroot}\"\n")


def current_graphroot(runner: Runner = run) -> str:
    return runner(["podman", "info", "--format", "{{.Store.GraphRoot}}"]).stdout.strip()


def same_filesystem(a: str, b: Path) -> bool:
    """Whether path a and (the nearest existing parent of) b are on one filesystem."""
    try:
        return os.stat(a).st_dev == os.stat(nearest_existing(b)).st_dev
    except OSError:
        return False


def storage_kept(graphroot: Path, current: str) -> bool:
    """Whether podman's current graphroot can stay: it is graphroot, or it is already on the
    work disk (graphroot's filesystem). On the appliance the whole root filesystem is the work
    disk, so /var/lib/containers/storage stays where it is and nothing is moved or labelled."""
    return os.path.realpath(current) == os.path.realpath(graphroot) or same_filesystem(current, graphroot.parent)


def storage_change(graphroot: Path, runner: Runner = run, dropin: Path = STORAGE_DROPIN) -> Optional[str]:
    """What the storage step would change, or None when podman's storage is already on the
    work disk."""
    if storage_kept(graphroot, current_graphroot(runner)):
        return None
    if dropin.is_file() and dropin.read_text() == dropin_text(graphroot):
        return f"{dropin} is written but podman reports another graphroot; check /etc/containers/storage.conf"
    return f"write {dropin}: graphroot {graphroot}"


def set_storage(graphroot: Path, runner: Runner = run, dropin: Path = STORAGE_DROPIN) -> bool:
    before = current_graphroot(runner)
    if storage_kept(graphroot, before):
        return False
    running = runner(["podman", "ps", "-q"]).stdout.split()
    if running:
        raise MediaError(f"{len(running)} containers are running in {before}; stop them (podman ps) and run "
                         "dciab media again, so container storage can move to the work disk")
    dropin_text(graphroot)  # refuse an unusable path before changing anything
    graphroot.mkdir(parents=True, exist_ok=True)
    graphroot.chmod(0o700)
    if shutil.which("selinuxenabled") and subprocess.run(["selinuxenabled"]).returncode == 0:
        # A new graphroot needs the labels of the default graphroot, /var/lib/containers/storage
        # (container-selinux labels volumes/ and artifacts/ under it, not under its parent).
        if not shutil.which("semanage"):
            raise MediaError("SELinux is enabled but semanage is missing: install policycoreutils-python-utils, "
                             "then run again")
        listed = runner(["semanage", "fcontext", "-l", "-C"]).stdout
        if f"{graphroot} = {DEFAULT_GRAPHROOT}" not in listed:
            runner(["semanage", "fcontext", "-a", "-e", DEFAULT_GRAPHROOT, str(graphroot)])
        runner(["restorecon", "-R", str(graphroot)])
    dropin.parent.mkdir(parents=True, exist_ok=True)
    tmp = dropin.with_name(dropin.name + TMP_SUFFIX)
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(dropin_text(graphroot))
    os.replace(tmp, dropin)
    after = current_graphroot(runner)
    if os.path.realpath(after) != os.path.realpath(graphroot):
        raise MediaError(f"wrote {dropin}, but podman still uses {after}: this podman doesn't read "
                         f"storage.conf.d drop-ins; set graphroot = \"{graphroot}\" in /etc/containers/storage.conf")
    return True


# ---------------------------------------------------------------- isos

HASHES = "iso-sha256.json"
OLD_HASHES = ".dciab-sha256.json"           # where #234's first version kept it, inside the served folder


def _cache_file(isos: Path) -> Path:
    """The ISO hash cache: next to <work>/isos, not in it (dciab services serves that folder)."""
    return isos.parent / ".cache" / HASHES


def _recorded(isos: Path) -> Dict[str, dict]:
    cache = _cache_file(isos)
    if cache.is_symlink() or cache.parent.is_symlink():
        return {}
    try:
        data = json.loads(cache.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _identity(st: os.stat_result) -> dict:
    """What says the file is the one hashed: size and mtime, and its device, inode and ctime,
    which utime can't set back, so a file replaced in place with the old size and mtime is
    hashed again."""
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "dev": st.st_dev, "ino": st.st_ino,
            "ctime_ns": st.st_ctime_ns}


def _record(isos: Path, name: str, path: Path, sha: str) -> None:
    """Remember an ISO's sha256 with its identity, so a rerun needn't hash 1.6 GB again."""
    data = _recorded(isos)
    data[name] = dict(_identity(path.stat()), sha256=sha)
    cache = _cache_file(isos)
    if cache.parent.is_symlink():
        raise MediaError(f"{cache.parent} is a symlink; remove it and run again")
    cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + TMP_SUFFIX)
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    # O_EXCL | O_NOFOLLOW, as copy_one writes: a planted symlink can't make root write elsewhere.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, cache)
    old = isos / OLD_HASHES
    if old.is_file() and not old.is_symlink():
        old.unlink()


def _sha_of(isos: Path, name: str, path: Path) -> str:
    """path's sha256: the recorded one when the file's identity hasn't changed, else hashed."""
    rec = _recorded(isos).get(name) or {}
    try:
        st = path.stat()
    except OSError:
        return ""
    ident = _identity(st)
    if rec.get("sha256") and all(rec.get(k) == v for k, v in ident.items()):
        return str(rec["sha256"])
    sha = verify_mod.sha256_file(path)
    _record(isos, name, path, sha)
    return sha


def place_isos(archive: Path, isos: Path, isos_from: Optional[Path] = None,
               pins: Optional[Dict[str, str]] = None, disk_usage=None,
               sources: Optional[Dict[str, Path]] = None) -> List[str]:
    """<work>/isos, holding the archive's installer ISOs (hard links into the copy), then any
    ISO bundle.lock pins that isn't there yet, copied from sources[name] (the ESXi ISO the site
    supplies, spec.hypervisors.installerFile) or isos_from, and checked against its sha256.
    A file in isos_from that bundle.lock doesn't pin is ignored."""
    isos.mkdir(parents=True, exist_ok=True)
    src_dir = archive / "payload" / "isos"
    placed: List[str] = []
    for src in sorted(src_dir.iterdir()) if src_dir.is_dir() else []:
        if not src.is_file():
            continue
        dst = isos / src.name
        if dst.exists() and os.path.samefile(src, dst):
            continue
        if dst.is_file() and verify_mod.sha256_file(dst) == verify_mod.sha256_file(src):
            continue
        if dst.exists() or dst.is_symlink():
            raise MediaError(f"{dst} exists and differs from the archive's {src.name}; rename or remove it "
                             "and run again")
        tmp = dst.with_name(dst.name + TMP_SUFFIX)
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        placed.append(src.name)
    if pins is None:
        pins = read_pins(archive)
    for name, sha in sorted(pins.items()):
        dst = isos / name
        if dst.is_symlink():
            raise MediaError(f"{dst} is a symlink; dciab media checks only real files against bundle.lock: "
                             "replace it with the file and run again")
        if dst.is_file():
            if _sha_of(isos, name, dst) == sha:
                continue
            raise MediaError(f"{dst} doesn't match the sha256 bundle.lock pins for it ({sha}); rename or "
                             "remove it and run again")
        src = (sources or {}).get(name) or (isos_from / name if isos_from else None)
        if src is None or not src.is_file():
            continue
        free = (disk_usage or shutil.disk_usage)(str(isos)).free
        need = src.stat().st_size + MARGIN_MIN
        if free < need:
            raise MediaError(f"{isos}: {free / GiB:.1f} GiB free, needs {need / GiB:.1f} GiB for {name}")
        say(f"  copying {name} from {src.parent} (checked against bundle.lock)")
        try:
            copy_one(src, dst, sha)
        except MediaError:
            raise MediaError(f"{src} doesn't match the sha256 bundle.lock pins for it ({sha}); it is damaged "
                             "or a different build: replace it and run again") from None
        dst.chmod(0o644)
        _record(isos, name, dst, sha)
        placed.append(name)
    return placed


def missing_isos(doc: dict, isos: Path, pins: Dict[str, str]) -> List[str]:
    """Pinned installer ISOs a site with spec.hypervisors still lacks in <work>/isos (pins is
    already filtered to the site's hypervisor type, site_pins)."""
    if not (doc.get("spec") or {}).get("hypervisors"):
        return []
    return [n for n in sorted(pins) if not (isos / n).is_file()]


def site_pins(doc: dict, lock_pins: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """{file name: sha256} of the installer ISOs this site needs: the one for
    spec.hypervisors.type (all of them when the type isn't given); none without hypervisors."""
    hv = (doc.get("spec") or {}).get("hypervisors")
    if not hv:
        return {}
    kind = hv.get("type") if isinstance(hv, dict) else None
    out: Dict[str, str] = {}
    for k, pin in lock_pins.items():
        if kind is None or k == kind:
            out.update(pin)
    return out


def site_sources(doc: dict, pins: Dict[str, str], config: Path) -> Dict[str, Path]:
    """The ESXi ISO the site supplies (spec.hypervisors.installerFile, relative to the config)
    as the source of its pinned name."""
    hv = (doc.get("spec") or {}).get("hypervisors") or {}
    f = hv.get("installerFile") if isinstance(hv, dict) else None
    if not f or hv.get("type") != "esxi" or len(pins) != 1:
        return {}
    path = Path(f) if os.path.isabs(f) else Path(os.path.abspath(config)).parent / f
    return {next(iter(pins)): path}


# ---------------------------------------------------------------- state

PARTIAL_SUFFIX = ".partial"


def legacy_state(config: Path) -> Path:
    """Where dciab keeps state when --state isn't given and the config isn't the appliance's
    site copy: .dciab/state next to the config (dciab_state.default_state)."""
    from dciab_state import LEGACY
    return Path(os.path.abspath(config)).parent / LEGACY


def _holds_files(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and any(path.iterdir())


# Entries dciab's steps write at the top of a state folder, compared to tell which of two
# folders looks partial. Whether a linked folder is dciab state at all is _looks_like_state's
# stricter test.
STATE_MARKERS = frozenset({
    "pki", "services", "registry", "kind", "isos", "host-isos", "installs", "answers", "backup",
    "capi-backup", "bootstrap.kubeconfig", "mgmt.kubeconfig", "mgmt-values.yaml", "hypervisors.lock",
    ".lock", "dciab-seed.lock", "proxmox", "host-isos.base-checked",
})


# dciab services' own files in <state>/services (dciab_services.lifecycle RECORD and APPLIED).
SERVICES_RECORD, SERVICES_APPLIED = "created", "applied"


def _markers(path: Path) -> frozenset:
    try:
        return frozenset(e.name for e in os.scandir(path)) & STATE_MARKERS
    except OSError:
        return frozenset()


# The kinds dciab_services.lifecycle records, one per line ("<kind> <fields...>").
SERVICES_KINDS = frozenset({"unit", "pod", "restore", "address", "port"})
STATE_MARKER_TEXT = ("pki/ca.crt, a dciab kubeconfig (bootstrap.kubeconfig or mgmt.kubeconfig), an "
                     "installs/<host>.json record, or dciab services' record in services/")


def _services_record(path: Path) -> bool:
    """services/created as dciab services writes it: every non-empty line starts with a kind
    lifecycle records, and there is at least one."""
    try:
        if not path.is_file() or path.stat().st_size > 1 << 20:
            return False
        lines = [l.split() for l in path.read_text().splitlines() if l.strip()]
    except (OSError, UnicodeDecodeError):
        return False
    return bool(lines) and all(l[0] in SERVICES_KINDS for l in lines)


def _services_applied(path: Path) -> bool:
    """services/applied as dciab services writes it: a single sha256 (render.fingerprint)."""
    try:
        if not path.is_file() or path.stat().st_size > 256:
            return False
        return re.fullmatch(r"[0-9a-f]{64}", path.read_text().strip()) is not None
    except (OSError, UnicodeDecodeError):
        return False


def _looks_like_state(path: Path) -> bool:
    """Whether path holds something only dciab writes: the seed's CA (pki/ca.crt), a dciab
    kubeconfig, a hypervisor install record (installs/<host>.json), or dciab services' record
    of what it added to the host (services/created with only lifecycle record lines, or
    services/applied holding its sha256 fingerprint; a state folder holds these before seed or
    hypervisors ever ran). Generic names such as
    backup/ or registry/ are not enough on their own: a link to unrelated data must never be
    moved (or, across filesystems, deleted)."""
    try:
        if (path / "pki" / "ca.crt").is_file():
            return True
        if any((path / n).is_file() for n in ("bootstrap.kubeconfig", "mgmt.kubeconfig")):
            return True
        if _services_record(path / "services" / SERVICES_RECORD) or \
                _services_applied(path / "services" / SERVICES_APPLIED):
            return True
        installs = path / "installs"
        return installs.is_dir() and any(p.suffix == ".json" and p.is_file() for p in installs.iterdir())
    except OSError:
        return False


def _moved_aside(source: Path) -> List[Path]:
    """Folders an interrupted cross-filesystem move renamed aside (<source>.moved-<time>)."""
    try:
        return sorted(p for p in source.parent.iterdir() if p.name.startswith(source.name + ".moved-"))
    except OSError:
        return []


def _same_tree(a: Path, b: Path) -> Optional[bool]:
    """Whether two folders hold the same files with the same contents (links not followed).
    Names and sizes are compared first; contents are then compared file by file, streamed
    (filecmp, not read into memory) and stopping at the first difference, since state can
    hold the seed registry's image data. None when it can't tell (a read error)."""
    def listing(root: Path) -> Dict[str, int]:
        out = {}
        for dirpath, dirs, files in os.walk(root):
            for name in files:
                p = Path(dirpath) / name
                out[str(p.relative_to(root))] = p.lstat().st_size
        return out
    try:
        la, lb = listing(a), listing(b)
        if la != lb:
            return False
        for rel in la:
            fa, fb = a / rel, b / rel
            if fa.is_symlink() or fb.is_symlink():
                if not (fa.is_symlink() and fb.is_symlink() and os.readlink(fa) == os.readlink(fb)):
                    return False
                continue
            if not filecmp.cmp(fa, fb, shallow=False):
                return False
        return True
    except (OSError, MemoryError):
        return None


def _both_detail(source: Path, new: Path, compare: bool = False) -> str:
    """The refusal when both folders hold state, saying which one looks partial, or (with
    compare, only when the refusal is actually raised) that the two are identical: a move
    interrupted after its copy was complete."""
    a, b = _markers(source), _markers(new)
    hint = ""
    same = _same_tree(source, new) if compare and a == b else False
    if same:
        hint = (f" The two hold identical files: an interrupted move left a complete copy in {new}. "
                f"Move {source} aside (or remove it) and run again.")
    elif same is None:
        hint = f" (Couldn't compare the two folders' contents to tell whether they're identical.)"
    elif a > b:
        hint = f" {new} lacks {', '.join(sorted(a - b))}, which {source} has: it looks like the partial one."
    elif b > a:
        hint = f" {source} lacks {', '.join(sorted(b - a))}, which {new} has: it looks like the partial one."
    aside = _moved_aside(source)
    if aside:
        hint += f" An interrupted move left {', '.join(map(str, aside))} (the source renamed aside): its copy in {new} is complete."
    return (f"{source} and {new} both hold dciab state (CA, sealed passwords, kubeconfigs). dciab media "
            f"won't merge them: keep the right one, move the other aside, and run again.{hint}")


class StatePlan(NamedTuple):
    """What the state step does: action is None (nothing), 'refuse', 'move' or 'link'.
    source is the folder that holds the state to move: .dciab/state itself, or where a link
    there points (an earlier run's work disk, or the operator's own link)."""
    action: Optional[str]
    source: Optional[Path]
    detail: str


def state_plan(config: Path, work: Path) -> StatePlan:
    old, new = legacy_state(config), work / "state"
    partial = new.with_name(new.name + PARTIAL_SUFFIX)
    new_real = os.path.realpath(new)
    if old.is_symlink():
        target = Path(os.path.realpath(old))
        if str(target) == new_real:
            return StatePlan(None, None, f"{old} links to {new}")
        if not _holds_files(target):
            if _holds_files(new):
                return StatePlan("link", None, f"relink {old} (to {target}, which holds no state) to {new}")
            return StatePlan(None, None, f"{old} links to {target}, which holds no state")
        if os.path.ismount(target):
            return StatePlan("refuse", target, f"{old} links to {target}, a mount point: dciab media won't move "
                                               f"a mount. Copy its state to {new} yourself, or pass --state "
                                               f"{target} to every step")
        if not _looks_like_state(target):
            return StatePlan("refuse", target, f"{old} links to {target}, which doesn't look like dciab state "
                                               f"(none of {STATE_MARKER_TEXT}): "
                                               "dciab media won't move it. Remove or repoint the link and run again")
        source = target
    elif os.path.lexists(old):
        if os.path.realpath(old) == new_real:
            return StatePlan(None, None, f"{old} holds no state")
        if not _holds_files(old):
            if old.is_dir() and not old.is_symlink() and _holds_files(new):
                return StatePlan("link", None, f"replace the empty folder {old} with a link to {new}")
            return StatePlan(None, None, f"{old} holds no state")
        if os.path.ismount(old):
            return StatePlan("refuse", old, f"{old} is a mount point: dciab media won't move a mount. Copy "
                                            f"its state to {new} yourself, or pass --state {old} to every step")
        source = old
    else:
        if _holds_files(new):
            return StatePlan("link", None, f"link {old} to {new} (the link was missing)")
        return StatePlan(None, None, "no state to move")
    if os.path.lexists(partial):
        return StatePlan("refuse", source, f"{partial} is a partial copy left by an interrupted run (the state "
                                           f"in {source} is complete): remove {partial} and run again")
    if _holds_files(new):
        return StatePlan("refuse", source, _both_detail(source, new))
    via = f" (where {old} links)" if source != old else ""
    return StatePlan("move", source, f"move {source}{via} to {new}, and link {old} to it")


def state_change(config: Path, work: Path) -> Optional[str]:
    """What the state step would do, or None when there's nothing to do."""
    plan = state_plan(config, work)
    if plan.action is None:
        return None
    return ("refuse: " if plan.action == "refuse" else "") + plan.detail


def tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def state_bytes(config: Path, work: Path) -> int:
    """Bytes the state step copies onto the work disk: the state's size when it moves across
    filesystems (a rename on the same filesystem needs no room), else 0."""
    plan = state_plan(config, work)
    if plan.action != "move" or plan.source is None:
        return 0
    try:
        if os.stat(plan.source).st_dev == os.stat(nearest_existing(work)).st_dev:
            return 0
    except OSError:
        return 0
    return tree_bytes(plan.source)


def preload_bytes(plan: "Plan", work: Path, runner: Optional[Runner] = None) -> int:
    """Room the preload step needs on the work disk: the Day 0 images root's container storage
    doesn't hold yet. The storage step, which runs first, always leaves that storage on the
    work disk's filesystem (it's there already, or it moves to <work>/containers), so they're
    always counted; an image already loaded isn't, so a rerun with nothing to load needs no
    room. Read from the source archive's payload, since the copy on the work disk doesn't
    exist yet at the target step."""
    payload = plan.source / "payload"
    if not (payload / "images.yaml").is_file():
        return 0
    try:
        refs = preload.lock_refs(payload)
    except preload.PreloadError:
        return 0
    try:
        todo = {k: ref for k, ref in refs.items() if not preload._exists(ref, runner or run)}
    except (OSError, FileNotFoundError):
        todo = dict(refs)                         # no podman to ask: count every image
    if not todo:
        return 0
    try:
        return preload.image_bytes(payload, todo)
    except (OSError, preload.PreloadError):
        return 0


def _fsync_tree(path: Path) -> None:
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            if os.path.islink(fp):
                continue
            fd = os.open(fp, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _move_tree(source: Path, new: Path) -> Optional[Path]:
    """Move source to new. On the same filesystem a rename (atomic); across filesystems a copy
    to new.partial, synced, renamed into place, and only then the source removed, so an
    interruption leaves the source complete and at most a .partial the next run names."""
    try:
        os.rename(source, new)
        return None
    except OSError as exc:
        import errno
        if exc.errno != errno.EXDEV:
            raise
    partial = new.with_name(new.name + PARTIAL_SUFFIX)
    shutil.copytree(source, partial, symlinks=True, copy_function=shutil.copy2)
    _fsync_tree(partial)
    os.rename(partial, new)
    fd = os.open(new.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # Rename the source aside, so an interrupted or failed removal leaves a folder whose name
    # says it was moved, not a half-deleted state folder under its old name. The caller removes
    # it after the link is in place.
    import time
    aside = source.with_name(f"{source.name}.moved-{time.strftime('%Y%m%dT%H%M%S')}")
    os.rename(source, aside)
    return aside


def _link(old: Path, new: Path) -> Optional[str]:
    """Point .dciab/state at <work>/state. Returns a note when the link can't be made."""
    try:
        old.parent.mkdir(parents=True, exist_ok=True)
        if old.is_symlink():
            old.unlink()
        elif old.is_dir():
            old.rmdir()                           # empty: state_plan only links over an empty folder
        os.symlink(str(new), str(old))
        return None
    except OSError as exc:
        return (f"couldn't link {old} to {new} ({exc.strerror or exc}): every dciab step needs "
                f"--state {new}")


# Locks the other steps hold while they run: dciab seed (/run/lock, or the state folder when
# /run/lock isn't writable), dciab services, dciab hypervisors and dciab mgmt (in the state).
HOST_STEP_LOCKS = (Path("/run/lock/dciab-seed.lock"), Path("/run/dciab-services.lock"))
STATE_STEP_LOCKS = ("dciab-seed.lock", "hypervisors.lock", ".lock")


class step_locks:
    """Take every other step's lock without waiting, and hold them while the state moves, so no
    step writes into the state mid-move. Refuses, naming the lock, when one is held."""

    def __init__(self, state: Path, host_locks: Optional[Sequence[Path]] = None):
        host_locks = HOST_STEP_LOCKS if host_locks is None else host_locks
        # Host locks are made if missing, so a step starting now is refused. Locks inside the state
        # folder are only opened if they already exist: creating them would add files to the folder
        # being moved (or, on a refusal, to a folder that may not be dciab's).
        self.paths = [(p, True) for p in host_locks if p.parent.is_dir()] + \
                     [(state / n, False) for n in STATE_STEP_LOCKS]
        self.fds: List[int] = []

    def __enter__(self):
        try:
            for path, create in self.paths:
                flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
                try:
                    fd = os.open(path, flags, 0o600)
                except OSError:
                    continue                      # missing, or not ours to open: nobody holds it here
                self.fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MediaError(f"another dciab step holds {path}; let it finish, then run dciab media "
                                     "again, so the state doesn't move while it writes there") from None
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc):
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        return False


def move_state(config: Path, work: Path, runner: Optional[Runner] = None) -> Optional[str]:
    """Put the state an earlier step left next to the config (or where a link there points) in
    <work>/state, and leave .dciab/state linking to it. Returns what was done, or None."""
    old, new = legacy_state(config), work / "state"
    plan = state_plan(config, work)
    if plan.action is None:
        return None
    if plan.action == "refuse":
        if plan.source is not None and plan.detail == _both_detail(plan.source, new):
            raise MediaError(_both_detail(plan.source, new, compare=True))
        raise MediaError(plan.detail)
    if plan.action == "link":
        note = _link(old, new)
        return note or f"linked {old} to {new}"
    try:
        running = (runner or run)(["podman", "ps", "-q"]).stdout.split()
    except FileNotFoundError:
        running = []                              # no podman: nothing can be running
    if running:
        raise MediaError(f"{len(running)} containers are running and may use {plan.source}; stop them "
                         "(podman ps) and run dciab media again, so the state can move to the work disk")
    assert plan.source is not None
    with step_locks(plan.source):
        new.parent.mkdir(parents=True, exist_ok=True)
        if new.is_dir() and not new.is_symlink():
            new.rmdir()                           # empty, checked by state_plan
        elif os.path.lexists(new):
            raise MediaError(f"{new} exists and isn't a folder; remove it and run again")
        aside = _move_tree(plan.source, new)
        new.chmod(0o700)
        note = _link(old, new)
        if aside is not None:
            try:
                shutil.rmtree(aside)
            except OSError as exc:
                raise MediaError(f"moved the state to {new}, but couldn't remove the old copy {aside} "
                                 f"({exc.strerror or exc}); remove it by hand") from None
    moved = f"moved {plan.source} to {new}"
    return f"{moved}; {note}" if note else f"{moved} (left a link at {old})"


# ---------------------------------------------------------------- installer ISOs

def pins_by_type(lock: dict) -> Dict[str, Dict[str, str]]:
    """{hypervisor type: {file name: sha256}} for each installer ISO bundle.lock pins."""
    from urllib.parse import urlsplit
    out: Dict[str, Dict[str, str]] = {}
    for kind, h in (lock.get("hypervisors") or {}).items():
        if not isinstance(h, dict) or not h.get("isoSHA256"):
            continue
        name = os.path.basename(urlsplit(str(h.get("isoURL") or "")).path) or str(h.get("isoFile") or "")
        if name in ("", ".", "..") or "/" in name or "\\" in name:
            continue
        out[str(kind)] = {name: str(h["isoSHA256"]).lower()}
    return out


def pinned_isos(lock: dict) -> Dict[str, str]:
    """{file name: sha256} for each installer ISO bundle.lock pins (hypervisors.*)."""
    pins: Dict[str, str] = {}
    for pin in pins_by_type(lock).values():
        pins.update(pin)
    return pins


def read_lock_pins(archive: Path) -> Dict[str, Dict[str, str]]:
    try:
        return pins_by_type(yaml.safe_load((archive / "bundle.lock").read_text()) or {})
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        raise MediaError(f"{archive / 'bundle.lock'}: {exc}") from None


def read_pins(archive: Path) -> Dict[str, str]:
    pins: Dict[str, str] = {}
    for pin in read_lock_pins(archive).values():
        pins.update(pin)
    return pins


# ---------------------------------------------------------------- main

def stable_config(config: str, site: Optional[Path] = None) -> Path:
    """The config path to print: absolute, symlinks kept. On the appliance, `site` links to a
    versioned folder that the next boot's sync_site replaces, so a config inside it (named
    either way) is given by the `site` path, which survives the sync."""
    path = Path(os.path.abspath(config))
    site = site or SITE
    if site.is_symlink():
        try:
            return site / path.resolve().relative_to(site.resolve())
        except (OSError, ValueError):
            pass
    return path


def next_commands(dest: Path, config: Path, work: Path) -> List[str]:
    q = shlex.quote
    dciab, state, isos = q(str(dest / "dciab")), q(str(work / "state")), q(str(work / "isos"))
    # The isos step put the copy's installers in <work>/isos. Without --isos, services and
    # hypervisors would look in <state>/isos when workDisk is disk: (the appliance).
    return [
        f"{dciab} services {q(str(config))} --state {state} --isos {isos}   # sites with spec.hypervisors",
        f"{dciab} hypervisors {q(str(config))} --state {state} --isos {isos}   # sites with spec.hypervisors",
        f"{dciab} seed {q(str(config))} --state {state}",
    ]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dciab media",
                                description="Copy the archive to the work disk and put the heavy state there.")
    p.add_argument("config")
    p.add_argument("--root", required=True, help="the unpacked archive (set by the dciab entrypoint)")
    p.add_argument("--source", help="the archive to copy (default: the one this dciab runs from)")
    key = p.add_mutually_exclusive_group(required=True)
    key.add_argument("--key", type=Path, help="the cosign public key the archive was signed with")
    key.add_argument("--checksums-only", action="store_true",
                     help="skip the signature: only compare the copy with MANIFEST (unsigned builds)")
    p.add_argument("--cosign", help="the cosign binary (default: cosign on PATH, else the copy's payload/bin/cosign)")
    p.add_argument("--isos-from", type=Path,
                   help=f"where the pinned installer ISOs are when the archive doesn't carry them "
                        f"(default: the stick's {USB_MOUNT / 'isos'})")
    p.add_argument("--until", choices=STEPS, help="stop after this step")
    p.add_argument("--print", dest="print_only", action="store_true", help="show the plan; change nothing")
    args = p.parse_args(argv)

    if not args.print_only and os.geteuid() != 0:
        print("dciab media: run as root (it writes the work disk and /etc/containers); --print needs no root",
              file=sys.stderr)
        return 2
    if args.key is not None and not args.key.is_file():
        print(f"dciab media: {args.key}: no such key file", file=sys.stderr)
        return 2
    config = stable_config(args.config)
    try:
        doc = yaml.safe_load(config.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"dciab media: {config}: {exc}", file=sys.stderr)
        return 2
    source = Path(args.source or args.root).resolve()
    if not args.source and not (source / verify_mod.MANIFEST).is_file():
        # The appliance's own dciab (/usr/share/dciab) isn't a packaged archive: use the stick's.
        try:
            stick = USB_MOUNT / f"dciab-{archive_version(source)}"
        except MediaError:
            stick = None
        if stick is not None and (stick / verify_mod.MANIFEST).is_file():
            source = stick

    try:
        work = resolve_target(doc, read_mounts())
        plan = make_plan(source, work)
        graphroot = work / "containers"
        if args.print_only:
            try:
                if os.geteuid() != 0:
                    raise OSError("not root")
                change = storage_change(graphroot)
            except (OSError, subprocess.CalledProcessError):
                change = f"root's podman graphroot not read (run --print as root); would write {STORAGE_DROPIN}: graphroot {graphroot}"
            near = nearest_existing(work)
            free = shutil.disk_usage(str(near)).free
            say(f"source:  {plan.source} ({plan.total_bytes / GiB:.2f} GiB)")
            say(f"target:  {plan.dest} ({free / GiB:.1f} GiB free on {work})")
            say(f"state:   {state_change(config, work) or 'unchanged: ' + str(work / 'state')}")
            say(f"copy:    {len(plan.to_copy)} of {len(plan.files)} files, {plan.copy_bytes} bytes")
            say(f"storage: {change or 'unchanged: root container storage is already on the work disk'}")
            say(f"isos:    {work / 'isos'}")
            payload = plan.source / "payload"
            say(f"preload: {'the Day 0 images from ' + str(plan.dest / 'payload') if (payload / 'images.yaml').is_file() else 'none (the archive carries no images)'}")
            return 0
        work.mkdir(parents=True, exist_ok=True)
        lock_path = HOST_LOCK if HOST_LOCK.parent.is_dir() and os.access(HOST_LOCK.parent, os.W_OK) \
            else work / ".dciab-media.lock"
        # One run per host: the storage drop-in is host-wide.
        with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise MediaError(f"another dciab media is running ({lock_path})") from None
            for step in STEPS:
                say(f"==> {step}")
                if step == "target":
                    extra = state_bytes(config, work) + preload_bytes(plan, work)
                    check_space(plan, work, extra=extra)
                    say(f"  {work}: {plan.copy_bytes + extra} bytes to copy (the archive, any state to move, and the "
                        "Day 0 images to load)")
                elif step == "state":
                    moved = move_state(config, work)
                    say(f"  {moved}" if moved else f"  unchanged ({work / 'state'})")
                elif step == "copy":
                    n = do_copy(plan)
                    say(f"  copied {n} files" if n else "  unchanged")
                elif step == "verify":
                    cosign = args.cosign or verify_mod.default_cosign(plan.dest)
                    do_verify(plan.dest, args.key, cosign)
                    say("  ok: signature and checksums match" if args.key else
                        "  ok: checksums match (signature not checked; pass --key)")
                elif step == "storage":
                    say(f"  {STORAGE_DROPIN}: graphroot {graphroot}" if set_storage(graphroot)
                        else "  unchanged (root's container storage is already on the work disk)")
                elif step == "isos":
                    isos_from = args.isos_from or (USB_MOUNT / "isos")
                    pins = site_pins(doc, read_lock_pins(plan.dest))
                    placed = place_isos(plan.dest, work / "isos", isos_from, pins,
                                        sources=site_sources(doc, pins, config))
                    say(f"  placed {', '.join(placed)}" if placed else f"  unchanged ({work / 'isos'})")
                    for name in missing_isos(doc, work / "isos", pins):
                        say(f"  note: {name} (pinned in bundle.lock for this site's hypervisors) isn't in "
                            f"{work / 'isos'} or {isos_from}: put it in either, then run again; dciab "
                            f"hypervisors needs it")
                elif step == "preload":
                    payload = plan.dest / "payload"
                    if (payload / "images.yaml").is_file():
                        try:
                            loaded, present, missing = preload.load(payload, preload.lock_refs(payload))
                        except preload.PreloadError as exc:
                            raise MediaError(str(exc)) from None
                        say(f"  loaded {len(loaded)}, {len(present)} already there (from {payload})")
                        for ref in missing:
                            say(f"  warning: the payload lacks {ref}; dciab services and seed stop until the "
                                f"archive is rebuilt with it (build/archive.py images)")
                    else:
                        say("  none (the archive carries no images)")
                if step == args.until:
                    say(f"stopped after {step} (--until)")
                    return 0
    except MediaError as exc:
        print(f"dciab media: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"dciab media: {exc} {detail.strip()}".rstrip(), file=sys.stderr)
        return 1
    say("Next, from the copy on the work disk:")
    for line in next_commands(plan.dest, config, work):
        say(f"  {line}")
    return 0
