"""The bootstrap appliance's setup plans, from the shipped examples."""
import json

import pytest
import yaml

from conftest import ROOT, load_example
from dciab_bootstrap_host import setup
from dciab_bootstrap_host.setup import ConfigError, main, plan_admins, plan_network


def _doc(**bootstrap):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"].update(bootstrap)
    return doc


def test_network_by_name_untagged():
    p = plan_network(_doc(interface="enp3s0f1", managementVLAN="untagged"))
    assert p == {
        "match": {"name": "enp3s0f1"},
        "vlan": None,
        "address": "10.30.110.5/24",
        "gateway": "10.30.110.1",
        "dns": [],                          # isolated site
        "dnsSearch": "lab1.example.com",
    }


def test_network_by_mac_tagged():
    p = plan_network(_doc(interface="B8:CA:3A:00:00:05", managementVLAN="tagged"))
    assert p["match"] == {"mac": "b8:ca:3a:00:00:05"}
    assert p["vlan"] == 110


def test_network_needs_vlan_mode():
    doc = _doc()
    doc["spec"]["bootstrap"].pop("managementVLAN", None)
    with pytest.raises(ConfigError, match="managementVLAN"):
        plan_network(doc)


def test_network_rejects_address_outside_node_network():
    with pytest.raises(ConfigError, match="outside"):
        plan_network(_doc(managementVLAN="untagged", address="10.99.0.5"))


def test_admins_merge_keys_per_user():
    doc = _doc()
    first = doc["spec"]["identity"]["initialAdmins"][0]
    doc["spec"]["identity"]["initialAdmins"].append(dict(first, sshPublicKey="ssh-ed25519 AAAAsecond second"))
    [a] = plan_admins(doc)
    assert a["username"] == "tparker"
    assert len(a["sshPublicKeys"]) == 2


def test_admins_reject_root():
    doc = _doc()
    doc["spec"]["identity"]["initialAdmins"][0]["username"] = "root"
    with pytest.raises(ConfigError, match="root"):
        plan_admins(doc)


def test_print_shows_both_plans(tmp_path, capsys):
    path = tmp_path / "dciab.yaml"
    path.write_text(yaml.safe_dump(_doc(interface="eno1", managementVLAN="tagged")))
    assert main([str(path), "--print"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["network"]["match"] == {"name": "eno1"}
    assert out["network"]["vlan"] == 110
    assert out["network"]["address"] == "10.30.110.5/24"
    assert [a["username"] for a in out["admins"]] == ["tparker"]


def test_admins_still_planned_when_network_is_not(tmp_path, capsys):
    doc = _doc()
    doc["spec"]["bootstrap"].pop("managementVLAN", None)
    path = tmp_path / "dciab.yaml"
    path.write_text(yaml.safe_dump(doc))
    assert main([str(path), "--print"]) == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["network"] is None and out["admins"]
    assert "managementVLAN" in captured.err


def test_blocking_errors_only_cover_network_and_identity(site, monkeypatch):
    monkeypatch.setenv("DCIAB_ROOT", str(ROOT))
    doc = _doc(managementVLAN="untagged")
    doc["spec"]["hypervisors"]["hosts"][0]["bmc"]["credentials"]["password"] = {"file": "./secrets/bmc/missing"}  # not blocking
    path = site.dir / "dciab.yaml"
    path.write_text(yaml.safe_dump(doc))
    assert setup.blocking_errors(str(path)) == []
    doc["spec"]["network"]["ntpServers"] = ["10.30.0.123"]                          # blocking
    path.write_text(yaml.safe_dump(doc))
    [err] = setup.blocking_errors(str(path))
    assert err.startswith("spec.network.ntpServers")


def test_sync_copies_config_and_secrets_privately(tmp_path):
    usb, site = tmp_path / "usb", tmp_path / "site"
    (usb / "secrets" / "bmc").mkdir(parents=True)
    (usb / "dciab.yaml").write_text("spec: {}\n")
    (usb / "secrets" / "bmc" / "pve01").write_text("pw\n")
    (usb / "isos").mkdir()
    (usb / "isos" / "big.iso").write_text("not copied")
    assert setup.sync_site(str(usb), str(site)) is True
    assert (site / "dciab.yaml").read_text() == "spec: {}\n"
    assert (site / "dciab.yaml").stat().st_mode & 0o777 == 0o600
    assert (site / "secrets" / "bmc" / "pve01").stat().st_mode & 0o777 == 0o600
    assert site.is_symlink() and site.resolve().stat().st_mode & 0o777 == 0o700
    assert not (site / "isos").exists()
    # A second sync replaces the copy as a whole.
    (usb / "secrets" / "bmc" / "pve01").unlink()
    first = site.resolve()
    assert setup.sync_site(str(usb), str(site)) is True
    assert not (site / "secrets" / "bmc" / "pve01").exists()
    assert not first.exists()                      # the previous copy is cleaned up
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith(".")) == ["site", "usb"]
    assert [p for p in tmp_path.glob(".site-*")] == [site.resolve()]   # only the current copy


def test_sync_replaces_an_old_plain_directory(tmp_path):
    usb, site = tmp_path / "usb", tmp_path / "site"
    usb.mkdir()
    (usb / "dciab.yaml").write_text("new\n")
    site.mkdir()
    (site / "dciab.yaml").write_text("old\n")
    assert setup.sync_site(str(usb), str(site)) is True
    assert site.is_symlink() and (site / "dciab.yaml").read_text() == "new\n"


def test_failed_copy_stops_setup(tmp_path, monkeypatch, capsys):
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / "dciab.yaml").write_text("spec: {}\n")

    def boom(*a, **k):
        raise OSError("read error")

    monkeypatch.setattr(setup, "sync_site", boom)
    assert main(["--usb", str(usb), "--site", str(tmp_path / "site")]) == 1
    assert "nothing changed" in capsys.readouterr().err


def test_sync_without_stick_keeps_the_copy(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "dciab.yaml").write_text("kept\n")
    assert setup.sync_site(str(tmp_path / "no-stick"), str(site)) is False
    assert (site / "dciab.yaml").read_text() == "kept\n"
