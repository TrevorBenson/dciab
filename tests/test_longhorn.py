"""addons/longhorn: per-site values, the backup credentials Secret, images and install.sh."""

import base64
import hashlib
import os
import re
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, load_example

sys.path.insert(0, str(ROOT / "addons" / "longhorn"))

import longhorn_values as lv  # noqa: E402

INSTALL = ROOT / "addons" / "longhorn" / "install.sh"


def decoded(manifest):
    return {k: base64.b64decode(v).decode() for k, v in manifest["data"].items()}


def test_replica_count_comes_from_the_config():
    site = load_example("proxmox")
    site["spec"]["storage"]["longhorn"]["replicaCount"] = 1
    v = lv.site_values(site)
    assert v["persistence"]["defaultClassReplicaCount"] == 1
    assert v["defaultSettings"]["defaultReplicaCount"] == 1


def test_nfs_target_has_no_credentials_secret(tmp_path):
    site = load_example("proxmox")
    v = lv.site_values(site)
    assert v["defaultBackupStore"] == {"backupTarget": "nfs://nas01.lab1.example.net:/exports/longhorn"}
    assert lv.secret(site, "longhorn-system", tmp_path) is None


def test_s3_target_is_translated_and_names_the_secret():
    v = lv.site_values(load_example("vsphere"))
    assert v["defaultBackupStore"] == {"backupTarget": "s3://longhorn@us-east-1/dc1",
                                       "backupTargetCredentialSecret": lv.SECRET_NAME}


@pytest.mark.parametrize("target,want", [
    ("s3://bkp@s3.example.net:8333", "s3://bkp@us-east-1/"),
    ("s3://bkp@s3.example.net/a/b", "s3://bkp@us-east-1/a/b"),
    ("nfs://nas:/x", "nfs://nas:/x"),
])
def test_backup_target(target, want):
    assert lv.backup_target(target) == want


def test_s3_secret_reads_the_credentials_file_and_adds_the_endpoint(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "longhorn-s3.env").write_text(
        "# S3\nAWS_ACCESS_KEY_ID=AKID\nexport AWS_SECRET_ACCESS_KEY='sk'\n")
    m = lv.secret(load_example("vsphere"), "longhorn-system", tmp_path)
    assert m["metadata"] == {"name": lv.SECRET_NAME, "namespace": "longhorn-system",
                             "labels": {"app.kubernetes.io/managed-by": "dciab"}}
    assert decoded(m) == {"AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": "sk",
                          "AWS_ENDPOINTS": "https://minio.dc1.example.net"}


def test_s3_secret_from_an_environment_variable(monkeypatch, tmp_path):
    site = load_example("vsphere")
    site["spec"]["storage"]["longhorn"]["backupCredentials"] = {"env": "LH_S3"}
    monkeypatch.setenv("LH_S3", "AWS_ACCESS_KEY_ID=a\nAWS_SECRET_ACCESS_KEY=b\nAWS_ENDPOINTS=http://s3:8333\n")
    assert decoded(lv.secret(site, "ns", tmp_path))["AWS_ENDPOINTS"] == "http://s3:8333"


def test_s3_secret_needs_both_keys(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "longhorn-s3.env").write_text("AWS_ACCESS_KEY_ID=a\n")
    with pytest.raises(ValueError, match="AWS_SECRET_ACCESS_KEY"):
        lv.secret(load_example("vsphere"), "ns", tmp_path)


def test_bad_credentials_line(tmp_path):
    with pytest.raises(ValueError, match="line 1"):
        lv.parse_env("not an assignment\n")


def test_defaults_keep_data_under_var_and_no_ingress():
    v = yaml.safe_load((ROOT / "addons" / "longhorn" / "values.yaml").read_text())
    assert v["defaultSettings"]["defaultDataPath"].startswith("/var/")
    assert v["ingress"]["enabled"] is False
    assert v["persistence"]["defaultClass"] is True


def test_images_reads_the_image_tree():
    chart = {"image": {"longhorn": {"manager": {"repository": "longhornio/longhorn-manager", "tag": "v1"},
                                    "engine": {"repository": "longhornio/longhorn-engine", "tag": "v1"}},
                       "csi": {"attacher": {"repository": "longhornio/csi-attacher", "tag": "v4"}},
                       "pullPolicy": "IfNotPresent"},
             "persistence": {"repository": "not-an-image", "tag": "x"}}
    assert lv.images(chart) == ["longhornio/csi-attacher:v4", "longhornio/longhorn-engine:v1",
                                "longhornio/longhorn-manager:v1"]
    chart["global"] = {"imageRegistry": "docker.io"}
    assert lv.images(chart)[0] == "docker.io/longhornio/csi-attacher:v4"


def run(*args, env=None):
    return subprocess.run([str(INSTALL), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_install_usage():
    assert run().returncode == 2
    assert run(ROOT / "examples" / "proxmox.yaml", "--bogus").returncode == 2
    assert run(ROOT / "examples" / "proxmox.yaml", "--template", "--nope", "x").returncode == 2
    assert run("/no/such.yaml", "--print-values").returncode == 2


def test_install_print_values_needs_no_cluster():
    r = run(ROOT / "examples" / "vsphere.yaml", "--print-values")
    assert r.returncode == 0, r.stderr
    v = yaml.safe_load(r.stdout)
    assert v["defaultSettings"]["defaultReplicaCount"] == 3
    assert "AWS_" not in r.stdout


def test_install_refuses_a_chart_with_the_wrong_sha256(tmp_path):
    fake = tmp_path / "longhorn.tgz"
    fake.write_bytes(b"not a chart")
    r = run(ROOT / "examples" / "proxmox.yaml", "--template", "--chart", fake)
    assert r.returncode == 1 and "refusing" in r.stderr
    assert hashlib.sha256(b"not a chart").hexdigest() in r.stderr


def test_install_uses_the_payload_chart(tmp_path):
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    r = run(ROOT / "examples" / "proxmox.yaml", "--template", env={"DCIAB_PAYLOAD": str(tmp_path)})
    assert r.returncode == 1
    assert f"charts/longhorn-{lock['charts']['longhorn']}.tgz" in r.stderr
    (tmp_path / "charts").mkdir()
    (tmp_path / "charts" / f"longhorn-{lock['charts']['longhorn']}.tgz").write_bytes(b"x")
    r = run(ROOT / "examples" / "proxmox.yaml", "--template", env={"DCIAB_PAYLOAD": str(tmp_path)})
    assert r.returncode == 1 and "refusing" in r.stderr


def test_mgmt_header_lists_every_step():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    steps = re.search(r"^STEPS=\((.*)\)", text, re.M).group(1).split()
    listed = text.split("Steps, in order", 1)[1].split("set -euo pipefail", 1)[0]
    header = re.findall(r"^#   ([a-z-]+)(?:\s|$)", listed, re.M)
    assert header == steps
    assert steps.index("longhorn") > steps.index("adopt")
