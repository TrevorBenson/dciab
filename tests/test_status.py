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


def test_services_only_with_hypervisors(monkeypatch):
    monkeypatch.setattr(cli, "is_root", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/" + t)
    assert cli.check_services(SITE, fake([])) is None
    site = {"spec": {"bootstrap": {"address": "x"}, "hypervisors": {"hosts": []}}}
    assert cli.check_services(site, fake([("pod inspect", 0, "Running\n")]))["status"] == "ok"
    assert cli.check_services(site, fake([]))["status"] == "missing"


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
