"""The bootstrap appliance's setup plan, from the shipped examples."""
import pytest

from conftest import load_example
from dciab_bootstrap_host.setup import ConfigError, main, plan


def _doc(**bootstrap):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"].update(bootstrap)
    return doc


def test_plan_by_name_untagged():
    p = plan(_doc(managementVLAN="untagged"))
    assert p["match"] == {"name": "eth0"}
    assert p["vlan"] is None
    assert p["address"] == "10.30.110.5/24"
    assert p["gateway"] == "10.30.110.1"
    assert p["dns"] == []                       # isolated site
    assert p["dnsSearch"] == "lab1.example.com"
    assert [a["username"] for a in p["admins"]] == ["tparker"]


def test_plan_by_mac_tagged():
    p = plan(_doc(interface="B8:CA:3A:00:00:05", managementVLAN="tagged"))
    assert p["match"] == {"mac": "b8:ca:3a:00:00:05"}
    assert p["vlan"] == 110


def test_plan_needs_vlan_mode():
    doc = _doc()
    doc["spec"]["bootstrap"].pop("managementVLAN", None)
    with pytest.raises(ConfigError, match="managementVLAN"):
        plan(doc)


def test_plan_rejects_address_outside_node_network():
    with pytest.raises(ConfigError, match="outside"):
        plan(_doc(managementVLAN="untagged", address="10.99.0.5"))


def test_print_mode(tmp_path, capsys):
    import yaml

    path = tmp_path / "dciab.yaml"
    path.write_text(yaml.safe_dump(_doc(managementVLAN="untagged")))
    assert main([str(path), "--print"]) == 0
    assert '"address": "10.30.110.5/24"' in capsys.readouterr().out


def test_missing_section_is_a_clear_error(tmp_path, capsys):
    path = tmp_path / "dciab.yaml"
    path.write_text("spec: {}\n")
    assert main([str(path), "--print"]) == 1
    assert "has no spec.bootstrap" in capsys.readouterr().err
