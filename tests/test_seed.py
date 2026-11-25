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


@pytest.mark.parametrize("state_arg", [True, False])
def test_cli_non_root_creates_nothing(tmp_path, monkeypatch, capsys, state_arg):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: {}\n")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)

    def no_default_state(*a, **kw):
        raise AssertionError("default_state (which may migrate state) ran before the root check")
    monkeypatch.setattr(cli, "default_state", no_default_state)
    extra = ["--state", str(tmp_path / "s")] if state_arg else []
    rc = cli.main([str(cfg), "--root", str(ROOT), "--payload", str(tmp_path)] + extra)
    assert rc == 1 and "run as root" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dciab.yaml"]


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
    good = {("10.30.110.54", "registry.dc3.example.com", "A")}
    queries = []

    def fake_dns(ip, name, qtype):
        queries.append((ip, name, qtype))
        return ["10.30.110.5"] if (ip, name, qtype) in good else []
    monkeypatch.setattr(probe, "dns", fake_dns)
    with pytest.raises(plan.SeedError, match="10.30.110.53 doesn't resolve.*run dciab services"):
        cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s, timeout=0)
    assert queries == [("10.30.110.53", "registry.dc3.example.com", "A"),
                       ("10.30.110.54", "registry.dc3.example.com", "A")]
    good.add(("10.30.110.53", "registry.dc3.example.com", "A"))
    queries.clear()
    cli.step_dns(ROOT, cfg, tmp_path, tmp_path, s, timeout=0)
    assert queries == [("10.30.110.53", "registry.dc3.example.com", "A"),
                       ("10.30.110.54", "registry.dc3.example.com", "A")]


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
    (tmp_path / "s").mkdir()
    cli.step_dns(ROOT, cfg, tmp_path / "p", tmp_path / "s", plan.load_settings(cfg))
    assert calls[0][1:] == ["services", str(cfg), "--state", str(tmp_path / "s"), "--payload", str(tmp_path / "p")]
    assert capsys.readouterr().out == "dns: pod dciab-services already runs this config\n"
    assert cli.load_created(tmp_path / "s") == {"services": "dns-only"}   # for dciab seed --remove


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


def test_ca_guard_names_state_kept_aside(tmp_path):
    state = tmp_path / "state"
    (state / "pki").mkdir(parents=True)
    (state / "pki" / "trust-bundle.pem").write_text("x\n")
    aside = tmp_path / "state.from-site-abc"
    aside.mkdir()
    with pytest.raises(plan.SeedError, match=f"mv {state} .*mv {aside} {state}"):
        pki.ensure(state / "pki", settings(tmp_path, recipients=["age1unused"]), identity=None)


@need_tools
def test_lone_ca_key_is_kept_aside(tmp_path):
    _, r1 = keypair(tmp_path, "a")
    d = tmp_path / "pki"
    d.mkdir()
    (d / "ca.key.age").write_text("interrupted\n")    # a first run stopped before ca.crt
    changed = pki.ensure(d, settings(tmp_path, recipients=[r1]), identity=None)
    [bak] = d.glob("ca.key.age.*.bak")
    assert bak.read_text() == "interrupted\n" and any("aside" in c for c in changed)
    assert (d / "ca.key.age").read_bytes() != b"interrupted\n"


# capi-providers.sh: the classes release, against fake kubectl and helm


FAKE_HELM = """#!/usr/bin/env bash
echo "helm $*" >> "$FAKE_LOG"
case "$1" in
  list) [[ -n "${FAKE_DEPLOYED:-}" ]] && echo classes ;;
  get) printf '%s\\n' "$FAKE_HAVE" ;;
  template) printf '%s\\n' "$FAKE_WANT" ;;
esac
exit 0
"""


def providers_run(tmp_path, *, deployed, have, want, missing=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "helm").write_text(FAKE_HELM)
    (bin_dir / "kubectl").write_text('#!/usr/bin/env bash\necho "kubectl $*" >> "$FAKE_LOG"\n'
                                     '[[ "$*" == *"get -f -"* && -n "${FAKE_MISSING:-}" ]] && exit 1\nexit 0\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    m = tmp_path / "manifests"
    for rel in ("cert-manager-v1/cert-manager.yaml", "baremetal-operator-v1/crds.yaml", "cluster-api-v1/core.yaml",
                "infrastructure-proxmox-v1/infra.yaml", "ipam-in-cluster-v1/ipam.yaml"):
        (m / rel).parent.mkdir(parents=True, exist_ok=True)
        (m / rel).write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    log = tmp_path / "log"
    log.write_text("")
    import os
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
           "FAKE_DEPLOYED": "1" if deployed else "", "FAKE_HAVE": have, "FAKE_WANT": want,
           "FAKE_MISSING": "1" if missing else ""}
    r = subprocess.run([str(ROOT / "lib" / "steps" / "capi-providers.sh"), "--with-classes", "--manifests", str(m)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout, [line for line in log.read_text().splitlines() if line.startswith("helm upgrade")]


MANIFEST = "---\n# Source: capi-clusterclasses/templates/classes.yaml\nkind: ClusterClass\nmetadata:\n  name: proxmox-r1\n"


def test_unchanged_classes_release_is_not_upgraded(tmp_path):
    # Trailing spaces and blank lines aside, the deployed manifest is what the chart renders now.
    out, upgrades = providers_run(tmp_path, deployed=True, have=MANIFEST.replace("r1\n", "r1  \n\n"), want=MANIFEST)
    assert upgrades == [] and "not upgrading" in out


def test_deleted_clusterclass_is_recreated(tmp_path):
    # The release still says deployed, but a ClusterClass in it was deleted: upgrade re-creates it.
    out, upgrades = providers_run(tmp_path, deployed=True, have=MANIFEST, want=MANIFEST, missing=True)
    assert len(upgrades) == 1 and "not upgrading" not in out


@pytest.mark.parametrize("deployed,have", [(True, MANIFEST.replace("r1", "r0")), (False, MANIFEST), (True, "")])
def test_changed_or_missing_classes_release_is_upgraded(tmp_path, deployed, have):
    out, upgrades = providers_run(tmp_path, deployed=deployed, have=have, want=MANIFEST)
    assert len(upgrades) == 1 and "upgrade --install classes" in upgrades[0] and "not upgrading" not in out


# dciab seed --remove, against a fake host


class FakeHost:
    """kind, podman, ip, firewall-cmd and kubectl over a little in-memory host."""

    def __init__(self):
        self.kind = {"dciab-bootstrap"}
        self.containers = {"dciab-registry": ("c0ffee" + "0" * 58, "tok1")}   # name: (ID, seed token)
        self.pods = {"dciab-services"}
        self.addrs = {"eth0": {"10.30.110.5/24", "10.30.110.53/24", "10.30.110.54/24"}}
        self.ports = {("public", "53/udp"), ("public", "53/tcp"), ("public", "22/tcp")}
        self.firewalld = True
        self.capi_clusters = []
        self.calls = []

    def __call__(self, cmd, check=False, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        rc, out = 0, ""
        if cmd[:3] == ["kind", "get", "clusters"]:
            out = "\n".join(sorted(self.kind))
        elif cmd[:3] == ["kind", "delete", "cluster"]:
            self.kind.discard(cmd[-1])
        elif cmd[0] == "kubectl":
            out = "\n".join(self.capi_clusters)
        elif cmd[:3] == ["podman", "ps", "-a"]:
            name = cmd[cmd.index("--filter") + 1].split("^")[1].rstrip("$")
            if name in self.containers:
                cid, token = self.containers[name]
                out = f"{cid} {token}" if "seed-token" in cmd[-1] else cid
        elif cmd[:2] == ["podman", "rm"]:
            self.containers = {n: c for n, c in self.containers.items() if c[0] != cmd[-1]}
        elif cmd[:3] == ["podman", "pod", "exists"]:
            rc = 0 if cmd[3] in self.pods else 1
        elif cmd[:3] == ["podman", "pod", "rm"]:
            self.pods.discard(cmd[-1])
        elif cmd[:2] == ["ip", "-o"]:
            out = " ".join(f"2: {cmd[-1]} inet {a} scope global" for a in sorted(self.addrs.get(cmd[-1], ())))
        elif cmd[:2] == ["ip", "-j"]:
            out = json.dumps([{"ifname": cmd[-1], "addr_info": [
                {"family": "inet", "local": a.split("/")[0], "prefixlen": int(a.split("/")[1]),
                 "scope": "global", "label": cmd[-1]} for a in sorted(self.addrs.get(cmd[-1], ()))]}])
        elif cmd[:3] == ["ip", "addr", "del"]:
            self.addrs[cmd[-1]].discard(cmd[3])
        elif cmd[0] == "firewall-offline-cmd":
            rc = 1   # nothing permanent on this host
        elif cmd[0] == "firewall-cmd":
            if not self.firewalld:
                rc = 252
            elif cmd[1] == "--permanent":
                rc = 1   # nothing permanent on this host
            elif cmd[1] != "--state":
                zone, (op, port) = cmd[1].split("=")[1], cmd[2].lstrip("-").split("=")
                if op == "query-port":
                    rc = 0 if (zone, port) in self.ports else 1
                elif op == "remove-port":
                    self.ports.discard((zone, port))
        else:
            raise AssertionError(f"unexpected command {cmd}")
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd, out, "")
        return subprocess.CompletedProcess(cmd, rc, out, "")


def seeded_state(tmp_path, services=True):
    state = tmp_path / "state"
    (state / "services").mkdir(parents=True)
    (state / "pki").mkdir()
    (state / "pki" / "ca.key.age").write_text("sealed\n")
    created = {"kind": "dciab-bootstrap", "registry": {"name": "dciab-registry", "token": "tok1"}}
    if services:
        created["services"] = "dns-only"
    cli.save_created(state, created)
    (state / "services" / "created").write_text(
        "address eth0 10.30.110.53/24\naddress eth0 10.30.110.54/24\nport public 53/udp\nport public 53/tcp\n"
        "pod dciab-services\n")
    (state / "services" / "applied").write_text("fingerprint\n")
    return state


@pytest.fixture
def host(monkeypatch):
    h = FakeHost()
    monkeypatch.setattr(cli.subprocess, "run", h)
    return h


def test_remove_undoes_what_seed_created_and_keeps_the_state(tmp_path, host, capsys):
    state = seeded_state(tmp_path)
    assert cli.remove(state, purge=False, force=False) == 0
    assert host.kind == set() and host.containers == {} and host.pods == set()
    assert host.addrs["eth0"] == {"10.30.110.5/24"}          # the bootstrap address was never seed's
    assert host.ports == {("public", "22/tcp")}
    assert (state / "pki" / "ca.key.age").read_text() == "sealed\n"
    assert not (state / cli.CREATED).exists() and not (state / "services" / "created").exists()
    assert not (state / "services" / "applied").exists()
    assert "kept the state folder" in capsys.readouterr().out
    # Idempotent: a second run finds nothing to do and touches nothing.
    host.calls.clear()
    assert cli.remove(state, purge=False, force=False) == 0
    assert mutated(host) == []


def test_remove_skips_what_is_already_gone(tmp_path, host):
    state = seeded_state(tmp_path)
    host.kind.clear()
    host.containers.clear()
    host.pods.clear()
    host.addrs["eth0"].discard("10.30.110.53/24")
    host.firewalld = False
    assert cli.remove(state, purge=False, force=False) == 0
    assert host.addrs["eth0"] == {"10.30.110.5/24"}
    assert not any(c[:3] in (["kind", "delete", "cluster"], ["podman", "pod", "rm"]) for c in host.calls)


def test_remove_leaves_a_registry_container_seed_did_not_create(tmp_path, host, capsys):
    state = seeded_state(tmp_path)
    host.containers["dciab-registry"] = ("beef" + "1" * 60, "")   # replaced by someone else
    assert cli.remove(state, purge=True, force=False) == 1
    assert host.containers == {"dciab-registry": ("beef" + "1" * 60, "")}
    assert "isn't the one seed created" in capsys.readouterr().err
    assert state.is_dir() and cli.load_created(state)["registry"]["token"] == "tok1"  # retried next time


MUTATING = (["kind", "delete"], ["podman", "rm"], ["podman", "pod", "rm"], ["ip", "addr", "del"])


def mutated(host):
    return [c for c in host.calls if any(c[:len(m)] == m for m in MUTATING) or "--remove-port" in " ".join(c)]


def test_remove_without_records_touches_nothing_and_says_what_is_left(tmp_path, host, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "services").mkdir()
    (state / "services" / "created").write_text("address eth0 10.30.110.53/24\n")  # services run on its own
    assert cli.remove(state, purge=False, force=False) == 0
    assert mutated(host) == [] and "10.30.110.53/24" in host.addrs["eth0"]
    out, err = capsys.readouterr()
    assert "lists nothing" in out and "see above for what is left" in out
    for cmd in ("kind delete cluster --name dciab-bootstrap", "podman rm -f dciab-registry",
                f"podman kube down {state}/services/pod.yaml", "ip addr del", "--remove-port"):
        assert cmd in err


@pytest.mark.parametrize("record", [None, "{not json", "[]"])
def test_purge_refuses_while_unrecorded_parts_still_run(tmp_path, host, capsys, record):
    """A seed from before the record, or a lost or corrupt one: the state is still in use."""
    state = tmp_path / "state"
    state.mkdir()
    if record is not None:
        (state / cli.CREATED).write_text(record)
    host.capi_clusters = ["cluster.cluster.x-k8s.io/mgmt"]
    assert cli.remove(state, purge=True, force=True) == 1
    assert state.is_dir() and mutated(host) == []
    assert host.kind and host.containers and host.pods
    err = capsys.readouterr().err
    assert "not deleting" in err and "still holds cluster.cluster.x-k8s.io/mgmt" in err
    if record not in (None,):
        assert "couldn't be read" in err and list(state.glob("seed-created.json.*.corrupt"))
    # Once they are gone by hand, the purge goes ahead.
    host.kind.clear(), host.containers.clear(), host.pods.clear()
    assert cli.remove(state, purge=True, force=False) == 0 and not state.exists()


def test_recorded_but_absent_parts_count_as_removed(tmp_path, host):
    state = seeded_state(tmp_path)
    host.kind.clear()
    host.containers.clear()
    assert cli.remove(state, purge=True, force=False) == 0 and not state.exists()


def test_records_are_written_atomically_and_privately(tmp_path):
    cli.record_created(tmp_path, "kind", "dciab-bootstrap")
    cli.record_created(tmp_path, "services", "dns-only")
    assert cli.load_created(tmp_path) == {"kind": "dciab-bootstrap", "services": "dns-only"}
    assert (tmp_path / cli.CREATED).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [cli.CREATED]   # no temporary file left
    (tmp_path / cli.CREATED).write_text("")                               # truncated by a crash
    cli.record_created(tmp_path, "kind", "dciab-bootstrap")              # seed carries on
    assert cli.load_created(tmp_path) == {"kind": "dciab-bootstrap"}


def test_registry_is_recorded_before_it_is_started(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        cmd = [str(c) for c in cmd]
        if cmd[:2] == ["podman", "run"]:
            rec = cli.load_created(tmp_path)["registry"]
            seen.append(rec)
            assert f"dciab.io/seed-token={rec['token']}" in cmd
        out = "sha256:img\n" if cmd[:2] == ["podman", "pull"] else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.units, "persistent", lambda: False)
    monkeypatch.setattr(cli, "open_registry_port", lambda state, s: None)
    monkeypatch.setattr(cli, "wait_registry", lambda *a: None)
    monkeypatch.setattr(cli, "payload_images", lambda p: [])
    monkeypatch.setattr(cli, "missing_images", lambda i, d: [])
    monkeypatch.setattr(cli, "find_image", lambda i, ref: {"name": "zot"})
    (tmp_path / "pki").mkdir()
    for f in ("registry.crt", "registry.key"):
        (tmp_path / "pki" / f).write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "bundle.lock").write_text("images: {zot: z}\n")

    class S:
        address = "10.0.0.5"
    cli.step_registry(root, tmp_path, tmp_path, S())
    assert seen and seen[0]["name"] == "dciab-registry"
    cli.step_registry(root, tmp_path, tmp_path, S())       # a rerun that recreates it keeps the token
    assert seen[1]["token"] == seen[0]["token"]


def registry_env(tmp_path, monkeypatch, persistent=True):
    """step_registry against a fake host; returns (calls, state)."""
    calls = []
    host = {"have": "", "active": False}

    def fake_run(cmd, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        out = ""
        if cmd[:2] == ["podman", "pull"]:
            out = "sha256:img\n"
        elif cmd[:3] == ["podman", "ps", "-a"]:
            out = host["have"]
        elif cmd[:2] == ["systemctl", "restart"]:
            host["active"] = True
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def fake_probe(cmd, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0 if host["active"] else 3, "", "")
        if cmd[:2] == ["podman", "info"]:
            return subprocess.CompletedProcess(cmd, 0, "/srv/containers/storage\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "probe_cmd", fake_probe)
    monkeypatch.setattr(cli.units, "persistent", lambda: persistent)
    monkeypatch.setattr(cli.units, "QUADLET_DIR", tmp_path / "quadlet")
    monkeypatch.setattr(cli, "open_registry_port", lambda state, s: None)
    monkeypatch.setattr(cli, "wait_registry", lambda *a: None)
    monkeypatch.setattr(cli, "payload_images", lambda p: [])
    monkeypatch.setattr(cli, "missing_images", lambda i, d: [])
    monkeypatch.setattr(cli, "find_image", lambda i, ref: {"name": "zot"})
    state = tmp_path / "state"
    (state / "pki").mkdir(parents=True)
    for f in ("registry.crt", "registry.key"):
        (state / "pki" / f).write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "bundle.lock").write_text("images: {zot: z}\n")
    return calls, state, host


class Seed:
    address = "10.0.0.5"
    interface = "eth0"


def test_registry_runs_as_a_quadlet_unit_and_survives_reboots(tmp_path, monkeypatch):
    calls, state, host = registry_env(tmp_path, monkeypatch)
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    unit = tmp_path / "quadlet" / "dciab-registry.container"
    text = unit.read_text()
    rec = cli.load_created(state)["registry"]
    assert rec["unit"] == str(unit)
    assert f"Label=dciab.io/seed-token={rec['token']}" in text
    assert "PublishPort=10.0.0.5:443:5000" in text and "WantedBy=multi-user.target" in text
    assert "Restart=always" in text
    # the state folder (registry data, its config) and container storage, wherever they live (#230 14)
    assert f"RequiresMountsFor={state} {state}/registry/data {state}/registry/etc /srv/containers/storage\n" in text
    assert ["systemctl", "daemon-reload"] in calls and ["systemctl", "restart", "dciab-registry.service"] in calls
    assert not any(c[:2] == ["podman", "run"] for c in calls)
    # A rerun with the container running and the unit unchanged touches nothing.
    spec = [line.split("=", 2)[2] for line in text.splitlines() if line.startswith("Label=dciab.io/spec=")][0]
    host["have"] = f"{spec} running"
    calls.clear()
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    assert not any(c[:2] in (["systemctl", "restart"], ["systemctl", "daemon-reload"]) for c in calls)
    assert unit.read_text() == text and cli.load_created(state)["registry"]["token"] == rec["token"]


def test_registry_unit_left_unloaded_is_reloaded_before_the_restart(tmp_path, monkeypatch):
    """A run that stopped after writing the unit, before daemon-reload: the next run finds the
    unit current but the service inactive, and reloads before restarting (CodeRabbit on #230)."""
    calls, state, host = registry_env(tmp_path, monkeypatch)
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    host["active"] = False                       # systemd never generated the service
    calls.clear()
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    reload, restart = ["systemctl", "daemon-reload"], ["systemctl", "restart", "dciab-registry.service"]
    assert reload in calls and restart in calls and calls.index(reload) < calls.index(restart)


def test_registry_unit_of_another_state_folder_is_refused(tmp_path, monkeypatch):
    """dciab-registry.container serves another state folder's registry: seed changes nothing
    and says which (that folder's --remove would remove this registry)."""
    calls, state, host = registry_env(tmp_path, monkeypatch)
    unit = tmp_path / "quadlet" / "dciab-registry.container"
    unit.parent.mkdir()
    unit.write_text(f"[Container]\nVolume={tmp_path / 'other' / 'registry' / 'data'}:/var/lib/registry:Z\n")
    with pytest.raises(cli.SeedError, match="one state folder owns it"):
        cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    assert "other" in unit.read_text()
    assert not any(c[:2] == ["systemctl", "restart"] for c in calls)
    assert "registry" not in cli.load_created(state)


def test_remove_leaves_a_registry_unit_another_state_folder_installed(tmp_path, monkeypatch, capsys):
    """A retry of a partial --remove finds the unit path now serving another state folder's
    registry: neither that unit nor its container is touched, and this seed's record goes
    (CodeRabbit on #230)."""
    calls = []
    monkeypatch.setattr(cli, "run", lambda cmd, **kw: calls.append([str(c) for c in cmd])
                        or subprocess.CompletedProcess(cmd, 0, "", ""))
    from dciab_services import lifecycle
    monkeypatch.setattr(lifecycle, "undo", lambda *a, **k: calls.append(["undo", *a[0]]))
    unit = tmp_path / "quadlet" / "dciab-registry.container"
    unit.parent.mkdir()
    unit.write_text(f"[Container]\nVolume={tmp_path / 'other' / 'registry' / 'data'}:/var/lib/registry:Z\n")
    state = tmp_path / "state"
    state.mkdir()
    cli.remove_registry({"name": "dciab-registry", "token": "tok1", "unit": str(unit)}, state)
    assert unit.exists() and calls == []
    assert "another state folder" in capsys.readouterr().out


def test_registry_from_an_older_seed_moves_under_systemd(tmp_path, monkeypatch):
    """A container started by podman run (before #81) is replaced by the unit's."""
    calls, state, host = registry_env(tmp_path, monkeypatch)
    host["have"] = "whatever running"
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    assert ["podman", "rm", "-f", "dciab-registry"] in calls
    assert calls.index(["podman", "rm", "-f", "dciab-registry"]) < calls.index(
        ["systemctl", "restart", "dciab-registry.service"])


def test_registry_without_quadlet_still_runs(tmp_path, monkeypatch, capsys):
    calls, state, host = registry_env(tmp_path, monkeypatch, persistent=False)
    cli.step_registry(tmp_path / "root", tmp_path, state, Seed())
    assert any(c[:2] == ["podman", "run"] for c in calls)
    assert "won't come back after a reboot" in capsys.readouterr().out
    assert "unit" not in cli.load_created(state)["registry"]


def _port_env(monkeypatch, ports, zone="internal"):
    calls = []

    def fake_probe(cmd, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if cmd == ["firewall-cmd", "--state"]:
            return subprocess.CompletedProcess(cmd, 0, "running", "")
        if cmd[1].startswith("--get-zone-of-interface"):
            return subprocess.CompletedProcess(cmd, 0, f"{zone}\n", "")
        where = "permanent" if cmd[1] == "--permanent" else "runtime"
        if "--query-port=443/tcp" in cmd:
            z = [c for c in cmd if c.startswith("--zone=")][0].split("=")[1]
            return subprocess.CompletedProcess(cmd, 0 if (z, "443/tcp") in ports[where] else 1, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    def fake_run(cmd, **kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        where = "permanent" if cmd[1] == "--permanent" else "runtime"
        z = [c for c in cmd if c.startswith("--zone=")][0].split("=")[1]
        if any(c.startswith("--add-port") for c in cmd):
            ports[where].add((z, "443/tcp"))
        else:
            ports[where].discard((z, "443/tcp"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    from dciab_services import lifecycle
    monkeypatch.setattr(cli, "probe_cmd", fake_probe)
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(lifecycle, "query", fake_probe)
    monkeypatch.setattr(lifecycle, "run", fake_run)
    monkeypatch.setattr(lifecycle, "firewalld_running", lambda: True)
    return calls


def test_registry_port_opened_and_kept(tmp_path, monkeypatch):
    ports = {"runtime": set(), "permanent": set()}
    calls = _port_env(monkeypatch, ports)
    cli.open_registry_port(tmp_path, Seed())
    assert ports == {"runtime": {("internal", "443/tcp")}, "permanent": {("internal", "443/tcp")}}
    assert cli.load_created(tmp_path)["registry-ports"] == [
        ["port", "internal", "443/tcp", "runtime"], ["port", "internal", "443/tcp", "permanent"]]
    calls.clear()
    cli.open_registry_port(tmp_path, Seed())       # a rerun changes nothing
    assert not any("--add-port=443/tcp" in c for c in calls)
    # An operator's runtime rule is never recorded; seed adds (and records) only the permanent one.
    other = tmp_path / "other"
    other.mkdir()
    ports.update(runtime={("internal", "443/tcp")}, permanent=set())
    cli.open_registry_port(other, Seed())
    assert cli.load_created(other)["registry-ports"] == [["port", "internal", "443/tcp", "permanent"]]


def test_registry_port_follows_the_interface_to_another_zone(tmp_path, monkeypatch):
    """The bootstrap interface moved zones between seed runs: what seed opened in the old zone
    is closed, not forgotten (#230 6). An older single-zone record counts too."""
    ports = {"runtime": {("public", "443/tcp")}, "permanent": {("public", "443/tcp")}}
    cli.save_created(tmp_path, {"registry-port": {"zone": "public", "port": "443/tcp"}})
    _port_env(monkeypatch, ports, zone="internal")
    cli.open_registry_port(tmp_path, Seed())
    assert ports == {"runtime": {("internal", "443/tcp")}, "permanent": {("internal", "443/tcp")}}
    rec = cli.load_created(tmp_path)
    assert "registry-port" not in rec
    assert rec["registry-ports"] == [["port", "internal", "443/tcp", "runtime"],
                                     ["port", "internal", "443/tcp", "permanent"]]


def test_remove_keeps_registry_ports_it_cannot_close(tmp_path, monkeypatch):
    """firewalld stopped and no firewall-offline-cmd: the permanent rule stays listed (#230 4)."""
    from dciab_services import lifecycle
    cli.save_created(tmp_path, {"registry-ports": [["port", "public", "443/tcp", "permanent"]]})
    monkeypatch.setattr(lifecycle, "firewalld_running", lambda: False)
    monkeypatch.setattr(lifecycle, "query", lambda cmd: subprocess.CompletedProcess(cmd, 127, "", ""))
    assert cli.remove(tmp_path, purge=False, force=False) == 1
    assert cli.load_created(tmp_path)["registry-ports"] == [["port", "public", "443/tcp", "permanent"]]


def test_remove_closes_the_registry_port_and_stops_the_unit(tmp_path, host, monkeypatch):
    state = seeded_state(tmp_path)
    unit = tmp_path / "dciab-registry.container"
    unit.write_text("[Container]\n")
    created = cli.load_created(state)
    created["registry"]["unit"] = str(unit)
    created["registry-port"] = {"zone": "public", "port": "443/tcp"}
    cli.save_created(state, created)
    host.ports.add(("public", "443/tcp"))
    undone = []
    from dciab_services import lifecycle
    real_undo = lifecycle.undo

    def undo(entry, say=print, firewalld=None, journal=None):
        undone.append(entry)
        if entry[0] == "unit":
            Path(entry[1]).unlink()
            return None
        return real_undo(entry, say, firewalld)
    monkeypatch.setattr(lifecycle, "undo", undo)
    assert cli.remove(state, purge=False, force=False) == 0
    assert undone[0] == ["unit", str(unit)] and not unit.exists()
    assert ("public", "443/tcp") not in host.ports
    rec = cli.load_created(state)
    assert "registry-port" not in rec and "registry-ports" not in rec


def test_services_pod_name_matches_services():
    from dciab_services import render
    assert cli.SERVICES_POD == render.POD


def test_remove_leaves_services_it_did_not_run(tmp_path, host):
    state = seeded_state(tmp_path, services=False)   # a site with spec.hypervisors: services is its own
    assert cli.remove(state, purge=False, force=False) == 0
    assert host.pods == {"dciab-services"} and "10.30.110.53/24" in host.addrs["eth0"]
    assert ("public", "53/udp") in host.ports and (state / "services" / "created").exists()


def test_remove_refuses_a_bootstrap_cluster_that_still_manages_clusters(tmp_path, host, capsys):
    state = seeded_state(tmp_path)
    host.capi_clusters = ["cluster.cluster.x-k8s.io/mgmt"]
    assert cli.remove(state, purge=True, force=False) == 1
    assert host.kind == {"dciab-bootstrap"} and state.is_dir()
    assert "still holds cluster.cluster.x-k8s.io/mgmt" in capsys.readouterr().err
    assert cli.remove(state, purge=False, force=True) == 0 and host.kind == set()


def test_remove_purge_state_deletes_it_with_a_warning(tmp_path, host, capsys):
    state = seeded_state(tmp_path)
    assert cli.remove(state, purge=True, force=False) == 0
    assert not state.exists()
    assert "WARNING: deleting the state folder" in capsys.readouterr().err


def test_cli_remove_and_purge_flags(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: {}\n")
    with pytest.raises(SystemExit):
        cli.main([str(cfg), "--root", str(ROOT), "--purge-state"])
    assert "go with --remove" in capsys.readouterr().err
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    seen = {}
    monkeypatch.setattr(cli, "remove", lambda state, purge, force: seen.update(state=state, purge=purge) or 0)
    # No state folder: nothing to remove, and none is made.
    assert cli.main([str(cfg), "--root", str(ROOT), "--state", str(tmp_path / "s"), "--remove"]) == 0
    assert not (tmp_path / "s").exists() and seen == {}
    (tmp_path / "s").mkdir()
    monkeypatch.setattr(cli.os, "access", lambda *a: False)
    assert cli.main([str(cfg), "--root", str(ROOT), "--payload", str(tmp_path / "none"), "--state",
                     str(tmp_path / "s"), "--remove", "--purge-state"]) == 0
    assert seen == {"state": tmp_path / "s", "purge": True}


def test_seed_prints_elapsed_time_per_step(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.os, "access", lambda *a: False)
    monkeypatch.setattr(cli, "verify_payload_tools", lambda payload: None)
    monkeypatch.setattr(cli, "load_settings", lambda config: None)
    monkeypatch.setattr(cli, "step_preflight", lambda *a: None)
    monkeypatch.setattr(cli.pki, "ensure", lambda *a, **k: [])
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: {}\n")
    assert cli.main([str(cfg), "--root", str(tmp_path), "--state", str(tmp_path / "s"), "--until", "pki"]) == 0
    out = capsys.readouterr().out
    assert "== seed: preflight done in 0s" in out and "== seed: pki done in 0s" in out


def test_services_prints_elapsed_time_per_step():
    step = (ROOT / "lib" / "steps" / "services.sh").read_text()
    funcs = step[step.index('current="" started=0'):step.index("\n# Read the plan")]
    script = f'set -euo pipefail\nuntil=b\n{funcs}\nstep a\necho work\nstep b\nfinish_if b\necho unreachable\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n") == ["", "== services: a", "work", "== services: a done in 0s", "",
                                    "== services: b", "== services: b done in 0s",
                                    "dciab services: stopped after 'b' (--until)", ""]
    assert step.rstrip().endswith("helper check\nstep_done")
