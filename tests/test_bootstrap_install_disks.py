"""The appliance installer reading lsblk and writing its kickstart, from recorded lsblk output."""
import os
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from conftest import load_example
from dciab_bootstrap_host import install

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "lsblk")


def _lsblk(monkeypatch, fixture=None, fail=False):
    """Serve a recorded `lsblk -J` (or fail it); keep kernel paths (no /dev/disk/by-id here)."""
    seen = []

    def run(cmd, check=False, capture_output=False, text=False):
        seen.append(list(cmd))
        if fail:
            raise subprocess.CalledProcessError(32, cmd, stderr="lsblk: failed")
        with open(os.path.join(FIXTURES, fixture)) as fh:
            return SimpleNamespace(stdout=fh.read(), returncode=0)

    monkeypatch.setattr(install.subprocess, "run", run)
    monkeypatch.setattr(install, "stable_path", lambda p: p)
    return seen


def test_only_whole_disks_are_candidates(monkeypatch):
    seen = _lsblk(monkeypatch, "server.json")
    names = [d["name"] for d in install.disks()]
    assert names == ["sda", "sdb", "sdc", "sdd"]                    # no rom, loop or partition
    assert seen[0][:4] == ["lsblk", "--json", "--bytes", "--tree"]


def test_fields_are_trimmed_and_typed(monkeypatch):
    _lsblk(monkeypatch, "server.json")
    sda = install.disks()[0]
    assert sda == {"name": "sda", "path": "/dev/sda", "size": 480103981056, "rotational": False,
                   "serialNumber": "S6XXNX0T500001", "model": "Dell BOSS-N1", "vendor": "DELL",
                   "wwn": "0x5000c500a1b2c3d4", "hctl": "0:0:0:0", "isDciabStick": False, "byId": "/dev/sda"}


def test_the_stick_is_flagged_by_its_partition_label(monkeypatch):
    _lsblk(monkeypatch, "server.json")
    flags = {d["name"]: d["isDciabStick"] for d in install.disks()}
    assert flags == {"sda": False, "sdb": False, "sdc": False, "sdd": True}


def test_older_lsblk_strings_and_missing_fields(monkeypatch):
    _lsblk(monkeypatch, "old-lsblk.json")
    vda, vdb = install.disks()
    assert vda["rotational"] is True and vdb["rotational"] is False   # "1"/"0", not booleans
    assert vda["size"] == 32 * install.GiB                            # a string size
    assert vda["serialNumber"] == vda["model"] == vda["wwn"] == vda["hctl"] == ""
    assert vdb["isDciabStick"] is True                                # the label on the disk itself


def test_rota_boolean_true(monkeypatch):
    _lsblk(monkeypatch, "server.json")
    assert {d["name"]: d["rotational"] for d in install.disks()}["sdc"] is True


# --- main() --------------------------------------------------------------------------------

def _config(tmp_path, hints):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["workDisk"] = {"disk": hints}
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc))
    return cfg


def test_main_writes_a_kickstart_for_the_one_match(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    out = tmp_path / "disk.ks"
    assert install.main([str(_config(tmp_path, {"serialNumber": "PHYF1234"})), "--out", str(out)]) == 0
    ks = out.read_text()
    assert "ignoredisk --only-use=/dev/sdb" in ks and "clearpart --all --initlabel --drives=/dev/sdb" in ks
    assert "/dev/sda" not in ks and "/dev/sdc" not in ks
    assert "installing onto /dev/sdb" in capsys.readouterr().out


def test_main_refuses_no_match(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    out = tmp_path / "disk.ks"
    assert install.main([str(_config(tmp_path, {"serialNumber": "NOPE"})), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "not installing" in err and "matches no disk" in err and "/dev/sdd" in err
    assert not out.exists()


def test_main_refuses_more_than_one_match(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    out = tmp_path / "disk.ks"
    assert install.main([str(_config(tmp_path, {"model": "SAMSUNG"})), "--out", str(out)]) == 1
    assert "2 disks (/dev/sdb, /dev/sdc)" in capsys.readouterr().err
    assert not out.exists()


def test_main_never_picks_the_stick(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    out = tmp_path / "disk.ks"
    assert install.main([str(_config(tmp_path, {"serialNumber": "USB0001"})), "--out", str(out)]) == 1
    assert "(the DCiaB stick)" in capsys.readouterr().err


def test_main_refuses_bad_yaml(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: [unclosed\n")
    assert install.main([str(cfg), "--out", str(tmp_path / "disk.ks")]) == 1
    assert "not installing" in capsys.readouterr().err


def test_main_refuses_a_missing_config(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, "server.json")
    assert install.main([str(tmp_path / "absent.yaml"), "--out", str(tmp_path / "disk.ks")]) == 1


def test_main_refuses_when_lsblk_fails(tmp_path, monkeypatch, capsys):
    _lsblk(monkeypatch, fail=True)
    out = tmp_path / "disk.ks"
    assert install.main([str(_config(tmp_path, {"serialNumber": "PHYF1234"})), "--out", str(out)]) == 1
    assert "not installing" in capsys.readouterr().err
    assert not out.exists()


def test_device_name_and_size_hints(tmp_path, monkeypatch):
    _lsblk(monkeypatch, "server.json")
    candidates = install.disks()
    assert install.match_disk({"deviceName": "/dev/sda"}, candidates)["name"] == "sda"
    assert install.match_disk({"minSizeGigabytes": 1000, "rotational": True}, candidates)["name"] == "sdc"
    with pytest.raises(install.InstallError):
        install.match_disk({"deviceName": "/dev/sdz"}, candidates)


def test_stable_path_without_by_id(tmp_path):
    assert install.stable_path("/dev/sda", by_id_dir=str(tmp_path / "absent")) == "/dev/sda"
