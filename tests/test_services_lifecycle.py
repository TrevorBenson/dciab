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
        self.offline = True   # firewall-offline-cmd is installed
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
        elif cmd[0] == "firewall-offline-cmd":
            if not self.offline:
                raise FileNotFoundError(cmd[0])
            if self.firewalld:
                rc = 1   # it refuses while firewalld runs
            else:
                zone, (op, port) = cmd[1].split("=")[1], cmd[2].lstrip("-").split("=")
                if op == "query-port":
                    rc = 0 if (zone, port) in self.permanent else 1
                elif op == "remove-port":
                    self.permanent.discard((zone, port))
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


def test_down_without_firewalld_closes_permanent_ports_offline(tmp_path, host):
    """firewalld stopped: runtime rules are gone with it, and the permanent ones DCiaB opened are
    removed from the zone files with firewall-offline-cmd, so nothing outlives DCiaB (#230 4)."""
    host.firewalld = False
    conf = conf_with(tmp_path, FULL)
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert host.permanent == set()
    assert any("permanent; firewalld isn't running" in m for m in msgs)
    assert not (conf / "created").exists()


def test_down_without_firewalld_or_offline_tool_keeps_the_port_lines(tmp_path, host):
    host.firewalld = False
    host.offline = False
    conf = conf_with(tmp_path, ["port public 53/udp permanent", "port public 53/tcp runtime", "pod dciab-services"])
    msgs = []
    left = lifecycle.down(conf, msgs.append)
    assert left == ["port public 53/udp permanent"]               # still listed, for a later --down
    assert (conf / "created").read_text() == "port public 53/udp permanent\n"
    assert any("stays listed" in m for m in msgs)
    assert ("public", "53/udp") in host.permanent


def test_only_the_scope_dciab_opened_is_closed(tmp_path, host):
    """The operator's permanent rule for 53/udp (DCiaB only opened it at runtime) survives
    --down; a legacy line without a scope still closes both (#230 5)."""
    conf = conf_with(tmp_path, ["port public 53/udp runtime", "port public 80/tcp"])
    assert lifecycle.down(conf, lambda m: None) == []
    assert ("public", "53/udp") not in host.ports and ("public", "53/udp") in host.permanent
    assert ("public", "80/tcp") not in host.ports and ("public", "80/tcp") not in host.permanent


def test_an_unknown_scope_is_not_understood(tmp_path, host):
    conf = conf_with(tmp_path, ["port public 53/udp sometimes"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert any("doesn't understand" in m for m in msgs)


def test_down_and_prune_take_the_services_lock(tmp_path, host, monkeypatch):
    """dciab seed --remove and dciab services share the record: one at a time (#230 8)."""
    import fcntl
    import os
    lock = tmp_path / "services.lock"
    monkeypatch.setenv("DCIAB_SERVICES_LOCK", str(lock))
    monkeypatch.delenv("DCIAB_SERVICES_LOCKED", raising=False)
    conf = conf_with(tmp_path, FULL)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(lifecycle.Busy):
            lifecycle.down(conf, lambda m: None)
        with pytest.raises(lifecycle.Busy):
            lifecycle.prune(conf, set(), say=lambda m: None)
        assert host.calls == [] and (conf / "created").exists()
        monkeypatch.setenv("DCIAB_SERVICES_LOCKED", "1")      # the caller holds it (services.sh)
        assert lifecycle.down(conf, lambda m: None) == []
    finally:
        os.close(fd)
    monkeypatch.delenv("DCIAB_SERVICES_LOCKED")
    assert lifecycle.down(conf, lambda m: None) == []         # free again


def test_down_with_an_empty_record_but_installed_units_says_so(tmp_path, host, monkeypatch, capsys):
    """--down from a config whose state folder isn't the one the units run from (#230 9)."""
    quadlet = tmp_path / "quadlet"
    quadlet.mkdir()
    other = tmp_path / "elsewhere" / "state" / "services"
    (quadlet / "dciab-services.kube").write_text(f"[Kube]\nYaml={other}/pod.yaml\n")
    monkeypatch.setenv("DCIAB_QUADLET_DIR", str(quadlet))
    conf = tmp_path / "state" / "services"
    conf.mkdir(parents=True)
    assert lifecycle.main(["down", str(conf)]) == 1
    err = capsys.readouterr().err
    assert f"--state {other.parent}" in err
    # the same state folder, but the record is gone: still not a silent success
    (quadlet / "dciab-services.kube").write_text(f"[Kube]\nYaml={conf}/pod.yaml\n")
    assert lifecycle.main(["down", str(conf)]) == 1
    assert "the record is gone" in capsys.readouterr().err
    # a record: --down does its job
    (conf / "created").write_text("pod dciab-services\n")
    assert lifecycle.main(["down", str(conf)]) == 0


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


def test_prune_without_firewalld(tmp_path, host):
    """firewalld stopped: a runtime line has nothing left to close and goes; a permanent one is
    closed offline; with no offline tool it stays listed."""
    host.firewalld = False
    conf = conf_with(tmp_path, ["port public 80/tcp runtime", "port public 80/tcp permanent",
                                "address eth0 10.30.110.54/24"])
    assert lifecycle.prune(conf, cli.wanted(load_example("proxmox"), "eth0", ""), say=lambda m: None) == []
    assert ("public", "80/tcp") not in host.permanent
    host.offline = False
    host.permanent.add(("public", "80/tcp"))
    conf2 = conf_with(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, ["port public 80/tcp permanent"])
    assert lifecycle.prune(conf2, set(), say=lambda m: None) == ["port public 80/tcp permanent"]
    assert (conf2 / "created").read_text() == "port public 80/tcp permanent\n"


def test_wanted_keeps_scoped_and_legacy_port_lines(tmp_path, host):
    doc = bare()
    w = cli.wanted(doc, "eth0", "public")
    assert {"port public 53/udp", "port public 53/udp runtime", "port public 53/udp permanent"} <= w
    conf = conf_with(tmp_path, ["port public 53/udp runtime", "port public 53/udp permanent", "port public 80/tcp"])
    lifecycle.prune(conf, w, say=lambda m: None)
    assert host.calls == []


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
    # no cache: CoreDNS answers from its cache before acl runs, to anyone
    assert "cache" not in core
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


# Reboot survival (#81): the units that bring the addresses and the pod back.

from dciab_services import units  # noqa: E402


def test_units_render(tmp_path):
    conf = tmp_path / "state" / "services"
    got = units.render(conf, "eth0", 24, ["10.30.110.53", "10.30.110.54"],
                       systemd_dir=tmp_path / "systemd", quadlet_dir=tmp_path / "quadlet")
    addr = got[tmp_path / "systemd" / "dciab-services-addresses.service"]
    kube = got[tmp_path / "quadlet" / "dciab-services.kube"]
    assert "ExecStart=ip addr replace 10.30.110.53/24 dev eth0\nExecStart=ip addr replace 10.30.110.54/24 dev eth0" in addr
    assert "Type=oneshot" in addr and "RemainAfterExit=yes" in addr and "Before=dciab-services.service" in addr
    assert "WantedBy=multi-user.target" in addr and "After=network-online.target" in addr
    assert f"Yaml={conf}/pod.yaml" in kube and f"RequiresMountsFor={conf}" in kube
    assert "ExecStartPre=ip addr replace 10.30.110.53/24 dev eth0" in kube
    with_mounts = units.render(conf, "eth0", 24, ["10.30.110.53"], systemd_dir=tmp_path / "s",
                               quadlet_dir=tmp_path / "q", mounts=[Path("/isos"), Path("/graph"), conf])
    assert f"RequiresMountsFor={conf} /isos /graph\n" in with_mounts[tmp_path / "q" / "dciab-services.kube"]
    assert "Requires=dciab-services-addresses.service" in kube and "WantedBy=multi-user.target" in kube


def test_units_written_once_and_recorded_first(tmp_path):
    conf = tmp_path / "services"
    conf.mkdir()
    u = units.render(conf, "eth0", 24, ["10.0.0.53", "10.0.0.54"],
                     systemd_dir=tmp_path / "systemd", quadlet_dir=tmp_path / "quadlet")
    assert sorted(units.write(conf, u)) == sorted(u)
    assert sorted(lifecycle.read(conf)) == sorted([["unit", str(p)] for p in u])
    assert units.write(conf, u) == []                     # unchanged: nothing written
    for p in u:
        assert p.stat().st_mode & 0o777 == 0o644
    # another DNS service IP rewrites both (the pod's unit re-adds the IPs on each start),
    # and the record isn't duplicated
    u2 = units.render(conf, "eth0", 24, ["10.0.0.53", "10.0.0.55"],
                      systemd_dir=tmp_path / "systemd", quadlet_dir=tmp_path / "quadlet")
    assert sorted(p.name for p in units.write(conf, u2)) == ["dciab-services-addresses.service", "dciab-services.kube"]
    assert len(lifecycle.read(conf)) == 2


def _apply(cfg, tmp_path, *extra):
    from conftest import ROOT
    return cli.main(["--root", str(ROOT), "--state", str(tmp_path / "st"), "--nic", "eth0",
                     *extra, str(cfg), "apply"])


def test_apply_writes_files_and_units_together(tmp_path, monkeypatch, capsys):
    from conftest import ROOT
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    cfg = bare_site(tmp_path)
    assert cli.main(["--root", str(ROOT), "--state", str(tmp_path / "st"), str(cfg), "apply"]) == 2   # needs --nic
    capsys.readouterr()
    assert _apply(cfg, tmp_path, "--units", "--graphroot", "/var/lib/containers/storage") == 0
    out = capsys.readouterr().out
    kube = tmp_path / "quadlet" / "dciab-services.kube"
    assert "wrote " in out and kube.is_file() and (tmp_path / "st" / "services" / "pod.yaml").is_file()
    text = kube.read_text()
    assert "/var/lib/containers/storage" in text and "host-isos" in text   # finding 14
    assert "ExecStartPre=ip addr replace 10.30.110.53/24 dev eth0" in text  # finding 7
    assert _apply(cfg, tmp_path, "--units", "--graphroot", "/var/lib/containers/storage") == 0
    assert capsys.readouterr().out == ""                                    # unchanged: silent


def test_a_run_stopped_before_the_pod_step_leaves_the_old_config_whole(tmp_path, monkeypatch, capsys):
    """The DNS service IP changes, but the run stops after render/address (--until, a failure):
    the live pod.yaml, Corefile and both units still describe the old IP, so a reboot or a
    crash restart brings back a consistent old config. apply then switches all of them
    together (#230 1)."""
    import yaml
    from conftest import ROOT
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    cfg = bare_site(tmp_path)
    conf = tmp_path / "st" / "services"
    run = ["--root", str(ROOT), "--state", str(tmp_path / "st")]
    assert cli.main(run + [str(cfg), "write"]) == 0
    assert _apply(cfg, tmp_path, "--units") == 0
    capsys.readouterr()
    addr_unit = tmp_path / "systemd" / "dciab-services-addresses.service"

    doc = yaml.safe_load(cfg.read_text())
    doc["spec"]["network"]["dnsServiceIPs"][1] = "10.30.110.55"
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert cli.main(run + [str(cfg), "write"]) == 0                       # render step
    assert "(changes in the pod step)" in capsys.readouterr().out
    live = (conf / "coredns" / "Corefile").read_text() + (conf / "pod.yaml").read_text()
    units_now = addr_unit.read_text() + (tmp_path / "quadlet" / "dciab-services.kube").read_text()
    assert "10.30.110.54" in live and "10.30.110.55" not in live           # live files: still old
    assert "10.30.110.54" in units_now and "10.30.110.55" not in units_now   # units: still old
    assert "10.30.110.55" in (conf / ".staged" / "coredns" / "Corefile").read_text()

    assert _apply(cfg, tmp_path, "--units") == 0                           # pod step
    live = (conf / "coredns" / "Corefile").read_text() + (conf / "pod.yaml").read_text()
    units_now = addr_unit.read_text() + (tmp_path / "quadlet" / "dciab-services.kube").read_text()
    assert "10.30.110.55" in live and "10.30.110.54" not in live
    assert "10.30.110.55" in units_now and "10.30.110.54" not in units_now
    assert not (conf / ".staged").exists()


def test_persistent_needs_systemd_and_quadlet(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(units, "QUADLET_BINARIES", (str(tmp_path / "missing"),))
    assert not units.persistent()
    q = tmp_path / "quadlet"
    q.write_text("#!/bin/sh\n")
    q.chmod(0o755)
    monkeypatch.setattr(units, "QUADLET_BINARIES", (str(q),))
    import shutil
    monkeypatch.setattr(shutil, "which", lambda t: "/usr/bin/systemctl")
    assert units.persistent()
