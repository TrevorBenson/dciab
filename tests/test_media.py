"""dciab media: target checks, the copy (idempotent, checked), --print and the guards."""

import collections
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_archive import verify as verify_mod  # noqa: E402
from dciab_media import cli  # noqa: E402

Usage = collections.namedtuple("Usage", "total used free")


def make_archive(base: Path, version="1.2.3") -> Path:
    src = base / "usb" / f"dciab-{version}"
    files = {
        "dciab": b"#!/usr/bin/env bash\necho dciab\n",
        "VERSION": f"{version}\n".encode(),
        "lib/x.py": b"print('x')\n",
        "payload/SHA256SUMS": b"",
        "payload/images/blob": b"B" * 4096,
        "payload/isos/installer.iso": b"ISO" * 100,
    }
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    (src / "dciab").chmod(0o755)
    sums = {rel: verify_mod.sha256_file(src / rel) for rel in files}
    (src / "MANIFEST").write_text(verify_mod.manifest_text(sums))
    return src


def write_config(base: Path, work: Path) -> Path:
    cfg = base / "dciab.yaml"
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"workDisk": {"path": str(work)}}}}))
    return cfg


@pytest.fixture
def site(tmp_path, monkeypatch):
    src = make_archive(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "dciab-media.lock")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    # podman already on the work disk: the storage step changes nothing.
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    return src, work, cfg


def args(src, cfg, *extra):
    return [str(cfg), "--root", str(src), "--checksums-only", *extra]


def test_copy_verify_and_rerun(site, capsys):
    src, work, cfg = site
    assert cli.main(args(src, cfg)) == 0
    dest = work / "dciab-1.2.3"
    assert (dest / "payload/images/blob").read_bytes() == b"B" * 4096
    assert (dest / "dciab").stat().st_mode & 0o777 == 0o755
    assert (work / "isos/installer.iso").read_bytes() == b"ISO" * 100
    assert verify_mod.verify_dir(dest, None) == []
    out = capsys.readouterr().out
    assert "copied 7 files" in out and f"{dest}/dciab seed {cfg} --state {work}/state" in out
    assert not list(work.rglob("*" + cli.TMP_SUFFIX))

    # A rerun copies nothing.
    mtimes = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
    assert cli.main(args(src, cfg)) == 0
    out = capsys.readouterr().out
    assert "copy\n  unchanged" in out
    assert mtimes == {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}


def test_damaged_copy_is_replaced(site):
    src, work, cfg = site
    assert cli.main(args(src, cfg)) == 0
    blob = work / "dciab-1.2.3/payload/images/blob"
    blob.write_bytes(b"C" * 4096)  # same size, wrong content
    plan = cli.make_plan(src, work)
    assert plan.to_copy == ["payload/images/blob"]
    assert cli.main(args(src, cfg)) == 0
    assert blob.read_bytes() == b"B" * 4096


def test_damaged_source_refused(site, capsys):
    src, work, cfg = site
    (src / "lib/x.py").write_bytes(b"print('y')\n")  # same size, no longer the MANIFEST's sha256
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't match MANIFEST" in capsys.readouterr().err
    assert not (work / "dciab-1.2.3/lib/x.py").exists()
    assert not list(work.rglob("*" + cli.TMP_SUFFIX))


def test_verify_refuses_extra_file_in_copy(site, capsys):
    src, work, cfg = site
    assert cli.main(args(src, cfg, "--until", "copy")) == 0
    (work / "dciab-1.2.3/lib/planted.py").write_text("x")
    assert cli.main(args(src, cfg)) == 1
    err = capsys.readouterr().err
    assert "does NOT match" in err and "planted.py" in err


def test_print_changes_nothing(site, capsys, monkeypatch):
    src, work, cfg = site
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)  # --print needs no root
    assert cli.main(args(src, cfg, "--print")) == 0
    out = capsys.readouterr().out
    assert f"source:  {src}" in out and f"target:  {work / 'dciab-1.2.3'}" in out
    assert "copy:    7 of 7 files" in out and "run --print as root" in out
    assert list(work.iterdir()) == []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(args(src, cfg, "--print")) == 0
    assert "storage: unchanged" in capsys.readouterr().out
    assert list(work.iterdir()) == []


def test_print_shows_storage_change(site, capsys, monkeypatch):
    src, work, cfg = site
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: "/var/lib/containers/storage")
    assert cli.main(args(src, cfg, "--print")) == 0
    assert f"graphroot {work / 'containers'}" in capsys.readouterr().out


def test_non_root_refused(site, capsys, monkeypatch):
    src, work, cfg = site
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert cli.main(args(src, cfg)) == 2
    assert "run as root" in capsys.readouterr().err
    assert list(work.iterdir()) == []


def test_insufficient_space_refused(site, capsys, monkeypatch):
    src, work, cfg = site
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: Usage(10 * cli.GiB, 9 * cli.GiB, cli.GiB))
    assert cli.main(args(src, cfg)) == 1
    assert "needs 2.0 GiB" in capsys.readouterr().err
    assert not (work / "dciab-1.2.3").exists()


def test_network_filesystem_refused(site):
    src, work, cfg = site
    mounts = f"/dev/sda1 / xfs rw 0 0\nnas:/x {work} nfs4 rw 0 0\n"
    with pytest.raises(cli.MediaError, match="nfs4"):
        cli.resolve_target(yaml.safe_load(cfg.read_text()), mounts)


def test_missing_path_refused(tmp_path):
    doc = {"spec": {"bootstrap": {"workDisk": {"path": str(tmp_path / "nope")}}}}
    with pytest.raises(cli.MediaError, match="not an existing directory"):
        cli.resolve_target(doc, "/dev/sda1 / xfs rw 0 0\n")


def test_disk_mode_requires_root_on_matching_disk(tmp_path, monkeypatch):
    base = tmp_path / "var-lib-dciab"
    base.mkdir()
    monkeypatch.setattr(cli, "WORK_DIR_APPLIANCE", base / "work")
    doc = {"spec": {"bootstrap": {"workDisk": {"disk": {"serialNumber": "S1"}}}}}
    disks = [{"name": "sda", "path": "/dev/sda", "size": 1 << 40, "rotational": False, "serialNumber": "S1",
              "model": "", "vendor": "", "wwn": "", "hctl": "", "isDciabStick": False},
             {"name": "sdb", "path": "/dev/sdb", "size": 1 << 40, "rotational": False, "serialNumber": "S2",
              "model": "", "vendor": "", "wwn": "", "hctl": "", "isDciabStick": False}]

    def lsblk(disk):
        def runner(cmd):
            tree = {"blockdevices": [{"path": "/dev/mapper/rl-root", "type": "lvm",
                                      "children": [{"path": disk + "3", "type": "part",
                                                    "children": [{"path": disk, "type": "disk"}]}]}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(tree), "")
        return runner

    mounts = "/dev/mapper/rl-root / xfs rw 0 0\n"
    assert cli.resolve_target(doc, mounts, lsblk("/dev/sda"), lambda: disks) == base / "work"
    with pytest.raises(cli.MediaError, match="doesn't partition"):
        cli.resolve_target(doc, mounts, lsblk("/dev/sdb"), lambda: disks)
    with pytest.raises(cli.MediaError, match="matches no disk"):
        cli.resolve_target({"spec": {"bootstrap": {"workDisk": {"disk": {"serialNumber": "X"}}}}},
                           mounts, lsblk("/dev/sda"), lambda: disks)


def test_storage_refused_while_containers_run(tmp_path):
    calls = []

    def runner(cmd):
        calls.append(cmd)
        out = "/var/lib/containers/storage" if cmd[1] == "info" else "abc123\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    with pytest.raises(cli.MediaError, match="containers are running"):
        cli.set_storage(tmp_path / "containers", runner, tmp_path / "conf.d/50.conf")
    assert not (tmp_path / "conf.d").exists()


def test_storage_dropin_written(tmp_path):
    graphroot = tmp_path / "containers"
    dropin = tmp_path / "conf.d/50.conf"
    state = {"root": "/var/lib/containers/storage"}

    def runner(cmd):
        if cmd[:2] == ["podman", "info"]:
            if dropin.is_file():
                state["root"] = str(graphroot)
            return subprocess.CompletedProcess(cmd, 0, state["root"], "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert cli.set_storage(graphroot, runner, dropin) is True
    assert f'graphroot = "{graphroot}"' in dropin.read_text()
    assert cli.set_storage(graphroot, runner, dropin) is False


def test_mount_of_longest_prefix():
    text = "/dev/sda1 / xfs rw 0 0\n/dev/sdb1 /data ext4 rw 0 0\n/dev/sdc1 /data2 xfs rw 0 0\n"
    assert cli.mount_of(Path("/data/work"), text)["source"] == "/dev/sdb1"
    assert cli.mount_of(Path("/data2"), text)["source"] == "/dev/sdc1"
    assert cli.mount_of(Path("/srv"), text)["source"] == "/dev/sda1"


def test_key_or_checksums_only_required(site, capsys):
    src, work, cfg = site
    with pytest.raises(SystemExit):
        cli.main([str(cfg), "--root", str(src)])
    assert list(work.iterdir()) == []


def test_mode_change_repaired(site):
    src, work, cfg = site
    assert cli.main(args(src, cfg)) == 0
    (work / "dciab-1.2.3/dciab").chmod(0o644)
    assert cli.main(args(src, cfg)) == 0
    assert (work / "dciab-1.2.3/dciab").stat().st_mode & 0o777 == 0o755


def test_site_iso_with_same_name_kept(site, capsys):
    src, work, cfg = site
    (work / "isos").mkdir()
    (work / "isos/installer.iso").write_bytes(b"site's own")
    assert cli.main(args(src, cfg)) == 1
    assert "differs from the archive" in capsys.readouterr().err
    assert (work / "isos/installer.iso").read_bytes() == b"site's own"


def test_symlinked_dir_in_copy_refused(site, tmp_path):
    src, work, cfg = site
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (work / "dciab-1.2.3").mkdir()
    (work / "dciab-1.2.3/lib").symlink_to(elsewhere)
    assert cli.main(args(src, cfg)) == 1
    assert list(elsewhere.iterdir()) == []


def test_printed_commands_are_quoted(tmp_path):
    lines = cli.next_commands(Path("/w/dciab-1"), Path("/a b/dciab.yaml"), Path("/w"))
    assert "'/a b/dciab.yaml'" in lines[1]


def test_unusable_graphroot_refused():
    with pytest.raises(cli.MediaError, match="quotes"):
        cli.dropin_text(Path('/x"\n[storage.options]'))


def test_malformed_workdisk():
    with pytest.raises(cli.MediaError, match="mapping"):
        cli.resolve_target({"spec": {"bootstrap": {"workDisk": None}}}, "/dev/sda1 / xfs rw 0 0\n")
