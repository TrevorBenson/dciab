"""addons/csi: the Proxmox CSI plugin and the vSphere CSI driver from their pinned upstream
manifests, their credentials Secrets, and what validate says about them."""

import base64
import hashlib
import importlib.util
import os
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, errors, load_example, warnings

sys.path.insert(0, str(ROOT / "addons" / "csi"))

import csi  # noqa: E402

LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
INSTALL = ROOT / "addons" / "csi" / "install.sh"


def _fetch():
    spec = importlib.util.spec_from_file_location("fetch_crds", ROOT / "hack" / "fetch_crds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CRDS = _fetch().cache_dir(LOCK)
needs_manifests = pytest.mark.skipif(not (CRDS / ".complete").is_file(), reason="run hack/fetch_crds.py first")


def decoded(secret):
    return {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}


def by_kind(docs, kind, name=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]


# --- pins -----------------------------------------------------------------------------------

def test_drivers_are_pinned_with_checksums():
    for name in ("proxmox-csi", "vsphere-csi"):
        files = csi.manifest_files(LOCK, name)
        assert files
        for _, _, key in files:
            assert len(LOCK["checksums"][key]) == 64, key


def test_fetch_crds_fetches_the_manifests_raw_at_their_tags():
    urls = [u for _, _, u in _fetch().csi_files(LOCK)]
    assert "https://raw.githubusercontent.com/sergelogvinov/proxmox-csi-plugin/v0.22.1/docs/deploy/proxmox-csi-plugin-release.yml" in urls
    # The manifests are part of the cache key, so a bump fetches again.
    other = dict(LOCK, csiDrivers={})
    assert _fetch().cache_dir(other) != CRDS


def test_a_changed_manifest_is_refused(tmp_path):
    folder, name, _ = csi.manifest_files(LOCK, "proxmox-csi")[0]
    (tmp_path / folder).mkdir()
    (tmp_path / folder / name).write_text("kind: Namespace\n")
    with pytest.raises(csi.CSIError, match="refusing"):
        csi.load_manifests(LOCK, "proxmox-csi", tmp_path)


# --- rendering ------------------------------------------------------------------------------

@needs_manifests
def test_proxmox_render():
    spec = load_example("proxmox")["spec"]
    docs = csi.render(spec, "mgmt", csi.load_manifests(LOCK, "proxmox-csi", CRDS))
    [ns] = by_kind(docs, "Namespace")
    assert ns["metadata"]["name"] == "csi-proxmox"
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"
    assert by_kind(docs, "CSIDriver", "csi.proxmox.sinextra.dev")
    [ctrl] = by_kind(docs, "Deployment")
    plugin = next(c for c in ctrl["spec"]["template"]["spec"]["containers"] if c["name"] == "proxmox-csi-plugin-controller")
    # TLS trust comes from the config's ca_file, not the environment: SSL_CERT_FILE would
    # keep the image's public CAs. The config's Secret is mounted where ca_file points.
    assert not any(e["name"].startswith("SSL_CERT") for e in plugin.get("env", []))
    mount = next(m for m in plugin["volumeMounts"] if m["mountPath"].rstrip("/") == "/etc/proxmox")
    vol = next(v for v in ctrl["spec"]["template"]["spec"]["volumes"] if v["name"] == mount["name"])
    assert vol["secret"]["secretName"] == "proxmox-csi-plugin"
    assert by_kind(docs, "DaemonSet")


@needs_manifests
def test_vsphere_render_drops_what_dciab_doesnt_use():
    spec = load_example("vsphere")["spec"]
    docs = csi.render(spec, "mgmt", csi.load_manifests(LOCK, "vsphere-csi", CRDS))
    assert [d["metadata"]["name"] for d in by_kind(docs, "DaemonSet")] == ["vsphere-csi-node"]
    [ctrl] = by_kind(docs, "Deployment", "vsphere-csi-controller")
    names = [c["name"] for c in ctrl["spec"]["template"]["spec"]["containers"]]
    assert "csi-snapshotter" not in names and "vsphere-csi-controller" in names
    assert ctrl["spec"]["replicas"] == 3
    prov = next(c for c in ctrl["spec"]["template"]["spec"]["containers"] if c["name"] == "csi-provisioner")
    assert "--feature-gates=Topology=true" in prov["args"]   # the example has vSphere zones
    spec["clusters"]["mgmt"]["controlPlane"]["replicas"] = 1
    spec["failureDomains"]["domains"] = []
    docs = csi.render(spec, "mgmt", csi.load_manifests(LOCK, "vsphere-csi", CRDS))
    [ctrl] = by_kind(docs, "Deployment", "vsphere-csi-controller")
    assert ctrl["spec"]["replicas"] == 1
    prov = next(c for c in ctrl["spec"]["template"]["spec"]["containers"] if c["name"] == "csi-provisioner")
    assert not any("Topology" in a for a in prov["args"])


def test_render_needs_an_array_driver():
    with pytest.raises(csi.CSIError, match="not an array CSI driver"):
        csi.driver(load_example("metal3-cluster")["spec"], "mgmt")


# --- secrets --------------------------------------------------------------------------------

def test_proxmox_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_CSI_TOKEN", "csi-secret")
    site = load_example("proxmox")
    s = csi.secret(site, "infra", tmp_path, certificate="-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
    assert s["metadata"] == {"name": "proxmox-csi-plugin", "namespace": "csi-proxmox",
                             "labels": {"app.kubernetes.io/managed-by": "dciab"}}
    data = decoded(s)
    cfg = yaml.safe_load(data["config.yaml"])
    assert cfg["features"] == {"provider": "capmox"}
    [c] = cfg["clusters"]
    assert c == {"url": "https://pve01.dc3.example.net:8006/api2/json", "insecure": False,
                 "ca_file": "/etc/proxmox/ca.pem",
                 "token_id": "csi@pve!dciab", "token_secret": "csi-secret", "region": "dc3"}
    assert data["ca.pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert "annotations" not in s["metadata"]
    assert "ca.pem" not in csi.secret(site, "infra", tmp_path, fetch=False)["data"]
    redacted = decoded(csi.secret(site, "infra", tmp_path, redact=True))
    assert "csi-secret" not in redacted["config.yaml"] and "<redacted>" in redacted["config.yaml"]
    assert "ca.pem" not in redacted


def test_proxmox_certificate_must_match_the_pin(monkeypatch):
    pem = ("-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"not-really-der").decode() +
           "\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(csi.ssl, "get_server_certificate", lambda addr, timeout: pem)
    good = ":".join(f"{b:02X}" for b in hashlib.sha256(b"not-really-der").digest())
    assert csi.proxmox_certificate("https://pve:8006", good) == pem
    with pytest.raises(csi.CSIError, match="not the pinned one"):
        csi.proxmox_certificate("https://pve:8006", "AA:" * 31 + "AA")


def test_vsphere_secret(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "vsphere.pw").write_text("pw\n")
    site = load_example("vsphere")
    conf = decoded(csi.secret(site, "mgmt", tmp_path))["csi-vsphere.conf"]
    v = site["spec"]["platform"]["vsphere"]
    assert f'[VirtualCenter "{v["server"]}"]' in conf
    assert f'thumbprint = "{v["thumbprint"]}"' in conf and 'password = "pw"' in conf
    assert 'cluster-id = "dc1-mgmt"' in conf
    assert 'topology-categories = "k8s-region,k8s-zone"' in conf
    (tmp_path / "secrets" / "vsphere.pw").write_text('p"w\n')
    with pytest.raises(csi.CSIError, match="quote"):
        csi.secret(site, "mgmt", tmp_path)


# --- install.sh and dciab mgmt ------------------------------------------------------------

@needs_manifests
def test_install_template_needs_no_cluster_and_shows_no_secret(tmp_path):
    env = {**os.environ, "DCIAB_PVE_CSI_TOKEN": "csi-SECRET"}
    r = subprocess.run([str(INSTALL), str(ROOT / "examples" / "proxmox.yaml"), "mgmt", "--template"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    # vSphere: the password file isn't there, and --template doesn't read it.
    v = subprocess.run([str(INSTALL), str(ROOT / "examples" / "vsphere.yaml"), "mgmt", "--template"],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stderr
    assert by_kind([d for d in yaml.safe_load_all(v.stdout) if d], "CSIDriver")
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert by_kind(docs, "CSIDriver") and by_kind(docs, "Secret")
    assert "csi-SECRET" not in r.stdout and base64.b64encode(b"csi-SECRET").decode() not in r.stdout


def test_install_usage():
    assert subprocess.run([str(INSTALL)], capture_output=True).returncode == 2
    r = subprocess.run([str(INSTALL), str(ROOT / "examples" / "metal3-cluster.yaml"), "mgmt", "--template"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "not an array CSI driver" in r.stderr


def test_install_applies_secrets_server_side():
    text = INSTALL.read_text()
    applies = [ln.strip() for ln in text.splitlines() if "kubectl apply" in ln]
    assert applies and all("--server-side" in a for a in applies)
    assert "lib/steps/storage-classes.sh" in text


def test_mgmt_installs_array_drivers():
    step = (ROOT / "lib" / "steps" / "mgmt.sh").read_text().split("step storage", 1)[1]
    assert "vsphere-csi|proxmox-csi)" in step and 'addons/csi/install.sh" "$config" mgmt "$mgmt_kubeconfig"' in step


# --- validate ------------------------------------------------------------------------------

def test_array_examples_validate_without_storage_warnings(site):
    for name in ("vsphere", "proxmox"):
        _, report = site.validate(load_example(name))
        assert errors(report) == []
        assert not [w for w in warnings(report) if w["path"].startswith("spec.storage")]


def test_proxmox_csi_on_local_storage_needs_a_zone_on_the_nodes_that_hold_volumes(site):
    doc = load_example("proxmox")
    assert doc["spec"]["clusters"]["mgmt"]["workerPools"] == []
    _, report = site.validate(doc)   # ceph-vm is shared: the plugin needs no zone
    assert not errors(report, "spec.clusters.mgmt.workerPools")
    doc["spec"]["storage"]["block"]["proxmoxCSI"]["storage"] = "local-lvm"
    _, report = site.validate(doc)
    assert "ADR 0010" in errors(report, "spec.clusters.mgmt.workerPools")[0]["fix"]
    doc["spec"]["clusters"]["mgmt"]["failureDomains"] = ["pve01"]
    _, report = site.validate(doc)
    assert not errors(report, "spec.clusters.mgmt.workerPools")


def test_proxmox_csi_on_local_storage_says_volumes_stay_on_their_node(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["storage"]["block"] = {"provider": "proxmox-csi", "proxmoxCSI": {
        "storage": "local-lvm", "credentials": {"tokenID": "csi@pve!dciab", "tokenSecret": {"env": "DCIAB_PVE_CSI_TOKEN"}}}}
    _, report = site.validate(doc)
    assert any("stay on the PVE node" in n for n in report["notes"])
