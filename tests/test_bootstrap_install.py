"""The appliance installer's disk choice."""
import pytest

from conftest import load_example
from dciab_bootstrap_host.install import GiB, InstallError, hints_from, kickstart, match_disk, stable_path


def _disk(name, size_gib, serial="", model="", rotational=False, stick=False, **extra):
    d = {"name": name, "path": f"/dev/{name}", "size": size_gib * GiB, "rotational": rotational,
         "serialNumber": serial, "model": model, "vendor": "", "wwn": "", "hctl": "", "isDciabStick": stick}
    d.update(extra)
    return d


DISKS = [
    _disk("sda", 480, serial="S6XXNX0T500001", model="Dell BOSS-N1"),
    _disk("sdb", 3840, serial="PHYF1234", model="SAMSUNG MZ7L33T8"),
    _disk("sdc", 3840, serial="PHYF5678", model="SAMSUNG MZ7L33T8"),
    _disk("sdd", 256, serial="USB0001", model="Extreme Pro", stick=True),
]


def test_match_by_serial():
    assert match_disk({"serialNumber": "S6XXNX0T500001"}, DISKS)["name"] == "sda"


def test_model_is_a_substring_match():
    assert match_disk({"model": "boss"}, DISKS)["name"] == "sda"


def test_ambiguous_hints_stop_the_install():
    with pytest.raises(InstallError, match="matches 2 disks"):
        match_disk({"model": "SAMSUNG"}, DISKS)


def test_no_match_lists_the_disks():
    with pytest.raises(InstallError, match="matches no disk") as exc:
        match_disk({"serialNumber": "nope"}, DISKS)
    for d in DISKS:
        assert f"{d['path']}: {d['size'] // GiB} GiB, serial {d['serialNumber']}" in str(exc.value)
    assert "(the DCiaB stick)" in str(exc.value)


def test_the_stick_is_never_chosen():
    with pytest.raises(InstallError):
        match_disk({"serialNumber": "USB0001"}, DISKS)


def test_size_and_rotational():
    assert match_disk({"minSizeGigabytes": 400, "rotational": False, "model": "boss"}, DISKS)["name"] == "sda"
    with pytest.raises(InstallError):
        match_disk({"minSizeGigabytes": 500, "model": "boss"}, DISKS)


def test_kickstart_touches_only_the_chosen_disk():
    ks = kickstart(DISKS[0])
    assert "ignoredisk --only-use=sda" in ks
    assert "clearpart --all --initlabel --drives=sda" in ks
    assert "--ondisk=sda" in ks
    for other in DISKS[1:]:
        assert other["name"] not in ks


def test_kickstart_prefers_the_by_id_name():
    disk = dict(DISKS[0], byId="/dev/disk/by-id/wwn-0x5000c500a1b2c3d4")
    ks = kickstart(disk)
    assert "ignoredisk --only-use=/dev/disk/by-id/wwn-0x5000c500a1b2c3d4" in ks
    assert "--drives=/dev/disk/by-id/wwn-0x5000c500a1b2c3d4" in ks
    assert "--ondisk=/dev/disk/by-id/wwn-0x5000c500a1b2c3d4" in ks


def test_stable_path_picks_the_most_stable_link(tmp_path):
    dev = tmp_path / "sda"
    dev.write_text("")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    for name in ("ata-SAMSUNG_X_123", "wwn-0x5000c500a1b2c3d4", "ata-SAMSUNG_X_123-part1"):
        (by_id / name).symlink_to(dev)
    assert stable_path(str(dev), str(by_id)) == str(by_id / "wwn-0x5000c500a1b2c3d4")
    assert stable_path(str(tmp_path / "sdb"), str(by_id)) == str(tmp_path / "sdb")


def test_hints_from_the_example():
    assert hints_from(load_example("proxmox-bare")) == {"serialNumber": "S6XXNX0T500001"}


def test_workdisk_path_is_not_for_the_appliance():
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["workDisk"] = {"path": "/var/lib/dciab"}
    with pytest.raises(InstallError, match="workDisk.disk"):
        hints_from(doc)


@pytest.mark.parametrize("work", [None, {"disk": None}, {"disk": {}}, {"disk": {"serial": "x"}}, "disk"])
def test_unusable_workdisk_is_a_clear_error(work):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["workDisk"] = work
    with pytest.raises(InstallError):
        hints_from(doc)


def test_missing_workdisk():
    doc = load_example("proxmox-bare")
    del doc["spec"]["bootstrap"]["workDisk"]
    with pytest.raises(InstallError, match="no spec.bootstrap.workDisk"):
        hints_from(doc)
