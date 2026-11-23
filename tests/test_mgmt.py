"""dciab mgmt: argument handling, and the platform credentials Secret it applies."""

import base64
import subprocess

import pytest

from conftest import ROOT, load_example

from dciab_cluster.credentials import secret


def decoded(manifest):
    return {k: base64.b64decode(v).decode() for k, v in manifest["data"].items()}


def test_proxmox_secret_reads_the_token_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "s3cret")
    m = secret(load_example("proxmox"), "mgmt", "cluster-mgmt", tmp_path)
    assert m["metadata"]["name"] == "mgmt-proxmox-credentials"
    assert m["metadata"]["namespace"] == "cluster-mgmt"
    assert "clusterctl.cluster.x-k8s.io/move" in m["metadata"]["labels"]
    assert decoded(m) == {"url": "https://pve01.dc3.example.net:8006", "token": "dciab@pve!capmox",
                          "secret": "s3cret"}


def test_vsphere_secret_reads_the_password_file(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "vsphere.pw").write_text("pw\n")
    m = secret(load_example("vsphere"), "mgmt", "cluster-mgmt", tmp_path)
    assert m["metadata"]["name"] == "mgmt-vsphere-credentials"
    assert decoded(m) == {"username": "svc-dciab@vsphere.local", "password": "pw"}


def test_missing_env_secret_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("DCIAB_PVE_TOKEN", raising=False)
    with pytest.raises(KeyError, match="DCIAB_PVE_TOKEN"):
        secret(load_example("proxmox"), "mgmt", "cluster-mgmt", tmp_path)


def run_mgmt(*args):
    return subprocess.run([str(ROOT / "dciab"), "mgmt", *args], capture_output=True, text=True)


def test_mgmt_needs_a_config_and_a_bootstrap_kubeconfig():
    assert run_mgmt().returncode == 2


def test_mgmt_rejects_an_unknown_step(tmp_path):
    config = tmp_path / "dciab.yaml"
    config.write_text("x")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("x")
    r = run_mgmt(str(config), "--bootstrap-kubeconfig", str(kubeconfig), "--until", "bogus")
    assert r.returncode == 1 and "preflight values cluster" in r.stderr


def test_mgmt_defaults_to_seeds_kubeconfig(tmp_path):
    config = tmp_path / "dciab.yaml"
    config.write_text("x")
    r = run_mgmt(str(config), "--state", str(tmp_path / "state"))
    assert r.returncode == 1 and "bootstrap.kubeconfig" in r.stderr and "dciab seed" in r.stderr


def test_mgmt_rejects_a_folder_that_isnt_a_payload(tmp_path):
    config = tmp_path / "dciab.yaml"
    config.write_text("x")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("x")
    r = run_mgmt(str(config), "--bootstrap-kubeconfig", str(kubeconfig), "--payload", str(tmp_path))
    assert r.returncode == 1 and "not an archive payload" in r.stderr


def test_cilium_uses_the_payload_chart(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("name: mgmt\napiVIP: 10.0.0.10\n")
    (tmp_path / "charts").mkdir()
    r = subprocess.run([str(ROOT / "addons" / "cilium" / "install.sh"), str(values), "--template"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "DCIAB_PAYLOAD": str(tmp_path)})
    assert r.returncode == 1 and "no such file in the payload" in r.stderr


def test_mgmt_refuses_a_tampered_payload(tmp_path):
    config = tmp_path / "dciab.yaml"
    config.write_text("x")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("x")
    payload = tmp_path / "payload"
    for d in ("providers", "charts", "bin"):
        (payload / d).mkdir(parents=True)
    (payload / "bin" / "kubectl").write_text("changed")
    (payload / "SHA256SUMS").write_text("0" * 64 + "  bin/kubectl\n")
    r = run_mgmt(str(config), "--bootstrap-kubeconfig", str(kubeconfig), "--payload", str(payload))
    assert r.returncode == 1 and "SHA256SUMS" in r.stderr
