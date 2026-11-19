import copy
import hashlib
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CLASSES = ROOT / "charts" / "capi-clusterclasses" / "classes"
CRDS = ROOT / ".cache" / "crds"

_spec = importlib.util.spec_from_file_location("check_manifests", ROOT / "hack" / "check_manifests.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

needs_crds = pytest.mark.skipif(not CRDS.is_dir(), reason="run hack/fetch_crds.py first")


def versions():
    return sorted((p.name for p in CLASSES.iterdir() if p.is_dir()), key=lambda v: int(v[1:]))


def class_docs(version="v1", provider="vsphere"):
    text = (CLASSES / version / f"{provider}.yaml").read_text()
    return [(f"{provider}.yaml", d) for d in yaml.safe_load_all(text) if d]


@pytest.fixture(scope="module")
def schemas():
    return cm.load_crds([CRDS])


def problems(schemas, docs):
    return cm.Checker(schemas, copy.deepcopy(docs)).run()


def clusterclass(docs):
    return next(d for _, d in docs if d["kind"] == "ClusterClass")


# --- the shipped classes ---------------------------------------------------------------

def test_frozen_classes_match_their_checksums():
    sums = {}
    for line in (CLASSES / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        sums[name] = digest
    files = sorted(str(p.relative_to(CLASSES)) for p in CLASSES.glob("v*/*.yaml"))
    assert sorted(sums) == files, "every class file needs a line in SHA256SUMS, and nothing else"
    for name, digest in sums.items():
        actual = hashlib.sha256((CLASSES / name).read_bytes()).hexdigest()
        assert actual == digest, (f"{name} changed. Released class versions are frozen: copy it to a "
                                  "new version folder instead (see charts/capi-clusterclasses/README.md)")


def test_every_version_has_all_three_providers():
    for v in versions():
        assert sorted(p.stem for p in (CLASSES / v).glob("*.yaml")) == ["metal3", "proxmox", "vsphere"]


def test_newest_classes_use_bundle_kube_vip():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    newest = versions()[-1]
    for f in (CLASSES / newest).glob("*.yaml"):
        images = set(re.findall(r"image: (\S+)", f.read_text()))
        assert images == {lock["images"]["kubeVip"]}, f"{f.name} uses {images}"


def test_class_names_carry_their_version():
    for v in versions():
        for f in (CLASSES / v).glob("*.yaml"):
            for d in yaml.safe_load_all(f.read_text()):
                if d:
                    assert d["metadata"]["name"].startswith(f"dciab-{f.stem}-{v}"), d["metadata"]["name"]


@needs_crds
@pytest.mark.parametrize("provider", ["vsphere", "proxmox", "metal3"])
def test_classes_pass_the_checker(schemas, provider):
    for v in versions():
        assert problems(schemas, class_docs(v, provider)) == []


# --- the checker catches mistakes ------------------------------------------------------

@needs_crds
def test_checker_catches_a_bad_patch_path(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    cc["spec"]["patches"][3]["definitions"][0]["jsonPatches"][0]["path"] = "/spec/template/spec/servr"
    assert any("path does not exist" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_replace_of_a_missing_field(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    op = cc["spec"]["patches"][3]["definitions"][0]["jsonPatches"][1]
    assert op["path"] == "/spec/template/spec/thumbprint"
    op["op"] = "replace"
    assert any("cannot replace or remove it (use add)" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_append_to_a_missing_list(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    node_base = cc["spec"]["patches"][0]["definitions"][0]["jsonPatches"]
    node_base[0]["path"] += "/-"  # the op that creates files now appends to a list that isn't there
    assert any("does not exist in the template at this point" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_an_undeclared_variable(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    op = cc["spec"]["patches"][3]["definitions"][0]["jsonPatches"][2]
    op["valueFrom"]["template"] = op["valueFrom"]["template"].replace(".apiVIP", ".apiVip")
    assert any("'.apiVip', which is not a declared variable" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_a_missing_template(schemas):
    docs = [(s, d) for s, d in class_docs() if d["kind"] != "VSphereClusterTemplate"]
    assert any("is not in the input" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_unknown_fields_from_templates(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    op = cc["spec"]["patches"][3]["definitions"][0]["jsonPatches"][2]
    op["valueFrom"]["template"] = "host: {{ .apiVIP }}\nprot: 6443\n"
    assert any("unknown field /prot" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_unknown_fields_in_objects(schemas):
    docs = class_docs()
    tmpl = next(d for _, d in docs if d["kind"] == "VSphereClusterTemplate")
    tmpl["spec"]["template"]["spec"]["sever"] = "x"
    assert any("'sever' was unexpected" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_a_variable_type_mismatch(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    for v in cc["spec"]["variables"]:
        if v["name"] == "dnsServers":
            v["schema"]["openAPIV3Schema"] = {"type": "string"}
    px = class_docs(provider="proxmox")
    pcc = clusterclass(px)
    for v in pcc["spec"]["variables"]:
        if v["name"] == "dnsServers":
            v["schema"]["openAPIV3Schema"] = {"type": "string"}
    assert any("variable 'dnsServers' is string but the field is array" in p for p in problems(schemas, px))


@needs_crds
def test_checker_reports_missing_required_cluster_variables(schemas):
    cluster = {
        "apiVersion": "cluster.x-k8s.io/v1beta2", "kind": "Cluster", "metadata": {"name": "c1"},
        "spec": {"topology": {"classRef": {"name": "dciab-vsphere-v1"}, "version": "v1.36.6",
                              "variables": [{"name": "apiVIP", "value": "10.0.0.10"}]}},
    }
    found = problems(schemas, class_docs() + [("cluster.yaml", cluster)])
    assert any("required variable 'vsphere'" in p for p in found)
