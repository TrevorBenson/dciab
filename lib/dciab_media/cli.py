"""dciab media: copy the archive to the work disk, and put the bootstrap's heavy state there.

    dciab media <config> (--key PUB | --checksums-only) [--source DIR] [--cosign BIN]
                [--until STEP] [--print]

Run as root on the bootstrap host (--print excepted). Steps, in order; each is safe to
re-run, and a finished run changes nothing:
  target   the work directory from spec.bootstrap.workDisk:
             path: that directory (it must exist, on a local filesystem)
             disk: WORK_DIR_APPLIANCE on the appliance, which must sit on the disk
                   matching the rootDeviceHints (the appliance installed its root there)
           and enough free space for what is left to copy, plus a margin
  copy     the archive (--source; default the one this dciab runs from, or on the
           appliance the stick's USB_MOUNT/dciab-<VERSION>) to
           <work>/dciab-<VERSION>/: only the files MANIFEST lists, plus MANIFEST and its
           signature; a file already there with the right size and sha256 is skipped;
           others are written to a temporary name, checked, then renamed into place
  verify   dciab verify-archive on the copy: the signature with --key, then checksums
  storage  podman's storage for root (graphroot) moved to <work>/containers with a
           drop-in in /etc/containers/storage.conf.d, unless it's there already;
           refused while containers are running
  isos     <work>/isos, with the archive's installer ISOs (payload/isos/) if it has any

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
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

import yaml

from dciab_archive import verify as verify_mod
from dciab_state import SITE

STEPS = ["target", "copy", "verify", "storage", "isos"]
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


def check_space(plan: Plan, work: Path, disk_usage=None) -> int:
    """Free bytes needed: what is left to copy plus a margin. Raises if there's less."""
    need = plan.copy_bytes + max(MARGIN_MIN, int(plan.total_bytes * MARGIN_FRACTION)) if plan.copy_bytes else 0
    free = (disk_usage or shutil.disk_usage)(str(work)).free
    if free < need:
        raise MediaError(f"{work}: {free / GiB:.1f} GiB free, needs {need / GiB:.1f} GiB "
                         f"({plan.copy_bytes / GiB:.1f} GiB to copy plus a margin)")
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

def dropin_text(graphroot: Path) -> str:
    if any(c in str(graphroot) for c in '"\\') or any(ord(c) < 0x20 or ord(c) == 0x7f for c in str(graphroot)):
        raise MediaError(f"{graphroot!r}: the work directory's path can't contain quotes, backslashes or "
                         "control characters")
    return ("# Written by dciab media: root's container storage on the work disk.\n"
            f"[storage]\ngraphroot = \"{graphroot}\"\n")


def current_graphroot(runner: Runner = run) -> str:
    return runner(["podman", "info", "--format", "{{.Store.GraphRoot}}"]).stdout.strip()


def storage_change(graphroot: Path, runner: Runner = run, dropin: Path = STORAGE_DROPIN) -> Optional[str]:
    """What the storage step would change, or None when podman already uses graphroot."""
    if os.path.realpath(current_graphroot(runner)) == os.path.realpath(graphroot):
        return None
    if dropin.is_file() and dropin.read_text() == dropin_text(graphroot):
        return f"{dropin} is written but podman reports another graphroot; check /etc/containers/storage.conf"
    return f"write {dropin}: graphroot {graphroot}"


def set_storage(graphroot: Path, runner: Runner = run, dropin: Path = STORAGE_DROPIN) -> bool:
    before = current_graphroot(runner)
    if os.path.realpath(before) == os.path.realpath(graphroot):
        return False
    running = runner(["podman", "ps", "-q"]).stdout.split()
    if running:
        raise MediaError(f"{len(running)} containers are running in {before}; stop them (podman ps) and run "
                         "dciab media again, so container storage can move to the work disk")
    dropin_text(graphroot)  # refuse an unusable path before changing anything
    graphroot.mkdir(parents=True, exist_ok=True)
    graphroot.chmod(0o700)
    if shutil.which("selinuxenabled") and subprocess.run(["selinuxenabled"]).returncode == 0:
        # A new graphroot needs the same SELinux labels as /var/lib/containers.
        if not shutil.which("semanage"):
            raise MediaError("SELinux is enabled but semanage is missing: install policycoreutils-python-utils, "
                             "then run again")
        listed = runner(["semanage", "fcontext", "-l", "-C"]).stdout
        if f"{graphroot} = /var/lib/containers" not in listed:
            runner(["semanage", "fcontext", "-a", "-e", "/var/lib/containers", str(graphroot)])
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

def place_isos(archive: Path, isos: Path) -> List[str]:
    """<work>/isos, holding the archive's installer ISOs (hard links into the copy)."""
    isos.mkdir(parents=True, exist_ok=True)
    src_dir = archive / "payload" / "isos"
    placed: List[str] = []
    if not src_dir.is_dir():
        return placed
    for src in sorted(src_dir.iterdir()):
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
    return placed


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
            say(f"copy:    {len(plan.to_copy)} of {len(plan.files)} files, {plan.copy_bytes} bytes")
            say(f"storage: {change or 'unchanged: podman already uses ' + str(graphroot)}")
            say(f"isos:    {work / 'isos'}")
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
                    check_space(plan, work)
                    say(f"  {work}: {plan.copy_bytes} bytes to copy")
                elif step == "copy":
                    n = do_copy(plan)
                    say(f"  copied {n} files" if n else "  unchanged")
                elif step == "verify":
                    cosign = args.cosign or verify_mod.default_cosign(plan.dest)
                    do_verify(plan.dest, args.key, cosign)
                    say("  ok: signature and checksums match" if args.key else
                        "  ok: checksums match (signature not checked; pass --key)")
                elif step == "storage":
                    say(f"  {STORAGE_DROPIN}: graphroot {graphroot}" if set_storage(graphroot) else "  unchanged")
                elif step == "isos":
                    placed = place_isos(plan.dest, work / "isos")
                    say(f"  placed {', '.join(placed)}" if placed else f"  unchanged ({work / 'isos'})")
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
