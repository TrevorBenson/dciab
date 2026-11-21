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
    assert decoded(m) == {"url": "https://pve01.lab1.example.net:8006", "token": "dciab@pve!capmox",
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
