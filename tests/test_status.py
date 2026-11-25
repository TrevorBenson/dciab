"""dciab status: each read-only check, with a fake runner."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_status import cli  # noqa: E402

SITE = {"spec": {"bootstrap": {"address": "10.0.0.5"}}}


def fake(answers):
    """answers: list of (substring of the joined command, returncode, stdout)."""
    def runner(cmd):
        joined = " ".join(cmd)
        for needle, rc, out in answers:
            if needle in joined:
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no answer")
    return runner


def test_nothing_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "is_root", lambda: False)
    checks = {c["check"]: c["status"] for c in cli.collect(SITE, tmp_path, fake([]))}
    assert checks == {"pki": "missing", "registry": "unknown", "bootstrap": "missing", "mgmt": "missing"}


NET = {"domain": "dc3.example.com", "dnsServiceIPs": ["10.0.0.53", "10.0.0.54"]}


def answers(addr, registry=None):
    """A resolver for the services readiness checks on every IP: bootstrap.dc3.example.com and
    registry.dc3.example.com give addr (registry: its own answer), the reverse name gives
    bootstrap.dc3.example.com. None: no answer at all."""
    def resolve(ip, name, qtype="A"):
        if addr is None:
            return None
        if qtype == "PTR":
            return ["bootstrap.dc3.example.com"]
        if name == "registry.dc3.example.com":
            return [registry or addr]
        return [addr] if name == "bootstrap.dc3.example.com" else []
    return resolve


def test_services_with_hypervisors(monkeypatch):
    monkeypatch.setattr(cli, "is_root", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/" + t)
    assert cli.check_services(SITE, fake([])) is None   # no DNS service IPs in the config
    site = {"spec": {"bootstrap": {"address": "10.0.0.5"}, "network": NET, "hypervisors": {"hosts": []}}}
    running = fake([("pod inspect", 0, "Running\n")])
    c = cli.check_services(site, running, resolve=answers("10.0.0.5"))
    assert (c["check"], c["status"]) == ("services", "ok")
    assert cli.check_services(site, fake([]), resolve=answers(None))["status"] == "missing"
    # running, but DNS doesn't answer from the network: down, with the IP named
    c = cli.check_services(site, running, resolve=answers(None))
    assert c["status"] == "down" and "10.0.0.53: no answer" in c["detail"]


def test_dns_only_pod_reported_as_dns(monkeypatch):
    """Sites without spec.hypervisors: dciab seed runs the pod DNS-only; status reports it."""
    monkeypatch.setattr(cli, "is_root", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/" + t)
    site = {"spec": {"bootstrap": {"address": "10.0.0.5"}, "network": NET}}
    running = fake([("pod inspect", 0, "Running\n")])
    c = cli.check_services(site, running, resolve=answers("10.0.0.5"))
    assert (c["check"], c["status"]) == ("dns", "ok")
    assert "dciab seed" in cli.check_services(site, fake([]), resolve=answers(None))["detail"]
    c = cli.check_services(site, running, resolve=answers("10.0.0.5", registry="10.9.9.9"))
    assert c["status"] == "down" and "10.0.0.54: registry.dc3.example.com gives 10.9.9.9" in c["detail"]
    # a pod that's gone is missing, even while something else still answers
    assert cli.check_services(site, fake([]), resolve=answers("10.0.0.5"))["status"] == "missing"
    assert cli.check_services(site, fake([("pod inspect", 0, "Exited\n")]),
                              resolve=answers("10.0.0.5"))["status"] == "down"


def test_status_checks_forward_and_reverse_too(monkeypatch):
    """The same three checks as the services ready step (#163): a wrong reverse answer is down."""
    monkeypatch.setattr(cli, "is_root", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/" + t)
    site = {"spec": {"bootstrap": {"address": "10.0.0.5"}, "network": NET}}
    good = answers("10.0.0.5")

    def no_ptr(ip, name, qtype="A"):
        return [] if qtype == "PTR" else good(ip, name, qtype)
    c = cli.check_services(site, fake([("pod inspect", 0, "Running\n")]), resolve=no_ptr)
    assert c["status"] == "down" and "10.0.0.5 (reverse) gives nothing" in c["detail"]


def test_dns_only_without_root_still_probes(monkeypatch):
    monkeypatch.setattr(cli, "is_root", lambda: False)
    site = {"spec": {"bootstrap": {"address": "10.0.0.5"}, "network": NET}}
    c = cli.check_services(site, fake([]), resolve=answers("10.0.0.5"))
    assert c["status"] == "ok" and "needs root" in c["detail"]
    assert cli.check_services(site, fake([]), resolve=answers(None))["status"] == "down"


def test_pki_expiry(tmp_path):
    (tmp_path / "pki").mkdir()
    (tmp_path / "pki" / "ca.crt").write_text("x")
    (tmp_path / "pki" / "registry.crt").write_text("x")
    ok = fake([("-enddate", 0, "notAfter=Dec 21 00:00:00 2026 GMT\n"), ("-checkend", 0, "")])
    assert cli.check_pki(tmp_path, ok)["status"] == "ok"
    soon = fake([("-enddate", 0, "notAfter=Nov 23 00:00:00 2026 GMT\n"), ("-checkend", 1, "")])
    assert "rerun dciab seed" in cli.check_pki(tmp_path, soon)["detail"]


def test_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "is_root", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/" + t)
    (tmp_path / "pki").mkdir()
    (tmp_path / "pki" / "trust-bundle.pem").write_text("x")
    running = fake([("container inspect", 0, "running\n")])
    assert cli.check_registry(SITE, tmp_path, running, probe=lambda a, c: None)["status"] == "ok"
    assert cli.check_registry(SITE, tmp_path, running, probe=lambda a, c: "refused")["status"] == "down"
    assert cli.check_registry(SITE, tmp_path, fake([("container inspect", 0, "exited\n")]),
                              probe=lambda a, c: None)["status"] == "down"


def test_bootstrap_and_mgmt_before_and_after_the_move(tmp_path):
    (tmp_path / "bootstrap.kubeconfig").write_text("x")
    up = [("/readyz", 0, "ok"), ("availableReplicas", 0, "1"), ("clusterclasses", 0, "a\nb\nc\n")]
    assert "3 ClusterClasses" in cli.check_bootstrap(tmp_path, fake(up))["detail"]
    before = fake([("bootstrap.kubeconfig --request-timeout=10s -n cluster-mgmt", 0, "mgmt/Provisioning")])
    assert "not moved" in cli.check_mgmt(tmp_path, before)["detail"]
    (tmp_path / "mgmt.kubeconfig").write_text("x")
    after = fake([("mgmt.kubeconfig --request-timeout=10s -n cluster-mgmt", 0, "mgmt/Provisioned")])
    assert cli.check_mgmt(tmp_path, after)["status"] == "ok"
    (tmp_path / "mgmt.kubeconfig").unlink()
    fresh = fake([("bootstrap.kubeconfig --request-timeout=10s -n cluster-mgmt", 0, "mgmt/")])
    assert "Pending" in cli.check_mgmt(tmp_path, fresh)["detail"]


def test_cli_json(tmp_path, monkeypatch, capsys):
    import json
    import yaml
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(SITE))
    monkeypatch.setattr(cli, "is_root", lambda: False)
    monkeypatch.setattr(cli, "run", fake([]))
    rc = cli.main([str(cfg), "--root", str(ROOT), "--output", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["state"].endswith(".dciab/state") and len(out["checks"]) == 4


def test_proxmox_control_plane_placement_is_a_note(tmp_path):
    site = {"spec": {"bootstrap": {"address": "x"}, "platform": {"type": "proxmox"}}}
    assert cli.check_placement(site, tmp_path, fake([])) is None          # no cluster yet
    (tmp_path / "bootstrap.kubeconfig").write_text("x")
    assert "no MGMT control plane machines yet" in cli.check_placement(site, tmp_path, fake([]))["detail"]
    before = fake([("bootstrap.kubeconfig --request-timeout=10s -n cluster-mgmt get proxmoxmachines", 0, "cp-a=pve01\n")])
    (tmp_path / "mgmt.kubeconfig").write_text("x")                        # saved before the move
    assert "pve01: 1" in cli.check_placement(site, tmp_path, before)["detail"]
    spread = fake([("proxmoxmachines", 0, "cp-a=pve01\ncp-b=pve02\ncp-c=pve03\n")])
    c = cli.check_placement(site, tmp_path, spread)
    assert c["status"] == "note" and "pve01: 1; pve02: 1; pve03: 1" in c["detail"] and "quorum" not in c["detail"]
    shared = fake([("proxmoxmachines", 0, "cp-a=pve01\ncp-b=pve01\ncp-c=pve02\n")])
    assert "pve01 holds more than one etcd member" in cli.check_placement(site, tmp_path, shared)["detail"]
    assert cli.check_placement({"spec": {"platform": {"type": "vsphere"}}}, tmp_path, shared) is None


def test_backups_shows_the_last_success_of_each_kind(tmp_path):
    import json
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    assert cli.check_backups(tmp_path, fake([]), now) is None
    (tmp_path / "capi-backup").mkdir()
    (tmp_path / "capi-backup" / "LAST").write_text("20261123T100000Z after-move\n")
    assert cli.check_backups(tmp_path, fake([]), now)["detail"] == "pivot CAPI export (after-move) 2h ago"
    (tmp_path / "mgmt.kubeconfig").write_text("x")
    jobs = {"items": [
        {"metadata": {"name": "etcd-snapshot"},
         "status": {"lastScheduleTime": "2026-11-23T11:00:00Z", "lastSuccessfulTime": "2026-11-23T11:01:00Z"}},
        {"metadata": {"name": "capi-export"},
         "status": {"lastScheduleTime": "2026-11-23T02:30:00Z", "lastSuccessfulTime": "2026-11-22T02:31:00Z"}}]}
    answers = [("get cronjobs", 0, json.dumps(jobs)),
               ("backups.longhorn.io", 0, "2026-11-23T01:00:05Z\n2026-11-22T01:00:04Z\n")]
    c = cli.check_backups(tmp_path, fake(answers), now)
    assert c["status"] == "down" and "Failing: capi-export" in c["detail"]
    assert "etcd-snapshot 59m ago" in c["detail"] and "longhorn 10h ago" in c["detail"]
    jobs["items"][1]["status"]["lastSuccessfulTime"] = "2026-11-23T02:32:00Z"
    answers[0] = ("get cronjobs", 0, json.dumps(jobs))
    assert cli.check_backups(tmp_path, fake(answers), now)["status"] == "ok"
    # A run still within its first hour isn't a failure yet.
    jobs["items"][0]["status"]["lastScheduleTime"] = "2026-11-23T11:30:00Z"
    answers[0] = ("get cronjobs", 0, json.dumps(jobs))
    assert cli.check_backups(tmp_path, fake(answers), now)["status"] == "ok"
    assert cli.check_backups(tmp_path, fake([("get cronjobs", 0, '{"items": []}')]), now)["status"] == "down"
