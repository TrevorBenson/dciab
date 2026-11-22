"""dciab seed: plan functions, the PKI with real openssl and age, and the CLI's guards."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_seed import cli, pki, plan  # noqa: E402

D1 = "sha256:" + "a" * 64
need_tools = pytest.mark.skipif(not (shutil.which("openssl") and shutil.which("age") and shutil.which("age-keygen")),
                                reason="needs openssl, age and age-keygen")


def write_payload(tmp: Path, images):
    (tmp / "images").mkdir(parents=True)
    (tmp / "providers").mkdir()
    (tmp / "images.yaml").write_text(yaml.safe_dump({"images": images}))
    return tmp


def test_payload_images_and_lookup(tmp_path):
    p = write_payload(tmp_path, [
        {"image": f"registry.k8s.io/pause:3.10.2@{D1}", "name": "registry.k8s.io_pause_3.10.2_aaaa"},
        {"image": f"quay.io/x/y@{D1}", "name": "quay.io_x_y_untagged_aaaa"},
    ])
    images = plan.payload_images(p)
    assert images[0]["repo"] == "registry.k8s.io/pause" and images[0]["tag"] == "3.10.2"
    assert plan.registry_tag(images[1]) == "sha256-" + "a" * 64
    assert plan.find_image(images, f"registry.k8s.io/pause:3.10.2@{D1}")["name"].startswith("registry.k8s.io_pause")
    with pytest.raises(plan.SeedError):
        plan.find_image(images, "ghcr.io/project-zot/zot-linux-amd64:v2@" + D1)


def test_payload_refuses_unpinned(tmp_path):
    p = write_payload(tmp_path, [{"image": "registry.k8s.io/pause:3.10.2", "name": "x"}])
    with pytest.raises(plan.SeedError, match="pinned"):
        plan.payload_images(p)


def test_missing_images_reads_layout_tags(tmp_path):
    img = {"image": "", "name": "n", "repo": "registry.k8s.io/pause", "tag": "3.10.2", "digest": D1}
    assert plan.missing_images([img], tmp_path) == [img]
    layout = tmp_path / "registry.k8s.io" / "pause"
    layout.mkdir(parents=True)
    (layout / "index.json").write_text(json.dumps({"manifests": [
        {"digest": D1, "annotations": {"org.opencontainers.image.ref.name": "3.10.2"}}]}))
    assert plan.missing_images([img], tmp_path) == []


def test_zot_is_read_only_tls():
    c = plan.zot_config()
    assert c["http"]["tls"]["cert"] and c["http"]["auth"]["htpasswd"]["path"]
    assert c["http"]["accessControl"]["repositories"]["**"] == {"anonymousPolicy": ["read"]}


def test_hosts_toml_routes_through_seed_registry():
    t = plan.hosts_toml("docker.io", "10.0.0.5")
    assert 'server = "https://registry-1.docker.io"' in t
    assert '[host."https://10.0.0.5/v2/docker.io"]' in t and "override_path = true" in t
    assert 'ca = "/etc/containerd/certs.d/docker.io/ca.crt"' in t


def test_kind_config_mounts_registry_hosts(tmp_path):
    doc = yaml.safe_load(plan.kind_config(tmp_path))
    mount = doc["nodes"][0]["extraMounts"][0]
    assert doc["name"] == plan.KIND_CLUSTER and mount["containerPath"] == "/etc/containerd/certs.d"


def settings(tmp_path, mode="generate-root", recipients=None, **pki_extra):
    site = {"spec": {"bootstrap": {"address": "10.30.110.5", "recipients": recipients or []},
                     "network": {"domain": "lab1.example.com"},
                     "pki": {"mode": mode, "commonName": "Test Root CA", **pki_extra}}}
    return plan.Settings(site, tmp_path / "dciab.yaml")


def keypair(tmp_path, name):
    ident = tmp_path / f"{name}.txt"
    subprocess.run(["age-keygen", "-o", str(ident)], check=True, capture_output=True)
    pub = subprocess.run(["age-keygen", "-y", str(ident)], check=True, capture_output=True, text=True).stdout.strip()
    return ident, pub


@need_tools
def test_generate_root_then_rerun_changes_nothing(tmp_path):
    i1, r1 = keypair(tmp_path, "a")
    _, r2 = keypair(tmp_path, "b")
    s = settings(tmp_path, recipients=[r1, r2])
    d = tmp_path / "pki"
    changed = pki.ensure(d, s, identity=None)
    assert any("root CA" in c for c in changed) and any("registry certificate" in c for c in changed)
    assert not (d / "ca.key").exists() and (d / "ca.key.age").stat().st_mode & 0o077 == 0
    assert (d / "registry.key").stat().st_mode & 0o077 == 0
    names = subprocess.run(["openssl", "x509", "-in", str(d / "registry.crt"), "-noout", "-ext", "subjectAltName"],
                           check=True, capture_output=True, text=True).stdout
    assert "DNS:registry.lab1.example.com" in names and "IP Address:10.30.110.5" in names
    assert pki.ensure(d, s, identity=None) == []
    # The encrypted key opens with a recipient's identity and matches the CA.
    key = tmp_path / "ca.key"
    subprocess.run(["age", "-d", "-i", str(i1), "-o", str(key), str(d / "ca.key.age")], check=True)
    pub_ca = subprocess.run(["openssl", "x509", "-in", str(d / "ca.crt"), "-noout", "-pubkey"],
                            check=True, capture_output=True, text=True).stdout
    pub_key = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"], check=True, capture_output=True,
                             text=True).stdout
    assert pub_ca == pub_key


@need_tools
def test_reissue_needs_an_identity(tmp_path):
    i1, r1 = keypair(tmp_path, "a")
    s = settings(tmp_path, recipients=[r1])
    d = tmp_path / "pki"
    pki.ensure(d, s, identity=None)
    (d / "registry.crt").unlink()
    with pytest.raises(plan.SeedError, match="--age-identity"):
        pki.ensure(d, s, identity=None)
    assert any("registry certificate" in c for c in pki.ensure(d, s, identity=i1))


@need_tools
def test_address_change_reissues(tmp_path):
    i1, r1 = keypair(tmp_path, "a")
    d = tmp_path / "pki"
    pki.ensure(d, settings(tmp_path, recipients=[r1]), identity=None)
    s2 = settings(tmp_path, recipients=[r1])
    s2.address = "10.30.110.6"
    assert any("10.30.110.6" in c for c in pki.ensure(d, s2, identity=i1))


@need_tools
def test_import_intermediate(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    sh = lambda *a: subprocess.run(list(a), check=True, capture_output=True)  # noqa: E731
    sh("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
       "-keyout", str(secrets / "root.key"), "-out", str(secrets / "chain.pem"), "-days", "30",
       "-subj", "/CN=Root", "-addext", "basicConstraints=critical,CA:TRUE")
    sh("openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
       "-keyout", str(secrets / "int.key"), "-out", str(secrets / "int.csr"), "-subj", "/CN=Intermediate")
    (secrets / "ext").write_text("basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n")
    sh("openssl", "x509", "-req", "-in", str(secrets / "int.csr"), "-CA", str(secrets / "chain.pem"),
       "-CAkey", str(secrets / "root.key"), "-CAcreateserial", "-days", "30", "-extfile", str(secrets / "ext"),
       "-out", str(secrets / "int.crt"))
    s = settings(tmp_path, mode="import-intermediate", certificate={"file": "secrets/int.crt"},
                 key={"file": "secrets/int.key"}, chain={"file": "secrets/chain.pem"})
    d = tmp_path / "pki"
    changed = pki.ensure(d, s, identity=None)
    assert any("intermediate" in c for c in changed)
    assert not (d / "ca.key.age").exists()
    assert (d / "registry.crt").read_text().count("BEGIN CERTIFICATE") == 2   # leaf + intermediate
    assert pki.ensure(d, s, identity=None) == []


def test_cli_refuses_non_root(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"address": "10.0.0.5", "recipients": []},
                                            "network": {"domain": "x.example"}, "pki": {"mode": "generate-root"}}}))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    rc = cli.main([str(cfg), "--root", str(ROOT), "--payload", str(tmp_path), "--state", str(tmp_path / "s")])
    assert rc == 1 and "run as root" in capsys.readouterr().err


def test_capi_providers_takes_a_manifests_folder(tmp_path):
    r = subprocess.run([str(ROOT / "lib" / "steps" / "capi-providers.sh"), "--manifests", str(tmp_path / "nope")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "no such folder" in r.stderr
