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


def test_mgmt_puts_the_payload_bin_first_in_path(tmp_path):
    """sudo resets PATH and the appliance has no clusterctl: mgmt must find the payload's (#98)."""
    import hashlib
    import os
    import sys

    import yaml

    want = yaml.safe_load((ROOT / "bundle.lock").read_text())["clusterAPI"]["core"]["version"]
    config = tmp_path / "dciab.yaml"
    config.write_text("spec: {platform: {type: vsphere}}\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "bootstrap.kubeconfig").write_text("x")
    payload = tmp_path / "payload"
    for d in ("providers", "charts", "bin"):
        (payload / d).mkdir(parents=True)
    tools = {"clusterctl": f"#!/bin/sh\necho {want}\n", "kubectl": "#!/bin/sh\nexit 1\n", "helm": "#!/bin/sh\n"}
    sums = []
    for name, body in tools.items():
        f = payload / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
        sums.append(f"{hashlib.sha256(body.encode()).hexdigest()}  bin/{name}")
    (payload / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    # A PATH like sudo's secure_path: python3 and coreutils, none of the payload's tools.
    pybin = tmp_path / "py"
    pybin.mkdir()
    (pybin / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{pybin}:/usr/bin:/bin"}
    r = subprocess.run([str(ROOT / "dciab"), "mgmt", str(config), "--state", str(state), "--payload", str(payload)],
                       capture_output=True, text=True, env=env)
    assert "not on PATH" not in r.stderr, r.stderr
    # Past the tools check and clusterctl's version: the fake kubectl finds no Cluster API.
    assert r.returncode == 1 and "has no Cluster API" in r.stderr, r.stderr


def test_credentials_leave_no_last_applied_copy(tmp_path):
    """A client-side kubectl apply would store the token or password in last-applied-configuration.
    apply_credentials applies the Secret server-side, then removes an annotation an older dciab left."""
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    func = text.split("apply_credentials() {", 1)[1].split("\n}\n", 1)[0]
    log = tmp_path / "kubectl.log"
    fake = tmp_path / "kubectl"
    fake.write_text('#!/bin/sh\necho "$*" >> "$LOG"\n[ "$1" = apply ] && cat > /dev/null\nexit 0\n')
    fake.chmod(0o755)
    script = (f'set -euo pipefail\npython3() {{ echo "{{}}"; }}\nNS=cluster-mgmt config=c platform=proxmox\n'
              f'apply_credentials() {{{func}\n}}\napply_credentials kc\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin", "LOG": str(log)})
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    # Only the namespace is applied client-side; the annotation is removed after the Secret's apply.
    applies = [c for c in calls if c.startswith("apply ")]
    assert sorted(applies) == ["apply --server-side --force-conflicts --field-manager=dciab -f -", "apply -f -"]
    assert calls.index("apply --server-side --force-conflicts --field-manager=dciab -f -") < len(calls) - 1
    assert calls[-1] == ("-n cluster-mgmt annotate secret mgmt-proxmox-credentials "
                         "kubectl.kubernetes.io/last-applied-configuration-")
    assert len(calls) == 4   # create namespace --dry-run, apply namespace, apply Secret, annotate


@pytest.mark.parametrize("example", ["proxmox", "vsphere"])
def test_credentials_secret_has_no_annotations(monkeypatch, tmp_path, example):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "s3cret")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "vsphere.pw").write_text("pw\n")
    assert "annotations" not in secret(load_example(example), "mgmt", "cluster-mgmt", tmp_path)["metadata"]
