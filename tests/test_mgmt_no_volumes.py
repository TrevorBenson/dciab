"""MGMT keeps no volumes (ADR 0008, decision 4a): nothing dciab mgmt installs on MGMT asks
for a PersistentVolumeClaim, volumeClaimTemplates or a StorageClass, unless the config sets
spec.storage.block.overrides.mgmt. hack/check_no_volumes.py is the check; CI also runs it on
the rendered Helm charts (the charts job)."""

import importlib.util
import os
import re
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, load_example

sys.path.insert(0, str(ROOT / "lib"))

from dciab_backup import jobs  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "hack" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nv = _load("check_no_volumes")
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
CRDS = _load("fetch_crds").cache_dir(LOCK)
PINNED = {**LOCK, "images": {**LOCK["images"], "backup": "ghcr.io/example/dciab-backup:abc@sha256:" + "d" * 64}}
KEYS = ["age1" + c * 58 for c in "qpz"]
EXAMPLES = ("vsphere", "proxmox", "proxmox-bare", "metal3-cluster")
# What dciab mgmt installs on MGMT apart from block storage (lib/steps/mgmt.sh,
# lib/steps/capi-providers.sh): the provider manifests it applies, by folder.
PROVIDER_GLOBS = ("cert-manager-*/cert-manager.yaml", "baremetal-operator-*/*.yaml", "cluster-api-*/*.yaml",
                  "infrastructure-*/*.yaml", "ipam-*/*.yaml")
# Charts and add-ons dciab mgmt installs on MGMT whose sources are in the repository. Their
# Helm renders are checked in CI's charts job; here, their templates and values.
MGMT_SOURCES = ("charts/capi-cluster", "charts/capi-clusterclasses", "addons/cilium", "addons/kubelet-csr-approver",
                "addons/vsphere-cpi")
VOLUME_TEXT = re.compile(r"kind:\s*(PersistentVolumeClaim|StorageClass)\b|volumeClaimTemplates?\s*:|"
                         r"persistentVolumeClaim\s*:")


# --- the check itself -------------------------------------------------------------------

def test_the_check_finds_every_kind_of_volume_request():
    docs = [
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "data"}},
        {"kind": "StorageClass", "metadata": {"name": "replicated"}},
        {"kind": "StatefulSet", "metadata": {"name": "db"},
         "spec": {"volumeClaimTemplates": [{"metadata": {"name": "d"}}]}},
        {"kind": "Deployment", "metadata": {"name": "app"},
         "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "x"}}]}}}},
        {"kind": "Pod", "metadata": {"name": "eph"},
         "spec": {"volumes": [{"name": "e", "ephemeral": {"volumeClaimTemplate": {"spec": {}}}}]}},
        {"kind": "List", "items": [{"kind": "PersistentVolumeClaim", "metadata": {"name": "listed"}}]},
    ]
    found = nv.offenders(docs)
    for name in ("PersistentVolumeClaim/data", "StorageClass/replicated", "StatefulSet/db", "Deployment/app",
                 "Pod/eph", "PersistentVolumeClaim/listed"):
        assert any(f.startswith(name) for f in found), (name, found)


def test_the_check_allows_scratch_and_host_volumes_and_crd_schemas():
    docs = [
        {"kind": "CronJob", "metadata": {"name": "etcd"}, "spec": {"jobTemplate": {"spec": {"template": {"spec": {
            "volumes": [{"name": "w", "emptyDir": {}}, {"name": "p", "hostPath": {"path": "/x"}},
                        {"name": "t", "nfs": {"server": "nas", "path": "/b"}}, {"name": "s", "secret": {}}]}}}}}},
        {"kind": "CustomResourceDefinition", "metadata": {"name": "x"},
         "spec": {"versions": [{"schema": {"properties": {"volumeClaimTemplates": {"type": "array"}}}}]}},
        None,
    ]
    assert nv.offenders(docs) == []


def test_the_cli_fails_on_a_claim(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: PersistentVolumeClaim\nmetadata: {name: data}\n---\nkind: ConfigMap\n")
    r = subprocess.run([sys.executable, str(ROOT / "hack" / "check_no_volumes.py"), str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "PersistentVolumeClaim/data" in r.stdout
    bad.write_text("kind: ConfigMap\n")
    r = subprocess.run([sys.executable, str(ROOT / "hack" / "check_no_volumes.py"), str(bad)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_check_can_leave_out_what_a_live_test_cluster_brings(tmp_path):
    """CI reads the kind cluster dciab mgmt ran against; kind's own StorageClass and its
    local-path provisioner aren't DCiaB's."""
    live = {"kind": "List", "items": [
        {"kind": "StorageClass", "metadata": {"name": "standard"}},
        {"kind": "Deployment", "metadata": {"name": "local-path-provisioner", "namespace": "local-path-storage"},
         "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "x"}}]}}}},
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": "cluster-mgmt"}},
    ]}
    f = tmp_path / "live.yaml"
    f.write_text(yaml.safe_dump(live))
    cmd = [sys.executable, str(ROOT / "hack" / "check_no_volumes.py"), "--ignore", "StorageClass/standard",
           "--ignore-namespace", "local-path-storage", str(f)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.count("\n") == 1 and "PersistentVolumeClaim/data" in r.stdout
    live["items"].pop()
    f.write_text(yaml.safe_dump(live))
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert subprocess.run(cmd[:2] + ["--bogus", str(f)], capture_output=True).returncode == 2


# --- what dciab mgmt installs on MGMT ---------------------------------------------------

@pytest.mark.parametrize("name", EXAMPLES)
def test_mgmt_backup_jobs_ask_for_no_volume(name):
    """ADR 0009's etcd snapshots and CAPI exports stage on the node's disk and write to the
    site target: they need no StorageClass on MGMT."""
    docs = jobs.manifests(load_example(name)["spec"], "mgmt", KEYS, PINNED)
    assert [d for d in docs if d["kind"] == "CronJob"]
    assert nv.offenders(docs) == []


@pytest.mark.skipif(not CRDS.is_dir() and not os.environ.get("CI"), reason="run hack/fetch_crds.py first")
def test_mgmt_provider_manifests_ask_for_no_volume():
    files = [p for g in PROVIDER_GLOBS for p in sorted(CRDS.glob(g))]
    assert any("cert-manager" in str(p) for p in files) and any("cluster-api" in str(p) for p in files)
    n, found = nv.check([str(p) for p in files])
    assert n and found == []


def test_mgmt_chart_and_addon_sources_ask_for_no_volume():
    found = []
    for src in MGMT_SOURCES:
        for p in sorted((ROOT / src).rglob("*")):
            if p.is_file() and p.suffix in (".yaml", ".yml", ".tpl", ".sh", ".py"):
                found += [f"{p.relative_to(ROOT)}: {m.group(0)}" for m in VOLUME_TEXT.finditer(p.read_text())]
    assert found == []


def test_the_mgmt_steps_install_only_what_these_tests_cover():
    """Every add-on or chart dciab mgmt installs outside the storage step is in the lists above;
    a new one has to be added here (and checked) first."""
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    before_storage = text.split("\nstep storage\n", 1)[0]
    addons = set(re.findall(r'\$ROOT/addons/([a-z0-9-]+)/install\.sh', before_storage))
    charts = set(re.findall(r'\$ROOT/charts/([a-z0-9-]+)', before_storage))
    assert addons and charts
    assert {f"addons/{a}" for a in addons} | {f"charts/{c}" for c in charts} <= set(MGMT_SOURCES)
    backups = text.split("\nstep backups\n", 1)[1]
    assert "addons/" not in backups and "charts/" not in backups
    providers = (ROOT / "lib" / "steps" / "capi-providers.sh").read_text()
    applied = set(re.findall(r'"\$crds"/([a-z-]+)-\*', providers))
    assert applied and applied <= {g.split("-*")[0] for g in PROVIDER_GLOBS}, applied
