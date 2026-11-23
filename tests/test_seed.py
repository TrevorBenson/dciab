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


@need_tools
def test_recipient_change_reencrypts(tmp_path):
    i1, r1 = keypair(tmp_path, "a")
    i3, r3 = keypair(tmp_path, "c")
    d = tmp_path / "pki"
    pki.ensure(d, settings(tmp_path, recipients=[r1]), identity=None)
    s2 = settings(tmp_path, recipients=[r3])
    with pytest.raises(plan.SeedError, match="recipients changed"):
        pki.ensure(d, s2, identity=None)
    assert any("re-encrypted" in c for c in pki.ensure(d, s2, identity=i1))
    subprocess.run(["age", "-d", "-i", str(i3), "-o", str(tmp_path / "k"), str(d / "ca.key.age")], check=True)


@need_tools
def test_mismatched_registry_key_reissues(tmp_path):
    i1, r1 = keypair(tmp_path, "a")
    d = tmp_path / "pki"
    s = settings(tmp_path, recipients=[r1])
    pki.ensure(d, s, identity=None)
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-out", str(d / "registry.key")], check=True)
    assert not pki.registry_cert_ok(d, s)


def test_tmpfs_required(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
    with pytest.raises(plan.SeedError, match="tmpfs"):
        pki.tmpfs_dir(str(mounts))


def test_payload_tools_checked_against_sums(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "oras").write_bytes(b"real")
    import hashlib
    (tmp_path / "SHA256SUMS").write_text(f"{hashlib.sha256(b'real').hexdigest()}  bin/oras\n")
    cli.verify_payload_tools(tmp_path)
    (tmp_path / "bin" / "oras").write_bytes(b"evil")
    with pytest.raises(plan.SeedError, match="SHA256SUMS"):
        cli.verify_payload_tools(tmp_path)


def test_cli_refuses_non_root(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump({"spec": {"bootstrap": {"address": "10.0.0.5", "recipients": []},
                                            "network": {"domain": "x.example"}, "pki": {"mode": "generate-root"}}}))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "verify_payload_tools", lambda p: None)
    rc = cli.main([str(cfg), "--root", str(ROOT), "--payload", str(tmp_path), "--state", str(tmp_path / "s")])
    assert rc == 1 and "run as root" in capsys.readouterr().err


def test_capi_providers_takes_a_manifests_folder(tmp_path):
    r = subprocess.run([str(ROOT / "lib" / "steps" / "capi-providers.sh"), "--manifests", str(tmp_path / "nope")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "no such folder" in r.stderr


def test_dns_step_runs_between_pki_and_registry():
    assert cli.STEPS[cli.STEPS.index("pki") + 1] == "dns"
    assert cli.STEPS[cli.STEPS.index("dns") + 1] == "registry"
    for step in cli.STEPS:
        assert f"  {step} " in cli.__doc__


def test_dns_step_with_hypervisors_needs_services_answering(tmp_path, monkeypatch):
    from dciab_services import probe
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox-bare.yaml").read_text())
    s = plan.load_settings(cfg)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("services must not run"))
    # Answers keyed by (server, name, type): a query to another server or of another type gets nothing.
    good = {("10.30.110.54", "registry.lab1.example.com", "A")}
    queries = []

    def fake_dns(ip, name, qtype):
        queries.append((ip, name, qtype))
        return ["10.30.110.5"] if (ip, name, qtype) in good else []
    monkeypatch.setattr(probe, "dns", fake_dns)
    with pytest.raises(plan.SeedError, match="10.30.110.53 doesn't resolve.*run dciab services"):
        cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s, timeout=0)
    assert queries == [("10.30.110.53", "registry.lab1.example.com", "A"),
                       ("10.30.110.54", "registry.lab1.example.com", "A")]
    good.add(("10.30.110.53", "registry.lab1.example.com", "A"))
    queries.clear()
    cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s, timeout=0)
    assert queries == [("10.30.110.53", "registry.lab1.example.com", "A"),
                       ("10.30.110.54", "registry.lab1.example.com", "A")]


def test_dns_step_retries_with_backoff_until_services_answers(tmp_path, monkeypatch):
    from dciab_services import probe
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox-bare.yaml").read_text())
    s = plan.load_settings(cfg)
    rounds, sleeps = [], []
    monkeypatch.setattr(probe, "dns", lambda ip, name, qtype:
                        ["10.30.110.5"] if len(rounds) >= 6 or rounds.append(1) else [])
    monkeypatch.setattr("time.sleep", sleeps.append)
    cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s)   # the pod answers on the fourth try
    assert sleeps == [0.5, 1.0, 2.0]
    # A ready pod is checked once, without sleeping.
    sleeps.clear()
    cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s)
    assert sleeps == []


def test_dns_step_without_hypervisors_runs_dns_only_services(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox.yaml").read_text())
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="\n== services: pod\npod dciab-services already runs this config\n")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    cli.step_dns(ROOT, cfg, tmp_path / "p", tmp_path / "s", plan.load_settings(cfg))
    assert calls[0][1:] == ["services", str(cfg), "--state", str(tmp_path / "s"), "--payload", str(tmp_path / "p")]
    assert capsys.readouterr().out == "dns: pod dciab-services already runs this config\n"


@pytest.mark.parametrize("left", ["pki/ca.crt", "pki/trust-bundle.pem", "pki/registry.crt", "mgmt.kubeconfig"])
def test_no_new_root_ca_when_one_was_issued_before(tmp_path, left):
    state = tmp_path / "state"
    (state / "pki").mkdir(parents=True)
    (state / left).write_text("x\n")
    s = settings(tmp_path, recipients=["age1unused"])

    def no_tools(cmd, **kw):
        raise AssertionError(f"ran {cmd}")

    with pytest.raises(plan.SeedError, match="--force-new-ca"):
        pki.ensure(state / "pki", s, identity=None, runner=no_tools)
    assert (state / left).read_text() == "x\n"


@need_tools
def test_force_new_ca_generates_over_a_lost_ca(tmp_path):
    _, r1 = keypair(tmp_path, "a")
    s = settings(tmp_path, recipients=[r1])
    d = tmp_path / "pki"
    pki.ensure(d, s, identity=None)
    old = (d / "ca.crt").read_text()
    (d / "ca.key.age").unlink()                       # the CA key is lost
    with pytest.raises(plan.SeedError, match="--force-new-ca"):
        pki.ensure(d, s, identity=None)
    assert any("root CA" in c for c in pki.ensure(d, s, identity=None, force_new_ca=True))
    assert (d / "ca.crt").read_text() != old


def test_cli_passes_force_new_ca(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.os, "access", lambda *a: False)  # the lock goes in the state, not /run/lock
    monkeypatch.setattr(cli, "verify_payload_tools", lambda payload: None)
    monkeypatch.setattr(cli, "load_settings", lambda config: None)
    monkeypatch.setattr(cli, "step_preflight", lambda *a: None)

    def ensure(pki_dir, s, identity, force_new_ca=False):
        seen["force"] = force_new_ca
        return []

    monkeypatch.setattr(cli.pki, "ensure", ensure)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: {}\n")
    args = [str(cfg), "--root", str(tmp_path), "--state", str(tmp_path / "s"), "--until", "pki"]
    assert cli.main(args + ["--force-new-ca"]) == 0 and seen["force"] is True
