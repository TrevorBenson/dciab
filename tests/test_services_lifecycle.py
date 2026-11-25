"""dciab services lifecycle (#164): what it added to the host is recorded, pruned when the
config no longer wants it, and removed by --down, against a fake host."""

import copy
import subprocess
from pathlib import Path

import pytest

from conftest import load_example

from dciab_services import cli, lifecycle, render


class FakeHost:
    """podman, ip, firewall-cmd and systemctl over a little in-memory host."""

    def __init__(self):
        self.pods = {"dciab-services"}
        self.addrs = {"eth0": {"10.30.110.5/24", "10.30.110.53/24", "10.30.110.54/24"}}
        self.ports = {("public", "53/udp"), ("public", "53/tcp"), ("public", "80/tcp"), ("public", "22/tcp")}
        self.permanent = {("public", "53/udp"), ("public", "80/tcp")}
        self.firewalld = True
        self.services = set()
        self.calls = []
        self.fail = set()   # commands (joined) that fail

    def __call__(self, cmd, check=False, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        rc, out = 0, ""
        if any(f in joined for f in self.fail):
            rc = 1
        elif cmd[:3] == ["podman", "pod", "exists"]:
            rc = 0 if cmd[3] in self.pods else 1
        elif cmd[:3] == ["podman", "pod", "rm"]:
            self.pods.discard(cmd[-1])
        elif cmd[:2] == ["ip", "-o"]:
            out = " ".join(f"2: {cmd[-1]} inet {a} scope global" for a in sorted(self.addrs.get(cmd[-1], ())))
        elif cmd[:3] == ["ip", "addr", "del"]:
            self.addrs[cmd[-1]].discard(cmd[3])
        elif cmd[0] == "firewall-cmd":
            if not self.firewalld:
                rc = 252
            elif cmd[1] != "--state":
                perm = cmd[1] == "--permanent"
                rest = cmd[2:] if perm else cmd[1:]
                zone, (op, port) = rest[0].split("=")[1], rest[1].lstrip("-").split("=")
                where = self.permanent if perm else self.ports
                if op == "query-port":
                    rc = 0 if (zone, port) in where else 1
                elif op == "remove-port":
                    where.discard((zone, port))
        elif cmd[0] == "systemctl":
            if cmd[1] == "cat":
                rc = 0 if cmd[2] in self.services else 1
            elif cmd[1] == "stop":
                self.services.discard(cmd[2])
        else:
            raise AssertionError(f"unexpected command {cmd}")
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd, out, "boom")
        return subprocess.CompletedProcess(cmd, rc, out, "")


@pytest.fixture
def host(monkeypatch):
    h = FakeHost()
    monkeypatch.setattr(lifecycle.subprocess, "run", h)
    return h


def conf_with(tmp_path, lines, applied=True):
    conf = tmp_path / "services"
    conf.mkdir()
    (conf / "created").write_text("".join(f"{line}\n" for line in lines))
    if applied:
        (conf / "applied").write_text("fp\n")
    return conf


FULL = ["address eth0 10.30.110.53/24", "address eth0 10.30.110.54/24", "port public 53/udp",
        "port public 53/tcp", "port public 80/tcp", "pod dciab-services"]


def test_down_undoes_exactly_what_is_listed(tmp_path, host):
    conf = conf_with(tmp_path, FULL)
    assert lifecycle.down(conf, lambda m: None) == []
    assert host.pods == set()
    assert host.addrs["eth0"] == {"10.30.110.5/24"}            # the bootstrap address was never ours
    assert host.ports == {("public", "22/tcp")}                  # nor was ssh
    assert host.permanent == set()                               # permanent copies closed too
    assert not (conf / "created").exists() and not (conf / "applied").exists()
    # idempotent: nothing left to do, nothing changed
    host.calls.clear()
    assert lifecycle.down(conf, lambda m: None) == []
    assert not [c for c in host.calls if c[:3] in (["podman", "pod", "rm"], ["ip", "addr", "del"])]


def test_down_removes_units_before_the_pod(tmp_path, host):
    unit = tmp_path / "dciab-services.kube"
    unit.write_text("[Kube]\n")
    svc = tmp_path / "dciab-services-addresses.service"
    svc.write_text("[Unit]\n")
    host.services = {"dciab-services.service", "dciab-services-addresses.service"}
    conf = conf_with(tmp_path, FULL + [f"unit {unit}", f"unit {svc}"])
    assert lifecycle.down(conf, lambda m: None) == []
    assert not unit.exists() and not svc.exists() and host.services == set()
    order = [" ".join(c[:3]) for c in host.calls if c[0] in ("systemctl", "podman") and c[1] in ("stop", "pod")]
    assert order.index("systemctl stop dciab-services.service") < order.index("podman pod rm")
    assert ["systemctl", "disable", "dciab-services-addresses.service"] in host.calls


def test_down_keeps_what_it_could_not_undo(tmp_path, host):
    host.fail = {"ip addr del 10.30.110.54/24"}
    conf = conf_with(tmp_path, FULL)
    left = lifecycle.down(conf, lambda m: None)
    assert left == ["address eth0 10.30.110.54/24"]
    assert (conf / "created").read_text() == "address eth0 10.30.110.54/24\n"
    assert (conf / "applied").exists()   # not a clean teardown yet
    assert host.pods == set()


def test_down_without_firewalld_leaves_ports_alone(tmp_path, host):
    host.firewalld = False
    conf = conf_with(tmp_path, FULL)
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert any("firewalld isn't running" in m for m in msgs)


def test_down_skips_lines_it_does_not_understand(tmp_path, host):
    conf = conf_with(tmp_path, ["bogus line", "pod dciab-services"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert any("doesn't understand: bogus line" in m for m in msgs)


def test_down_with_no_record(tmp_path, host):
    conf = tmp_path / "services"
    conf.mkdir()
    msgs = []
    assert lifecycle.down(conf, msgs.append) == [] and "lists nothing" in msgs[0]
    assert host.calls == []


def bare():
    return copy.deepcopy(load_example("proxmox-bare"))


def test_prune_after_a_config_change(tmp_path, host):
    """A new second DNS service IP, and the site no longer needs HTTP (spec.hypervisors gone):
    the old address and port 80 go; everything still wanted stays."""
    conf = conf_with(tmp_path, FULL)
    doc = copy.deepcopy(load_example("proxmox"))           # no spec.hypervisors: DNS only
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.53", "10.30.110.55"]
    left = lifecycle.prune(conf, cli.wanted(doc, "eth0", "public"), say=lambda m: None)
    assert left == []
    assert host.addrs["eth0"] == {"10.30.110.5/24", "10.30.110.53/24"}
    assert ("public", "80/tcp") not in host.ports and ("public", "80/tcp") not in host.permanent
    assert ("public", "53/udp") in host.ports and host.pods == {"dciab-services"}   # pods aren't pruned
    assert (conf / "created").read_text().splitlines() == [
        "address eth0 10.30.110.53/24", "port public 53/udp", "port public 53/tcp", "pod dciab-services"]


def test_prune_moves_ports_when_the_zone_changes(tmp_path, host):
    conf = conf_with(tmp_path, ["port public 53/udp", "port public 53/tcp"])
    host.ports |= {("internal", "53/udp")}
    lifecycle.prune(conf, cli.wanted(load_example("proxmox"), "eth0", "internal"), say=lambda m: None)
    assert ("public", "53/udp") not in host.ports and ("public", "53/tcp") not in host.ports


def test_prune_keeps_ports_when_firewalld_is_off(tmp_path, host):
    host.firewalld = False
    conf = conf_with(tmp_path, ["port public 80/tcp", "address eth0 10.30.110.54/24"])
    lifecycle.prune(conf, cli.wanted(load_example("proxmox"), "eth0", ""), say=lambda m: None)
    assert (conf / "created").read_text() == "port public 80/tcp\naddress eth0 10.30.110.54/24\n"


def test_prune_nothing_changed_runs_no_command(tmp_path, host):
    doc = bare()
    lines = sorted(cli.wanted(doc, "eth0", "public")) + ["pod dciab-services"]
    conf = conf_with(tmp_path, lines)
    lifecycle.prune(conf, cli.wanted(doc, "eth0", "public"), say=lambda m: None)
    assert host.calls == []


def test_record_file_is_private(tmp_path, host):
    conf = conf_with(tmp_path, FULL)
    host.fail = {"ip addr del"}
    lifecycle.down(conf, lambda m: None)
    assert (conf / "created").stat().st_mode & 0o777 == 0o600


def test_wanted_ports_follow_the_config():
    assert render.ports(bare()) == ["53/udp", "53/tcp", "80/tcp", "123/udp"]
    no_ntp = bare()
    no_ntp["spec"]["time"] = {"servedBy": "site", "servers": ["10.30.0.123"]}
    assert render.ports(no_ntp) == ["53/udp", "53/tcp", "80/tcp"]
    assert render.ports(load_example("proxmox")) == ["53/udp", "53/tcp"]
    w = cli.wanted(bare(), "eth0", "")
    assert w == {"address eth0 10.30.110.53/24", "address eth0 10.30.110.54/24"}   # no zone: no ports


def test_forwarding_only_for_the_site_networks():
    doc = load_example("vsphere")
    core = render.corefile(doc)
    nets = render.client_networks(doc)
    assert nets[0] == doc["spec"]["network"]["nodes"]["cidr"] and nets[-1] == "127.0.0.0/8"
    for c in doc["spec"]["clusters"].values():
        if isinstance(c, dict) and c.get("podCIDR"):
            assert c["podCIDR"] in nets
    assert f"acl {{\n        allow net {' '.join(nets)}\n        block\n    }}\n    forward ." in core
    # an isolated site forwards nothing, so there is no forwarding block to guard
    assert "acl" not in render.corefile(bare())


def test_lifecycle_cli(tmp_path, host, capsys):
    conf = conf_with(tmp_path, FULL)
    assert lifecycle.main(["down", str(conf)]) == 0
    assert "down: removed pod dciab-services" in capsys.readouterr().out
    assert lifecycle.main(["up"]) == 2
    host.fail = {"podman pod rm"}
    conf2 = conf_with(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, ["pod dciab-services"])
    host.pods = {"dciab-services"}
    assert lifecycle.main(["down", str(conf2)]) == 1
