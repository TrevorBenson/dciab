"""dciab media follow-ups (#165): the state step, and pinned installer ISOs from the stick."""
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
    return src, work, cfg


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
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    _stick_iso(tmp_path, data=ISO[:-1] + b"X")
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't match the sha256 bundle.lock pins" in capsys.readouterr().err
    assert not (work / "isos" / "proxmox-ve_9.2-1.iso").exists()
    assert not list((work / "isos").glob("*" + cli.TMP_SUFFIX))


def test_a_wrong_iso_already_in_work_isos_is_refused(site, tmp_path, capsys):
    src, work, cfg = site
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    (work / "isos").mkdir()
    (work / "isos" / "proxmox-ve_9.2-1.iso").write_bytes(b"old build")
    assert cli.main(args(src, cfg)) == 1
    assert "rename or remove it" in capsys.readouterr().err


def test_isos_from_overrides_the_stick(site, tmp_path):
    src, work, cfg = site
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    other = tmp_path / "downloads"
    other.mkdir()
    (other / "proxmox-ve_9.2-1.iso").write_bytes(ISO)
    assert cli.main(args(src, cfg, "--isos-from", str(other))) == 0
    assert (work / "isos" / "proxmox-ve_9.2-1.iso").is_file()


def test_missing_iso_is_a_note_for_hypervisor_sites(site, capsys):
    src, work, cfg = site
    _lock(src, verify_mod.hashlib.sha256(ISO).hexdigest())
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"workDisk": {"path": str(work)}},
                                            "hypervisors": {"hosts": []}}}))
    assert cli.main(args(src, cfg)) == 0
    assert "note: proxmox-ve_9.2-1.iso (pinned in bundle.lock) isn't in" in capsys.readouterr().out


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
