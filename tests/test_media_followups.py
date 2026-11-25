"""dciab media follow-ups (#165): the state step, and pinned installer ISOs from the stick."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from dciab_archive import verify as verify_mod  # noqa: E402
from dciab_media import cli  # noqa: E402
from test_media import Usage, args, make_archive, write_config  # noqa: E402

ISO = b"PVE-ISO" * 1000


def _lock(src: Path, sha: str, name="proxmox-ve_9.2-1.iso") -> None:
    """Give the archive a bundle.lock pinning one ISO (and re-sign the MANIFEST)."""
    (src / "bundle.lock").write_text(yaml.safe_dump({"hypervisors": {
        "proxmox": {"isoURL": f"https://enterprise.proxmox.com/iso/{name}", "isoSHA256": sha, "shipped": True},
        "esxi": {"isoFile": "", "isoSHA256": ""}}}))
    (src / "payload" / "isos" / "installer.iso").unlink()
    files = [p for p in src.rglob("*") if p.is_file() and p.name != "MANIFEST"]
    sums = {str(p.relative_to(src)): verify_mod.sha256_file(p) for p in files}
    (src / "MANIFEST").write_text(verify_mod.manifest_text(sums))


def _runner(running=""):
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, running, "")
    runner.calls = calls
    return runner


@pytest.fixture
def site(tmp_path, monkeypatch):
    src = make_archive(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "dciab-media.lock")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    monkeypatch.setattr(cli, "run", _runner())
    monkeypatch.setattr(cli, "USB_MOUNT", tmp_path / "stick")
    locks = tmp_path / "run-lock"
    locks.mkdir()
    monkeypatch.setattr(cli, "HOST_STEP_LOCKS", (locks / "dciab-seed.lock", locks / "dciab-services.lock"))
    return src, work, cfg


def _hv_config(cfg: Path, work: Path, **hv) -> None:
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"workDisk": {"path": str(work)}},
                                            "hypervisors": {"type": "proxmox", "hosts": [], **hv}}}))


# --- the state step -----------------------------------------------------------------------------

def _old_state(cfg: Path) -> Path:
    old = cfg.parent / ".dciab" / "state"
    (old / "pki").mkdir(parents=True)
    (old / "pki" / "ca.crt").write_text("CA")
    return old


def test_state_next_to_the_config_moves_to_the_work_disk(site, capsys):
    src, work, cfg = site
    old = _old_state(cfg)
    assert cli.main(args(src, cfg)) == 0
    assert (work / "state" / "pki" / "ca.crt").read_text() == "CA"
    assert old.is_symlink() and old.resolve() == (work / "state").resolve()
    assert "moved" in capsys.readouterr().out
    # A command run without --state still finds it, and a rerun changes nothing.
    from dciab_state import default_state
    assert default_state(cfg, site=cfg.parent / "no-site").resolve() == (work / "state").resolve()
    assert cli.main(args(src, cfg)) == 0
    assert "unchanged (" in capsys.readouterr().out.split("==> state")[1].split("==>")[0]


def test_state_into_an_empty_work_state(site):
    src, work, cfg = site
    _old_state(cfg)
    (work / "state").mkdir()
    assert cli.main(args(src, cfg)) == 0
    assert (work / "state" / "pki" / "ca.crt").is_file()


def test_both_states_refused(site, capsys):
    src, work, cfg = site
    old = _old_state(cfg)
    (work / "state").mkdir()
    (work / "state" / "other").write_text("x")
    assert cli.main(args(src, cfg)) == 1
    assert "both hold dciab state" in capsys.readouterr().err
    assert (old / "pki" / "ca.crt").is_file() and not old.is_symlink()


def test_state_not_moved_while_containers_run(site, monkeypatch, capsys):
    src, work, cfg = site
    old = _old_state(cfg)
    monkeypatch.setattr(cli, "run", _runner("abc123\n"))
    assert cli.main(args(src, cfg)) == 1
    assert "containers are running" in capsys.readouterr().err
    assert (old / "pki" / "ca.crt").is_file() and not (work / "state").exists()


def test_print_shows_the_state_move(site, capsys):
    src, work, cfg = site
    old = _old_state(cfg)
    assert cli.main(args(src, cfg, "--print")) == 0
    assert f"state:   move {old}" in capsys.readouterr().out
    assert not old.is_symlink()


def test_nothing_to_move():
    assert cli.state_change(Path("/nonexistent/dciab.yaml"), Path("/nonexistent/work")) is None


# --- pinned installer ISOs ---------------------------------------------------------------------

def test_pins_from_bundle_lock():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    pins = cli.pinned_isos(lock)
    assert pins == {"proxmox-ve_9.2-1.iso": lock["hypervisors"]["proxmox"]["isoSHA256"]}
    assert cli.pinned_isos({"hypervisors": {"esxi": {"isoFile": "VMware-ESXi-9.iso", "isoSHA256": "AB"}}}) == \
        {"VMware-ESXi-9.iso": "ab"}
    assert cli.pinned_isos({}) == {}


def _stick_iso(tmp_path, data=ISO, name="proxmox-ve_9.2-1.iso"):
    d = tmp_path / "stick" / "isos"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    (d / "unpinned.iso").write_bytes(b"nope")
    return d


def test_pinned_iso_copied_from_the_stick(site, tmp_path, capsys):
    src, work, cfg = site
    _hv_config(cfg, work)
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    _stick_iso(tmp_path)
    assert cli.main(args(src, cfg)) == 0
    assert (work / "isos" / "proxmox-ve_9.2-1.iso").read_bytes() == ISO
    assert not (work / "isos" / "unpinned.iso").exists()            # only what bundle.lock pins
    assert "placed proxmox-ve_9.2-1.iso" in capsys.readouterr().out
    assert cli.main(args(src, cfg)) == 0                             # a rerun copies nothing
    assert "unchanged (" in capsys.readouterr().out.split("==> isos")[1]


def test_a_damaged_iso_on_the_stick_is_refused(site, tmp_path, capsys):
    src, work, cfg = site
    _hv_config(cfg, work)
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    _stick_iso(tmp_path, data=ISO[:-1] + b"X")
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't match the sha256 bundle.lock pins" in capsys.readouterr().err
    assert not (work / "isos" / "proxmox-ve_9.2-1.iso").exists()
    assert not list((work / "isos").glob("*" + cli.TMP_SUFFIX))


def test_a_wrong_iso_already_in_work_isos_is_refused(site, tmp_path, capsys):
    src, work, cfg = site
    _hv_config(cfg, work)
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    (work / "isos").mkdir()
    (work / "isos" / "proxmox-ve_9.2-1.iso").write_bytes(b"old build")
    assert cli.main(args(src, cfg)) == 1
    assert "rename or remove it" in capsys.readouterr().err


def test_isos_from_overrides_the_stick(site, tmp_path):
    src, work, cfg = site
    _hv_config(cfg, work)
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    other = tmp_path / "downloads"
    other.mkdir()
    (other / "proxmox-ve_9.2-1.iso").write_bytes(ISO)
    assert cli.main(args(src, cfg, "--isos-from", str(other))) == 0
    assert (work / "isos" / "proxmox-ve_9.2-1.iso").is_file()


def test_missing_iso_is_a_note_for_hypervisor_sites(site, capsys):
    src, work, cfg = site
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    _hv_config(cfg, work)
    assert cli.main(args(src, cfg)) == 0
    assert "note: proxmox-ve_9.2-1.iso (pinned in bundle.lock for this site's hypervisors) isn't in" \
        in capsys.readouterr().out


def test_no_note_without_hypervisors(site, capsys):
    src, work, cfg = site
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    assert cli.main(args(src, cfg)) == 0
    assert "note:" not in capsys.readouterr().out


def test_not_enough_room_for_an_iso(tmp_path):
    archive, isos = tmp_path / "a", tmp_path / "isos"
    archive.mkdir()
    stick = _stick_iso(tmp_path)
    pins = {"proxmox-ve_9.2-1.iso": verify_mod.hashlib.sha256(ISO).hexdigest()}
    with pytest.raises(cli.MediaError, match="GiB free"):
        cli.place_isos(archive, isos, stick, pins, disk_usage=lambda p: Usage(10, 10, 0))


def test_bad_bundle_lock(tmp_path):
    (tmp_path / "bundle.lock").write_text("hypervisors: [unclosed\n")
    with pytest.raises(cli.MediaError, match="bundle.lock"):
        cli.read_pins(tmp_path)
    assert cli.read_pins(tmp_path / "absent") == {}


# --- review fixes: the state step (F1-F3) -------------------------------------------------------

def test_a_changed_work_disk_moves_the_state_from_the_old_one(site, tmp_path, capsys):
    """workDisk A then B: the link left on A must not hide A's state (F1)."""
    src, work, cfg = site
    old = _old_state(cfg)
    assert cli.main(args(src, cfg)) == 0                              # state now on A, link left
    b = tmp_path / "work-b"
    b.mkdir()
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"workDisk": {"path": str(b)}}}}))
    capsys.readouterr()
    assert cli.main(args(src, cfg, "--until", "state")) == 0
    out = capsys.readouterr().out
    assert f"moved {work / 'state'} to {b / 'state'}" in out
    assert (b / "state" / "pki" / "ca.crt").read_text() == "CA"
    assert not (work / "state").exists()
    assert old.is_symlink() and old.resolve() == (b / "state").resolve()


def test_an_operator_link_to_state_elsewhere_is_moved_not_ignored(site, tmp_path):
    src, work, cfg = site
    elsewhere = tmp_path / "my-state"
    (elsewhere / "pki").mkdir(parents=True)
    (elsewhere / "pki" / "ca.crt").write_text("MINE")
    legacy = cfg.parent / ".dciab" / "state"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(elsewhere)
    assert "move " + str(elsewhere) in cli.state_change(cfg, work)
    assert cli.main(args(src, cfg)) == 0
    assert (work / "state" / "pki" / "ca.crt").read_text() == "MINE"
    assert legacy.resolve() == (work / "state").resolve()


def test_a_link_to_state_elsewhere_with_state_on_the_work_disk_is_refused(site, tmp_path, capsys):
    src, work, cfg = site
    elsewhere = tmp_path / "my-state"
    (elsewhere / "pki").mkdir(parents=True)
    (elsewhere / "services").mkdir()
    (work / "state" / "pki").mkdir(parents=True)
    legacy = cfg.parent / ".dciab" / "state"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(elsewhere)
    assert cli.main(args(src, cfg)) == 1
    err = capsys.readouterr().err
    assert str(elsewhere) in err and str(work / "state") in err and "both hold dciab state" in err
    assert f"{work / 'state'} lacks services" in err and "looks like the partial one" in err


def test_a_missing_link_is_recreated(site, capsys):
    """The state moved but the link wasn't left (F2): a rerun puts it back."""
    src, work, cfg = site
    (work / "state" / "pki").mkdir(parents=True)
    (work / "state" / "pki" / "ca.crt").write_text("CA")
    assert cli.main(args(src, cfg)) == 0
    legacy = cfg.parent / ".dciab" / "state"
    assert legacy.is_symlink() and legacy.resolve() == (work / "state").resolve()
    assert "linked" in capsys.readouterr().out


def test_a_link_that_cant_be_made_says_state_is_needed(site, monkeypatch, capsys):
    src, work, cfg = site
    _old_state(cfg)

    def eperm(src_, dst):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(cli.os, "symlink", eperm)
    assert cli.main(args(src, cfg)) == 0
    out = capsys.readouterr().out
    assert "couldn't link" in out and f"every dciab step needs --state {work / 'state'}" in out
    assert (work / "state" / "pki" / "ca.crt").is_file()
    assert f"--state {work / 'state'}" in out.split("Next, from the copy")[1]


def test_a_move_across_filesystems_copies_syncs_then_removes(site, monkeypatch):
    import errno
    src, work, cfg = site
    old = _old_state(cfg)
    real_rename = cli.os.rename
    calls = []

    def rename(a, b):
        calls.append((str(a), str(b)))
        if str(a) == str(old) and str(b) == str(work / "state"):   # only the move onto the work disk
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b)
    monkeypatch.setattr(cli.os, "rename", rename)
    assert cli.main(args(src, cfg)) == 0
    assert (str(work / "state.partial"), str(work / "state")) in calls
    aside = [b for a, b in calls if a == str(old) and ".moved-" in b]
    assert aside, "the source is renamed aside before it's removed"
    assert (work / "state" / "pki" / "ca.crt").read_text() == "CA"
    assert not (work / "state.partial").exists() and not Path(aside[0]).exists()
    assert old.is_symlink()


def test_an_interrupted_cross_filesystem_move_is_named(site, capsys):
    src, work, cfg = site
    old = _old_state(cfg)
    (work / "state.partial" / "pki").mkdir(parents=True)
    assert cli.main(args(src, cfg)) == 1
    err = capsys.readouterr().err
    assert str(work / "state.partial") in err and "partial copy" in err
    assert (old / "pki" / "ca.crt").is_file()                          # the source is untouched


def test_the_space_check_counts_state_copied_across_filesystems(site, monkeypatch):
    src, work, cfg = site
    old = _old_state(cfg)
    (old / "big").write_bytes(b"x" * 5000)
    real_stat = cli.os.stat

    class St:
        def __init__(self, st, dev):
            self._st, self.st_dev = st, dev

        def __getattr__(self, k):
            return getattr(self._st, k)

    def stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        return St(st, 1 if str(path).startswith(str(old)) else 2)
    monkeypatch.setattr(cli.os, "stat", stat)
    assert cli.state_bytes(cfg, work) >= 5000
    monkeypatch.setattr(cli.os, "stat", real_stat)
    assert cli.state_bytes(cfg, work) == 0                              # same filesystem: a rename


def test_check_space_includes_the_extra(tmp_path):
    plan = cli.Plan(tmp_path, tmp_path, {}, [], 10, 0)
    with pytest.raises(cli.MediaError, match="GiB free"):
        cli.check_space(plan, tmp_path, disk_usage=lambda p: Usage(10, 10, 1), extra=5)
    assert cli.check_space(plan, tmp_path, disk_usage=lambda p: Usage(10, 10, 1)) == 0


# --- review fixes: ISOs (F9, F11, F13) ----------------------------------------------------------

LOCK2 = {"hypervisors": {
    "proxmox": {"isoURL": "https://x/proxmox-ve_9.2-1.iso", "isoSHA256": "AA"},
    "esxi": {"isoFile": "VMware-ESXi-9.iso", "isoSHA256": "BB"}}}


def test_pins_follow_the_sites_hypervisor_type():
    by = cli.pins_by_type(LOCK2)
    assert cli.site_pins({"spec": {"hypervisors": {"type": "proxmox"}}}, by) == {"proxmox-ve_9.2-1.iso": "aa"}
    assert cli.site_pins({"spec": {"hypervisors": {"type": "esxi"}}}, by) == {"VMware-ESXi-9.iso": "bb"}
    assert cli.site_pins({"spec": {}}, by) == {}


def test_esxi_iso_comes_from_installer_file(tmp_path):
    iso = tmp_path / "VMware-custom-name.iso"
    iso.write_bytes(b"ESXI")
    doc = {"spec": {"hypervisors": {"type": "esxi", "installerFile": "VMware-custom-name.iso"}}}
    pins = {"VMware-ESXi-9.iso": verify_mod.hashlib.sha256(b"ESXI").hexdigest()}
    sources = cli.site_sources(doc, pins, tmp_path / "dciab.yaml")
    assert sources == {"VMware-ESXi-9.iso": iso}
    archive = tmp_path / "a"
    archive.mkdir()
    assert cli.place_isos(archive, tmp_path / "isos", None, pins, sources=sources) == ["VMware-ESXi-9.iso"]
    assert (tmp_path / "isos" / "VMware-ESXi-9.iso").read_bytes() == b"ESXI"


def test_odd_iso_names_are_rejected():
    for bad in ("https://x/", "https://x/.", "https://x/.."):
        assert cli.pinned_isos({"hypervisors": {"p": {"isoURL": bad, "isoSHA256": "a"}}}) == {}
    assert cli.pinned_isos({"hypervisors": {"e": {"isoFile": "..", "isoSHA256": "a"}}}) == {}


def test_a_placed_iso_isnt_hashed_again(tmp_path, monkeypatch):
    archive, isos = tmp_path / "a", tmp_path / "isos"
    archive.mkdir()
    stick = _stick_iso(tmp_path)
    pins = {"proxmox-ve_9.2-1.iso": verify_mod.hashlib.sha256(ISO).hexdigest()}
    assert cli.place_isos(archive, isos, stick, pins) == ["proxmox-ve_9.2-1.iso"]
    hashed = []
    real = verify_mod.sha256_file
    monkeypatch.setattr(verify_mod, "sha256_file", lambda p: hashed.append(p) or real(p))
    assert cli.place_isos(archive, isos, stick, pins) == []
    assert hashed == []                                               # the same file, unchanged
    assert not (isos / cli.OLD_HASHES).exists() and (tmp_path / ".cache" / cli.HASHES).is_file()
    (isos / "proxmox-ve_9.2-1.iso").write_bytes(b"changed")
    with pytest.raises(cli.MediaError, match="doesn't match"):
        cli.place_isos(archive, isos, stick, pins)


# --- review fixes (re-review N1-N8) -----------------------------------------------------------

def test_the_state_doesnt_move_while_another_step_holds_its_lock(site, capsys):
    import fcntl
    src, work, cfg = site
    old = _old_state(cfg)
    seed_lock = cli.HOST_STEP_LOCKS[0]
    with open(seed_lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert cli.main(args(src, cfg)) == 1
    err = capsys.readouterr().err
    assert f"another dciab step holds {seed_lock}" in err
    assert (old / "pki" / "ca.crt").is_file() and not (work / "state").exists()


def test_a_held_lock_inside_the_state_stops_the_move(site, capsys):
    import fcntl
    src, work, cfg = site
    old = _old_state(cfg)
    with open(old / ".lock", "w") as held:                               # dciab mgmt's lock
        fcntl.flock(held, fcntl.LOCK_EX)
        assert cli.main(args(src, cfg)) == 1
    assert f"another dciab step holds {old / '.lock'}" in capsys.readouterr().err
    assert (old / "pki" / "ca.crt").is_file()


def test_a_link_to_something_that_isnt_state_is_refused(site, tmp_path, capsys):
    src, work, cfg = site
    notes = tmp_path / "notes"
    (notes / "photos").mkdir(parents=True)
    (notes / "notes.txt").write_text("mine")
    legacy = cfg.parent / ".dciab" / "state"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(notes)
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't look like dciab state" in capsys.readouterr().err
    assert (notes / "notes.txt").read_text() == "mine" and not (work / "state").exists()


def test_a_link_to_a_mount_point_is_refused(site, tmp_path, monkeypatch, capsys):
    src, work, cfg = site
    mnt = tmp_path / "mnt"
    (mnt / "pki").mkdir(parents=True)
    legacy = cfg.parent / ".dciab" / "state"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(mnt)
    real = cli.os.path.ismount
    monkeypatch.setattr(cli.os.path, "ismount", lambda p: str(p) == str(mnt) or real(p))
    assert cli.main(args(src, cfg)) == 1
    assert "a mount point" in capsys.readouterr().err
    assert (mnt / "pki").is_dir()


def test_an_empty_state_folder_is_replaced_by_the_link(site, capsys):
    src, work, cfg = site
    legacy = cfg.parent / ".dciab" / "state"
    legacy.mkdir(parents=True)                                          # empty, real
    (work / "state" / "pki").mkdir(parents=True)
    assert "replace the empty folder" in cli.state_change(cfg, work)
    assert cli.main(args(src, cfg)) == 0
    assert legacy.is_symlink() and legacy.resolve() == (work / "state").resolve()


def test_an_iso_replaced_with_the_same_size_and_mtime_is_hashed_again(tmp_path):
    archive, isos = tmp_path / "a", tmp_path / "isos"
    archive.mkdir()
    stick = _stick_iso(tmp_path)
    pins = {"proxmox-ve_9.2-1.iso": verify_mod.hashlib.sha256(ISO).hexdigest()}
    cli.place_isos(archive, isos, stick, pins)
    iso = isos / "proxmox-ve_9.2-1.iso"
    st = iso.stat()
    other = tmp_path / "other.iso"
    other.write_bytes(bytes(len(ISO)))                                  # same size, other bytes
    os.replace(other, iso)                                              # a new inode
    os.utime(iso, ns=(st.st_atime_ns, st.st_mtime_ns))                  # the old mtime put back
    with pytest.raises(cli.MediaError, match="doesn't match"):
        cli.place_isos(archive, isos, stick, pins)
