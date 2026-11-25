"""dciab services lifecycle (#164): what it added to the host is recorded, pruned when the
config no longer wants it, and removed by --down, against a fake host."""

import copy
import subprocess
from pathlib import Path

import pytest

from conftest import load_example

from dciab_services import cli, lifecycle, render


REAL_RUN = subprocess.run


class FakeHost:
    """podman, ip, firewall-cmd and systemctl over a little in-memory host. Other commands
    (openssl from the config checks, say) run for real."""

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
        self.error = set()  # commands (joined) that fail with an error, not a "no" (exit 125)
        self.takes_along = {}   # cidr -> other cidrs the kernel drops with it (secondaries)
        self.dynamic = {}   # cidr -> valid lifetime (s): a DHCP-style address

    def __call__(self, cmd, check=False, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        rc, out = 0, ""
        if any(f in joined for f in self.error):
            rc = 125
        elif any(f in joined for f in self.fail):
            rc = 1
        elif cmd[:3] == ["podman", "pod", "exists"]:
            rc = 0 if cmd[3] in self.pods else 1
        elif cmd[:3] == ["podman", "pod", "rm"]:
            self.pods.discard(cmd[-1])
        elif cmd[:2] == ["ip", "-o"]:
            out = " ".join(f"2: {cmd[-1]} inet {a} scope global" for a in sorted(self.addrs.get(cmd[-1], ())))
        elif cmd[:2] == ["ip", "-j"]:
            import json
            info = []
            for a in sorted(self.addrs.get(cmd[-1], ())):
                ip, plen = a.split("/")
                life = self.dynamic.get(a, 4294967295)
                info.append({"family": "inet", "local": ip, "prefixlen": int(plen), "scope": "global",
                             "label": cmd[-1], "valid_life_time": life, "preferred_life_time": life})
            out = json.dumps([{"ifname": cmd[-1], "addr_info": info}])
        elif cmd[:3] == ["ip", "addr", "add"]:
            self.addrs.setdefault(cmd[5], set()).add(cmd[3])
        elif cmd[:3] == ["ip", "addr", "del"]:
            self.addrs[cmd[-1]].discard(cmd[3])
            for other in self.takes_along.get(cmd[3], ()):
                self.addrs[cmd[-1]].discard(other)
        elif cmd[:3] == ["ip", "addr", "replace"]:
            self.addrs.setdefault(cmd[-1], set()).add(cmd[3])
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
            self.calls.pop()
            return REAL_RUN(cmd, check=check, **kw)
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd, out, "boom")
        return subprocess.CompletedProcess(cmd, rc, out, "")


BOOT = "boot-1"


@pytest.fixture
def host(monkeypatch, tmp_path):
    h = FakeHost()
    monkeypatch.setattr(lifecycle.subprocess, "run", h)
    boot = tmp_path / "boot_id"
    boot.write_text(BOOT + "\n")
    monkeypatch.setattr(lifecycle, "BOOT_ID", str(boot))
    sysnet = tmp_path / "sys-class-net"            # the fake host's NICs, whatever the machine has
    for nic in ("eth0", "dciab0"):
        (sysnet / nic).mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "SYS_NET", str(sysnet))
    return h


def conf_with(tmp_path, lines, applied=True):
    conf = tmp_path / "services"
    conf.mkdir()
    (conf / "created").write_text("".join(f"{line}\n" for line in lines))
    if applied:
        (conf / "applied").write_text("fp\n")
    return conf


FULL = ["address eth0 10.30.110.53/24", "address eth0 10.30.110.54/24",
        "port public 53/udp runtime", "port public 53/udp permanent", "port public 53/tcp runtime",
        "port public 80/tcp runtime", "port public 80/tcp permanent", "pod dciab-services"]


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


def test_down_keeps_lines_whose_state_cant_be_read(tmp_path, host):
    """A query that fails for another reason than "not there" (a missing tool, an error) is not
    absence: the line stays listed and is reported, and down() says it left something behind
    (CodeRabbit on #230)."""
    host.error = {"podman pod exists", "ip -j", "--query-port=53/udp"}
    conf = conf_with(tmp_path, FULL)
    msgs = []
    left = lifecycle.down(conf, msgs.append)
    assert "pod dciab-services" in left
    assert "address eth0 10.30.110.53/24" in left and "address eth0 10.30.110.54/24" in left
    assert "port public 53/udp runtime" in left and "port public 53/udp permanent" in left
    assert "port public 80/tcp runtime" not in left                       # that one was readable
    assert host.pods == {"dciab-services"} and "10.30.110.53/24" in host.addrs["eth0"]
    assert any("couldn't tell whether pod dciab-services is there" in m for m in msgs)
    assert set((conf / "created").read_text().splitlines()) == set(left)
    host.error = set()                                                   # once it can be read
    assert lifecycle.down(conf, lambda m: None) == []
    assert not (conf / "created").exists()


def test_a_missing_ip_tool_is_not_absence(tmp_path, monkeypatch):
    (tmp_path / "sys-class-net" / "eth0").mkdir(parents=True)   # the NIC is there; ip isn't
    monkeypatch.setattr(lifecycle, "SYS_NET", str(tmp_path / "sys-class-net"))
    real = lifecycle.query
    monkeypatch.setattr(lifecycle, "query", lambda cmd: lifecycle.subprocess.CompletedProcess(
        cmd, 127, "", "ip: not found") if cmd[0] == "ip" else real(cmd))
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    assert lifecycle.down(conf, lambda m: None) == ["address eth0 10.30.110.53/24"]
    assert (conf / "created").read_text() == "address eth0 10.30.110.53/24\n"


def test_removing_an_address_puts_back_what_it_took_along(tmp_path, host):
    """Deleting a recorded primary can take the operator's unrecorded secondary along: it is
    put back as it was (a DHCP-style lifetime included), and only the recorded one goes."""
    host.addrs["eth0"] |= {"10.30.110.60/24"}
    host.dynamic = {"10.30.110.60/24": 3600}
    host.takes_along = {"10.30.110.53/24": ["10.30.110.60/24", "10.30.110.5/24"]}
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert host.addrs["eth0"] == {"10.30.110.5/24", "10.30.110.54/24", "10.30.110.60/24"}
    assert ["ip", "addr", "add", "10.30.110.60/24", "dev", "eth0",
            "valid_lft", "3600", "preferred_lft", "3600"] in host.calls
    assert ["ip", "addr", "add", "10.30.110.5/24", "dev", "eth0"] in host.calls
    assert any("re-added 10.30.110.60/24 on eth0 (removing 10.30.110.53/24 took it along)" in m
               for m in msgs)


def test_a_restore_that_fails_is_kept_for_the_next_run(tmp_path, host):
    """Removing the recorded primary takes the operator's address along and putting it back
    fails: a restore line stays in the record, and the next run puts it back even though the
    primary is gone by then (CodeRabbit on #230)."""
    host.addrs["eth0"] = {"10.30.110.53/24", "10.30.110.60/24"}
    host.dynamic = {"10.30.110.60/24": 3600}
    host.takes_along = {"10.30.110.53/24": ["10.30.110.60/24"]}
    host.fail = {"ip addr add 10.30.110.60/24"}
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    left = lifecycle.down(conf, lambda m: None)
    restore = f"restore eth0 10.30.110.60/24 boot={BOOT} valid_lft 3600 preferred_lft 3600"
    assert restore in left and "10.30.110.53/24" not in host.addrs["eth0"]
    assert restore in (conf / "created").read_text()
    host.fail = set()                                       # the next run
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert "10.30.110.60/24" in host.addrs["eth0"] and not (conf / "created").exists()
    assert any("re-added 10.30.110.60/24 on eth0" in m for m in msgs)


def test_an_unreadable_interface_after_the_removal_keeps_every_restore(tmp_path, host, monkeypatch):
    """The addresses can't be read after the removal: every other address on the interface is
    left in the record as a restore line (written before the removal); the next run checks
    each and puts back the ones that are missing."""
    host.takes_along = {"10.30.110.53/24": ["10.30.110.54/24"]}
    real, calls = lifecycle.addresses, []

    def flaky(nic):
        calls.append(nic)
        return None if len(calls) == 2 else real(nic)      # the read right after the removal
    monkeypatch.setattr(lifecycle, "addresses", flaky)
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    left = lifecycle.down(conf, lambda m: None)
    assert f"restore eth0 10.30.110.5/24 boot={BOOT}" in left
    assert f"restore eth0 10.30.110.54/24 boot={BOOT}" in left
    assert f"restore eth0 10.30.110.54/24 boot={BOOT}" in (conf / "created").read_text()
    assert lifecycle.down(conf, lambda m: None) == []
    assert host.addrs["eth0"] == {"10.30.110.5/24", "10.30.110.54/24"}
    assert not (conf / "created").exists()


def test_prune_finishes_restores_a_previous_run_left(tmp_path, host):
    host.addrs["eth0"].discard("10.30.110.54/24")
    conf = conf_with(tmp_path, ["address eth0 10.30.110.54/24", f"restore eth0 10.30.110.60/24 boot={BOOT}"])
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.53", "10.30.110.54"]
    assert lifecycle.prune(conf, cli.wanted(doc, "eth0", "public"), say=lambda m: None) == []
    assert "10.30.110.60/24" in host.addrs["eth0"]
    assert (conf / "created").read_text() == "address eth0 10.30.110.54/24\n"


def test_a_failed_removal_lists_nothing_to_restore(tmp_path, host):
    host.fail = {"ip addr del 10.30.110.53/24"}
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    assert lifecycle.down(conf, lambda m: None) == ["address eth0 10.30.110.53/24"]
    assert (conf / "created").read_text() == "address eth0 10.30.110.53/24\n"


def test_a_unit_that_wont_stop_stays_and_holds_back_the_rest(tmp_path, host, monkeypatch, capsys):
    """systemctl stop failing: the unit file and its line stay, and so do the pod, the addresses
    and the ports it would bring back; down() reports them and --down exits 1."""
    unit = tmp_path / "dciab-services.kube"
    unit.write_text("[Kube]\n")
    host.services = {"dciab-services.service"}
    host.fail = {"systemctl stop dciab-services.service"}
    conf = conf_with(tmp_path, FULL + [f"unit {unit}"])
    left = lifecycle.down(conf, lambda m: None)
    assert unit.exists() and f"unit {unit}" in left
    assert set(left) == set(FULL + [f"unit {unit}"])                      # nothing else was undone
    assert host.pods == {"dciab-services"} and "10.30.110.53/24" in host.addrs["eth0"]
    assert ("public", "53/udp") in host.ports
    monkeypatch.setenv("DCIAB_QUADLET_DIR", str(tmp_path / "none"))
    assert lifecycle.main(["down", str(conf)]) == 1
    assert "left these behind" in capsys.readouterr().err
    host.fail = {"systemctl disable"}                                      # a .service that won't disable
    svc = tmp_path / "dciab-services-addresses.service"
    svc.write_text("[Unit]\n")
    host.services = {"dciab-services-addresses.service"}
    conf2 = conf_with(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                      [f"unit {svc}"])
    assert lifecycle.down(conf2, lambda m: None) == [f"unit {svc}"] and svc.exists()


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
    --down (#230 5). A line without a scope comes from older records, which only recorded ports
    opened at runtime: it closes the runtime rule only, never a permanent one (#230 N2)."""
    conf = conf_with(tmp_path, ["port public 53/udp runtime", "port public 80/tcp"])
    assert lifecycle.down(conf, lambda m: None) == []
    assert ("public", "53/udp") not in host.ports and ("public", "53/udp") in host.permanent
    assert ("public", "80/tcp") not in host.ports and ("public", "80/tcp") in host.permanent


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
        # The variable alone isn't trusted: fd 9 (here: LOCK_FD) must be the lock, held by us.
        monkeypatch.setenv("DCIAB_SERVICES_LOCKED", "1")
        monkeypatch.setattr(lifecycle, "LOCK_FD", fd + 1000)  # not open: not trusted
        with pytest.raises(lifecycle.Busy):
            lifecycle.down(conf, lambda m: None)
        other = os.open(tmp_path / "not-the-lock", os.O_WRONLY | os.O_CREAT)
        monkeypatch.setattr(lifecycle, "LOCK_FD", other)      # another file: not trusted
        with pytest.raises(lifecycle.Busy):
            lifecycle.down(conf, lambda m: None)
        os.close(other)
        monkeypatch.setattr(lifecycle, "LOCK_FD", fd)         # the caller's own lock (services.sh)
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
        "address eth0 10.30.110.53/24", "port public 53/udp runtime", "port public 53/udp permanent",
        "port public 53/tcp runtime", "pod dciab-services"]


def test_prune_moves_ports_when_the_zone_changes(tmp_path, host):
    conf = conf_with(tmp_path, ["port public 53/udp", "port public 53/tcp"])
    host.ports |= {("internal", "53/udp")}
    lifecycle.prune(conf, cli.wanted(load_example("proxmox"), "eth0", "internal"), say=lambda m: None)
    assert ("public", "53/udp") not in host.ports and ("public", "53/tcp") not in host.ports


def test_prune_without_firewalld_leaves_the_ports_alone(tmp_path, host):
    """firewalld stopped and the config unchanged: the zone can't be told, so every port would
    look unwanted. prune leaves the port lines and the rules alone (runtime and permanent) and
    says so; addresses are still pruned (#230 N1)."""
    host.firewalld = False
    doc = bare()                                     # wants 53/udp, 53/tcp, 80/tcp, 123/udp
    lines = ["address eth0 10.30.110.53/24", "address eth0 10.30.110.54/24",
             "port public 53/udp runtime", "port public 53/udp permanent",
             "port public 80/tcp runtime", "port public 80/tcp permanent", "port public 53/tcp"]
    conf = conf_with(tmp_path, lines)
    msgs, notes = [], []
    assert lifecycle.prune(conf, cli.wanted(doc, "eth0", ""), say=msgs.append, note=notes.append) == []
    assert (conf / "created").read_text().splitlines() == lines        # nothing dropped
    assert host.permanent == {("public", "53/udp"), ("public", "80/tcp")}
    assert not [c for c in host.calls if "--remove-port" in " ".join(c)]
    # A note, not a change: services.sh would restart the pod for any change line (#230 N8).
    assert any("firewall ports weren't checked" in m for m in notes)
    assert msgs == []
    # the address kind is still pruned
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.53", "10.30.110.55"]
    assert lifecycle.prune(conf, cli.wanted(doc, "eth0", ""), say=lambda m: None) == []
    assert "10.30.110.54/24" not in host.addrs["eth0"]
    assert sum(1 for line in (conf / "created").read_text().splitlines() if line.startswith("port")) == 5


def test_prune_puts_back_a_wanted_address_its_removal_took_along(tmp_path, host):
    """Removing an address can take others in its subnet along (secondaries go with their
    primary unless promote_secondaries is set): prune re-adds the recorded ones the config
    still wants (#230 N6)."""
    host.takes_along = {"10.30.110.53/24": ["10.30.110.54/24"]}
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24", "address eth0 10.30.110.54/24"])
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.54", "10.30.110.55"]
    msgs = []
    assert lifecycle.prune(conf, cli.wanted(doc, "eth0", "public"), say=msgs.append) == []
    assert "10.30.110.53/24" not in host.addrs["eth0"] and "10.30.110.54/24" in host.addrs["eth0"]
    assert ["ip", "addr", "add", "10.30.110.54/24", "dev", "eth0"] in host.calls
    assert any("re-added 10.30.110.54/24" in m for m in msgs)
    host.calls.clear()                                # nothing taken along: no re-add
    conf2 = conf_with(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                      ["address eth0 10.30.110.54/24"])
    host.addrs["eth0"].add("10.30.110.54/24")
    lifecycle.prune(conf2, cli.wanted(doc, "eth0", "public"), say=lambda m: None)
    assert not [c for c in host.calls if c[:3] in (["ip", "addr", "replace"], ["ip", "addr", "add"])]


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
    # The pod's unit adds the addresses itself, so it only Wants= the addresses unit; both retry
    # every 5 s with no start limit, so an interface that appears late is picked up (#230 N4).
    assert "Wants=dciab-services-addresses.service" in kube and "Requires=" not in kube.replace("RequiresMountsFor", "")
    assert "WantedBy=multi-user.target" in kube
    for unit in (addr, kube):
        assert "RestartSec=5" in unit and "StartLimitIntervalSec=0" in unit and "Restart=on-failure" in unit
    both = units.render(conf, "eth0", 24, ["10.30.110.53"], systemd_dir=tmp_path / "s",
                        quadlet_dir=tmp_path / "q", extra=["10.30.110.54/24", "10.30.110.53/24"])
    assert both[tmp_path / "s" / "dciab-services-addresses.service"].count("ExecStart=ip addr replace") == 2


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


def record_addresses(tmp_path, *ips):
    """What the address step records before the pod step (dciab services added them)."""
    for ip in ips:
        lifecycle.record(tmp_path / "st" / "services", ["address", "eth0", f"{ip}/24"])


def test_apply_writes_files_and_units_together(tmp_path, monkeypatch, capsys, host):
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
    record_addresses(tmp_path, "10.30.110.53", "10.30.110.54")
    assert _apply(cfg, tmp_path, "--units", "--graphroot", "/var/lib/containers/storage") == 0
    out = capsys.readouterr().out
    kube = tmp_path / "quadlet" / "dciab-services.kube"
    assert "wrote " in out and kube.is_file() and (tmp_path / "st" / "services" / "pod.yaml").is_file()
    text = kube.read_text()
    assert "/var/lib/containers/storage" in text and "host-isos" in text   # finding 14
    assert "ExecStartPre=ip addr replace 10.30.110.53/24 dev eth0" in text  # finding 7
    assert _apply(cfg, tmp_path, "--units", "--graphroot", "/var/lib/containers/storage") == 0
    assert capsys.readouterr().out == ""                                    # unchanged: silent
    # firewalld stopped (no --zone) with ports recorded: the note goes to stderr, so stdout,
    # which services.sh reads as changes, stays empty and the pod isn't restarted (#230 N8)
    lifecycle.record(tmp_path / "st" / "services", ["port", "public", "53/udp", "permanent"])
    assert _apply(cfg, tmp_path, "--units", "--graphroot", "/var/lib/containers/storage") == 0
    got = capsys.readouterr()
    assert got.out == "" and "firewall ports weren't checked" in got.err


def test_a_run_stopped_before_the_pod_step_leaves_the_old_config_whole(tmp_path, monkeypatch, capsys, host):
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
    record_addresses(tmp_path, "10.30.110.53", "10.30.110.54")
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

    record_addresses(tmp_path, "10.30.110.55")                             # address step
    host.addrs["eth0"].add("10.30.110.55/24")
    assert _apply(cfg, tmp_path, "--units") == 0                           # pod step
    live = (conf / "coredns" / "Corefile").read_text() + (conf / "pod.yaml").read_text()
    units_now = addr_unit.read_text() + (tmp_path / "quadlet" / "dciab-services.kube").read_text()
    assert "10.30.110.55" in live and "10.30.110.54" not in live
    assert "10.30.110.55" in units_now and "10.30.110.54" not in units_now
    assert not (conf / ".staged").exists()


def test_a_crash_while_the_pod_moves_leaves_units_for_every_live_address(tmp_path, monkeypatch, capsys):
    """The DNS service IP changes and the run dies between writing the units and moving the
    files (or while moving them): the units bring up the old and the new addresses, so
    whichever pod.yaml is live after a reboot finds its addresses. The next run drops the old
    one from the units (#230 N3)."""
    import yaml
    from conftest import ROOT
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    monkeypatch.setattr(lifecycle, "prune", lambda *a, **k: [])
    cfg = bare_site(tmp_path)
    conf = tmp_path / "st" / "services"
    run = ["--root", str(ROOT), "--state", str(tmp_path / "st")]
    assert cli.main(run + [str(cfg), "write"]) == 0
    assert _apply(cfg, tmp_path, "--units") == 0
    for ip in ("10.30.110.53", "10.30.110.54"):        # what the address step recorded
        lifecycle.record(conf, ["address", "eth0", f"{ip}/24"])
    doc = yaml.safe_load(cfg.read_text())
    doc["spec"]["network"]["dnsServiceIPs"][1] = "10.30.110.55"
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert cli.main(run + [str(cfg), "write"]) == 0
    record_addresses(tmp_path, "10.30.110.55")                             # address step
    capsys.readouterr()

    def crash(*a, **k):
        raise KeyboardInterrupt("power loss")
    real = cli.apply_files
    monkeypatch.setattr(cli, "apply_files", crash)
    with pytest.raises(KeyboardInterrupt):
        _apply(cfg, tmp_path, "--units")
    addr_unit = (tmp_path / "systemd" / "dciab-services-addresses.service").read_text()
    kube = (tmp_path / "quadlet" / "dciab-services.kube").read_text()
    assert "10.30.110.54" in (conf / "coredns" / "Corefile").read_text()   # the live config: still old
    for unit in (addr_unit, kube):
        assert "10.30.110.54/24" in unit and "10.30.110.55/24" in unit   # both addresses
    assert "keeps the old addresses until the pod moves" in capsys.readouterr().out
    monkeypatch.setattr(cli, "apply_files", real)
    assert _apply(cfg, tmp_path, "--units") == 0
    addr_unit = (tmp_path / "systemd" / "dciab-services-addresses.service").read_text()
    assert "10.30.110.55/24" in addr_unit and "10.30.110.54/24" not in addr_unit


def test_units_assert_only_addresses_dciab_added(tmp_path, monkeypatch, capsys, host):
    """A DNS service IP that is the operator's address (recorded by nobody, any prefix) is
    never in the units' ip addr replace lines; with none of ours the addresses unit is a no-op
    (CodeRabbit on #230)."""
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    cfg = bare_site(tmp_path)
    record_addresses(tmp_path, "10.30.110.54")          # .53 is the operator's (say, as /25)
    assert _apply(cfg, tmp_path, "--units") == 0
    for unit in (tmp_path / "systemd" / "dciab-services-addresses.service",
                 tmp_path / "quadlet" / "dciab-services.kube"):
        text = unit.read_text()
        assert "10.30.110.54/24" in text and "10.30.110.53" not in text
    (tmp_path / "st" / "services" / "created").unlink()  # none of ours
    assert _apply(cfg, tmp_path, "--units") == 0
    text = (tmp_path / "systemd" / "dciab-services-addresses.service").read_text()
    assert "ip addr replace" not in text and "ExecStart=/usr/bin/true" in text
    assert "ExecStartPre" not in (tmp_path / "quadlet" / "dciab-services.kube").read_text()


def test_a_crash_while_moving_to_another_nic_keeps_the_old_nics_addresses(tmp_path, monkeypatch, capsys, host):
    """The bootstrap interface changes (eth0 to eth1) and the run dies before the files move:
    the units still bring up the old addresses on eth0, which the live pod.yaml binds, as well
    as the new ones on eth1 (CodeRabbit on #230)."""
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    monkeypatch.setattr(lifecycle, "prune", lambda *a, **k: [])
    cfg = bare_site(tmp_path)
    conf = tmp_path / "st" / "services"
    record_addresses(tmp_path, "10.30.110.53", "10.30.110.54")          # on eth0
    assert _apply(cfg, tmp_path, "--units") == 0
    for ip in ("10.30.110.53", "10.30.110.54"):                          # the address step on eth1
        lifecycle.record(conf, ["address", "eth1", f"{ip}/24"])
    monkeypatch.setattr(cli, "apply_files", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _apply(cfg, tmp_path, "--units", "--nic", "eth1")
    for unit in (tmp_path / "systemd" / "dciab-services-addresses.service",
                 tmp_path / "quadlet" / "dciab-services.kube"):
        text = unit.read_text()
        for ip in ("10.30.110.53", "10.30.110.54"):
            assert f"{ip}/24 dev eth0" in text and f"{ip}/24 dev eth1" in text


def test_apply_refuses_units_another_state_folder_owns(tmp_path, monkeypatch, capsys, host):
    """dciab-services.kube runs from another state folder: apply changes nothing and says which
    (that folder's --down would otherwise stop and remove this one's pod; CodeRabbit on #230)."""
    from test_services import CHRONY_LOCAL, bare_site
    monkeypatch.setattr(units, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "placeholder")
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", CHRONY_LOCAL)
    cfg = bare_site(tmp_path)
    kube = tmp_path / "quadlet" / "dciab-services.kube"
    kube.parent.mkdir()
    kube.write_text(f"[Kube]\nYaml={tmp_path / 'other' / 'services' / 'pod.yaml'}\n")
    record_addresses(tmp_path, "10.30.110.53")           # with a record of its own, too
    assert _apply(cfg, tmp_path, "--units") == 1
    err = capsys.readouterr().err
    assert str(tmp_path / "other") in err and str(tmp_path / "st") in err and "--down --state" in err
    assert "other" in kube.read_text()                    # untouched
    assert not (tmp_path / "st" / "services" / "pod.yaml").exists()
    assert not (tmp_path / "systemd").exists()
    # the same folder's unit: fine
    kube.write_text(f"[Kube]\nYaml={tmp_path / 'st' / 'services' / 'pod.yaml'}\n")
    assert _apply(cfg, tmp_path, "--units") == 0
    assert units.installed_conf() == tmp_path / "st" / "services"


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


def _address_step(tmp_path, addrs, record_lines, ips="10.30.110.53 10.30.110.54", prefix="24"):
    """Runs services.sh's add_service_ips against a fake ip (state in a file)."""
    import subprocess
    from conftest import ROOT
    step = (ROOT / "lib" / "steps" / "services.sh").read_text()
    funcs = []
    for name in ("cidrs_of", "recorded", "record", "add_service_ips"):
        lines = step.splitlines()
        i = next(n for n, line in enumerate(lines) if line.startswith(f"{name}() "))
        if lines[i].rstrip().endswith("}"):
            funcs.append(lines[i])
        else:
            j = next(n for n in range(i, len(lines)) if lines[n] == "}")
            funcs.append("\n".join(lines[i:j + 1]))
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "addrs"
    state.write_text("".join(f"{a}\n" for a in addrs))
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "created").write_text("".join(f"{line}\n" for line in record_lines))
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ip").write_text(f"""#!/bin/sh
case "$*" in
  "-o -4 addr show dev eth0") while read -r a; do echo "2: eth0    inet $a brd x scope global eth0"; done < {state} ;;
  "addr add "*) echo "$3" >> {state} ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
""")
    (fake / "ip").chmod(0o755)
    script = "\n".join(funcs) + f'\nCONF_DIR={conf}\nSERVICE_IPS="{ips}"\nPREFIX={prefix}\nadd_service_ips eth0\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return r, state.read_text().split(), (conf / "created").read_text().splitlines()


def test_address_step_compares_ip_and_prefix(tmp_path):
    """has the IP with the wanted prefix: nothing; with another prefix that dciab added (recorded):
    the wanted one is added and recorded, the old one goes in the pod step's prune; with another
    prefix someone else added: reported and never touched (#230 N9)."""
    r, addrs, rec = _address_step(tmp_path, ["10.30.110.53/24"], [])
    assert "10.30.110.53/24 already on eth0" in r.stdout
    assert addrs == ["10.30.110.53/24", "10.30.110.54/24"] and rec == ["address eth0 10.30.110.54/24"]

    r, addrs, rec = _address_step(tmp_path / "b", ["10.30.110.53/16", "10.30.110.54/24"],
                                  ["address eth0 10.30.110.53/16", "address eth0 10.30.110.54/24"])
    assert "added 10.30.110.53/24 to eth0 (the old prefix, 10.30.110.53/16, goes in the pod step)" in r.stdout
    assert "10.30.110.53/24" in addrs and "address eth0 10.30.110.53/24" in rec

    r, addrs, rec = _address_step(tmp_path / "c", ["10.30.110.53/16"], [])
    assert "10.30.110.53 is on eth0 as 10.30.110.53/16, which dciab services didn't add" in r.stderr
    assert "10.30.110.53/24" not in addrs and "address eth0 10.30.110.53/24" not in rec
    # a lookalike IP (10.30.110.531) is a different address
    r, addrs, rec = _address_step(tmp_path / "d", ["10.30.110.531/24"], [], ips="10.30.110.53")
    assert "added 10.30.110.53/24 to eth0" in r.stdout and "old prefix" not in r.stdout


def test_units_slow_their_retries_where_systemd_can(tmp_path, monkeypatch):
    """A lasting failure (missing image, renamed NIC) retries every 5 s; with systemd 254 or
    later the retries slow to every 5 minutes, so the journal doesn't fill (#230 N10)."""
    conf = tmp_path / "services"
    kw = dict(systemd_dir=tmp_path / "s", quadlet_dir=tmp_path / "q")
    slow = units.render(conf, "eth0", 24, ["10.0.0.53"], backoff=True, **kw)
    plain = units.render(conf, "eth0", 24, ["10.0.0.53"], backoff=False, **kw)
    for text in slow.values():
        assert "RestartSteps=10\nRestartMaxDelaySec=5min" in text and "RestartSec=5" in text
    for text in plain.values():
        assert "RestartSteps" not in text and "RestartMaxDelaySec" not in text and "RestartSec=5" in text

    import subprocess as sp
    for out, version, has in (("systemd 257 (257.4-1.el10)\n+PAM ...", 257, True),
                              ("systemd 252 (252-46.el9)\n", 252, False), ("", 0, False)):
        monkeypatch.setattr(sp, "run",
                            lambda *a, _o=out, **k: sp.CompletedProcess(a, 0, _o, ""))
        assert units.systemd_version() == version
        auto = units.render(conf, "eth0", 24, ["10.0.0.53"], **kw)
        assert all(("RestartSteps" in t) == has for t in auto.values())


# R1-R4 (fourth review of #230)

def test_an_address_on_a_nic_that_no_longer_exists_counts_as_gone(tmp_path, host, monkeypatch):
    """The bootstrap NIC was renamed or removed: ip can't read it and /sys/class/net has no entry.
    The address went with the NIC, so the line is dropped and --down finishes."""
    host.error = {"ip -j -4 addr show dev gone0"}      # the fixture's /sys/class/net has no gone0
    conf = conf_with(tmp_path, ["address gone0 10.30.110.53/24"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert not (conf / "created").exists()
    assert any("gone0 no longer exists" in m for m in msgs)


def test_an_unreadable_nic_that_still_exists_stays_listed(tmp_path, host, monkeypatch):
    (Path(lifecycle.SYS_NET) / "eth9").mkdir()
    host.error = {"ip -j -4 addr show dev eth9"}
    conf = conf_with(tmp_path, ["address eth9 10.30.110.53/24"])
    assert lifecycle.down(conf, lambda m: None) == ["address eth9 10.30.110.53/24"]


class ZoneGoneHost(FakeHost):
    """firewall-cmd answers INVALID_ZONE (112) for zone 'old'."""

    def __call__(self, cmd, check=False, **kw):
        if cmd and str(cmd[0]) in ("firewall-cmd", "firewall-offline-cmd") and any(
                str(c) == "--zone=old" for c in cmd):
            self.calls.append([str(c) for c in cmd])
            if check:
                raise subprocess.CalledProcessError(112, cmd, "", "INVALID_ZONE: old")
            return subprocess.CompletedProcess(cmd, 112, "", "Error: INVALID_ZONE: old")
        return super().__call__(cmd, check=check, **kw)


@pytest.mark.parametrize("firewalld", [True, False])
def test_a_port_in_a_zone_that_no_longer_exists_counts_as_gone(tmp_path, monkeypatch, firewalld):
    h = ZoneGoneHost()
    h.firewalld = firewalld
    monkeypatch.setattr(lifecycle.subprocess, "run", h)
    conf = conf_with(tmp_path, ["port old 53/udp runtime", "port old 53/udp permanent"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert not (conf / "created").exists()
    assert any("zone old no longer exists" in m for m in msgs)


def test_another_firewalld_error_still_keeps_the_port(tmp_path, host):
    host.error = {"--query-port=53/udp"}
    conf = conf_with(tmp_path, ["port public 53/udp runtime"])
    assert lifecycle.down(conf, lambda m: None) == ["port public 53/udp runtime"]


def test_forget_drops_one_line_and_touches_nothing(tmp_path, host):
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24", "pod dciab-services"])
    msgs = []
    assert lifecycle.forget(conf, "pod   dciab-services", msgs.append)
    assert (conf / "created").read_text() == "address eth0 10.30.110.53/24\n"
    assert host.calls == [] and "dciab-services" in host.pods
    assert not lifecycle.forget(conf, "pod other", msgs.append)
    assert lifecycle.forget(conf, "address eth0 10.30.110.53/24", msgs.append)
    assert not (conf / "created").exists() and not (conf / "applied").exists()


def test_forget_needs_root(tmp_path, monkeypatch, capsys):
    conf = conf_with(tmp_path, ["pod dciab-services"])
    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 1000)
    assert lifecycle.main(["down", str(conf), "--forget", "pod dciab-services"]) == 1
    assert "root" in capsys.readouterr().err
    assert (conf / "created").exists()


def test_forget_with_no_match_lists_the_record(tmp_path, monkeypatch, capsys):
    conf = conf_with(tmp_path, ["pod dciab-services"])
    monkeypatch.setattr(lifecycle.os, "geteuid", lambda: 0)
    monkeypatch.setenv("DCIAB_SERVICES_LOCK", str(tmp_path / "lock"))
    assert lifecycle.main(["down", str(conf), "--forget", "pod nope"]) == 1
    err = capsys.readouterr().err
    assert "has no line 'pod nope'" in err and "pod dciab-services" in err


def test_a_stale_restore_for_a_removed_address_doesnt_bring_it_back(tmp_path, host):
    """The reviewer's scenario: a crash left a restore line for .54 next to its address line.
    --down removes .54 and must not put it back."""
    conf = conf_with(tmp_path, ["address eth0 10.30.110.54/24", f"restore eth0 10.30.110.54/24 boot={BOOT}"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert "10.30.110.54/24" not in host.addrs["eth0"]
    assert not (conf / "created").exists()


def test_prune_settles_restores_first_and_drops_one_for_an_address_it_removes(tmp_path, host):
    host.addrs["eth0"].discard("10.30.110.60/24")
    conf = conf_with(tmp_path, ["address eth0 10.30.110.54/24", f"restore eth0 10.30.110.54/24 boot={BOOT}",
                                f"restore eth0 10.30.110.60/24 boot={BOOT}"])
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.53", "10.30.110.55"]   # .54 no longer wanted
    assert lifecycle.prune(conf, cli.wanted(doc, "eth0", "public"), say=lambda m: None) == []
    assert "10.30.110.54/24" not in host.addrs["eth0"]                  # removed, not put back
    assert "10.30.110.60/24" in host.addrs["eth0"]                      # the other restore replayed
    adds = [c for c in host.calls if c[:3] == ["ip", "addr", "add"]]
    dels = [c for c in host.calls if c[:3] == ["ip", "addr", "del"]]
    assert host.calls.index(adds[0]) < host.calls.index(dels[0])        # restores before removals


@pytest.mark.parametrize("line", ["restore eth0 10.30.110.60/24", "restore eth0 10.30.110.60/24 boot=boot-0"])
def test_a_restore_from_an_earlier_boot_is_dropped_not_replayed(tmp_path, host, line):
    host.addrs["eth0"].discard("10.30.110.60/24")
    conf = conf_with(tmp_path, [line])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert "10.30.110.60/24" not in host.addrs["eth0"]
    assert not (conf / "created").exists()
    assert any("written before this boot" in m for m in msgs)


def test_restore_keeps_noprefixroute_and_nodad(tmp_path, host, monkeypatch):
    """NetworkManager's addresses carry noprefixroute; put back without it, the kernel adds its
    own prefix route next to NetworkManager's."""
    real = lifecycle.addresses

    def flagged(nic):
        out = real(nic)
        if out is None:
            return out
        for a in out:
            if a["cidr"] == "10.30.110.60/24":
                a["cmd"] += ["noprefixroute", "nodad"]
        return out
    monkeypatch.setattr(lifecycle, "addresses", flagged)
    host.addrs["eth0"] |= {"10.30.110.60/24"}
    host.takes_along = {"10.30.110.53/24": ["10.30.110.60/24"]}
    conf = conf_with(tmp_path, ["address eth0 10.30.110.53/24"])
    assert lifecycle.down(conf, lambda m: None) == []
    assert ["ip", "addr", "add", "10.30.110.60/24", "dev", "eth0", "noprefixroute", "nodad"] in host.calls


def test_addresses_reads_noprefixroute_and_nodad_from_ip_json(monkeypatch):
    import json
    out = json.dumps([{"ifname": "eth0", "addr_info": [
        {"family": "inet", "local": "10.30.110.60", "prefixlen": 24, "scope": "global", "label": "eth0",
         "noprefixroute": True, "nodad": True, "valid_life_time": 4294967295,
         "preferred_life_time": 4294967295}]}])
    monkeypatch.setattr(lifecycle, "query", lambda cmd: subprocess.CompletedProcess(cmd, 0, out, ""))
    assert lifecycle.addresses("eth0") == [
        {"cidr": "10.30.110.60/24",
         "cmd": ["ip", "addr", "add", "10.30.110.60/24", "dev", "eth0", "noprefixroute", "nodad"]}]


def test_a_restore_for_a_nic_that_no_longer_exists_is_dropped(tmp_path, host):
    host.error = {"ip -j -4 addr show dev gone0"}
    conf = conf_with(tmp_path, [f"restore gone0 10.30.110.60/24 boot={BOOT}"])
    msgs = []
    assert lifecycle.down(conf, msgs.append) == []
    assert not (conf / "created").exists()
    assert any("gone0 no longer exists" in m for m in msgs)
