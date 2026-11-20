import copy
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CLASSES = ROOT / "charts" / "capi-clusterclasses" / "classes"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "hack" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("check_manifests")
CRDS = _load("fetch_crds").cache_dir(yaml.safe_load((ROOT / "bundle.lock").read_text()))
NS = "dciab-clusterclasses"

needs_crds = pytest.mark.skipif(not CRDS.is_dir(), reason="run hack/fetch_crds.py first")


def revisions():
    return sorted((p.name for p in CLASSES.iterdir() if p.is_dir()), key=lambda r: int(r[1:]))


def installed_revisions():
    """Revisions the chart installs. Revisions no cluster uses any more stay on disk, unchecked."""
    values = yaml.safe_load((ROOT / "charts" / "capi-clusterclasses" / "values.yaml").read_text())
    return values["revisions"]


def class_docs(revision="r1", provider="vsphere"):
    text = (CLASSES / revision / f"{provider}.yaml").read_text()
    return [(f"{provider}.yaml", NS, d) for d in yaml.safe_load_all(text) if d]


@pytest.fixture(scope="module")
def schemas():
    return cm.load_crds([CRDS])


def problems(schemas, docs):
    return cm.Checker(schemas, copy.deepcopy(docs)).run()


def clusterclass(docs):
    return next(d for _, _, d in docs if d["kind"] == "ClusterClass")


# --- the shipped classes ---------------------------------------------------------------

def test_revision_folders_are_named_rN():
    assert revisions() and all(re.fullmatch(r"r[0-9]+", r) for r in revisions())


def test_every_revision_has_all_three_providers():
    for v in revisions():
        assert sorted(p.stem for p in (CLASSES / v).glob("*.yaml")) == ["metal3", "proxmox", "vsphere"]


def test_chart_installs_the_newest_revision():
    assert revisions()[-1] in installed_revisions()


def test_newest_classes_use_bundle_kube_vip():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    newest = revisions()[-1]
    for f in (CLASSES / newest).glob("*.yaml"):
        images = set(re.findall(r"image: (\S+)", f.read_text()))
        assert images == {lock["images"]["kubeVip"]}, f"{f.name} uses {images}"


def test_installed_classes_skip_kube_proxy():
    # Cilium (addons/cilium) replaces kube-proxy.
    for rev in installed_revisions():
        for f in (CLASSES / rev).glob("*.yaml"):
            kcp = next(d for d in yaml.safe_load_all(f.read_text()) if d and d["kind"] == "KubeadmControlPlaneTemplate")
            init = kcp["spec"]["template"]["spec"]["kubeadmConfigSpec"]["initConfiguration"]
            assert "addon/kube-proxy" in init.get("skipPhases", []), f"{rev}/{f.name}"


def test_cilium_addon_matches_the_classes():
    values = yaml.safe_load((ROOT / "addons/cilium/values.yaml").read_text())
    assert values["kubeProxyReplacement"] == "true"
    assert values["k8sServicePort"] == 6443
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    assert lock["charts"]["cilium"] and lock["chartRepositories"]["cilium"].startswith("https://")


def test_class_names_carry_their_revision():
    for v in revisions():
        for f in (CLASSES / v).glob("*.yaml"):
            for d in yaml.safe_load_all(f.read_text()):
                if not d:
                    continue
                name = d["metadata"]["name"]
                if d["kind"] == "ClusterClass":
                    # capi-cluster renders classRef.name as exactly dciab-<class>-<classRevision>
                    assert name == f"dciab-{f.stem}-{v}", name
                else:
                    assert name.startswith(f"dciab-{f.stem}-{v}"), name


@needs_crds
@pytest.mark.parametrize("provider", ["vsphere", "proxmox", "metal3"])
def test_classes_pass_the_checker(schemas, provider):
    for v in installed_revisions():
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
    assert any("'.apiVip': variable 'apiVip' is not declared" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_catches_a_missing_template(schemas):
    docs = [(s, n, d) for s, n, d in class_docs() if d["kind"] != "VSphereClusterTemplate"]
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
    tmpl = next(d for _, _, d in docs if d["kind"] == "VSphereClusterTemplate")
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
    assert any("'dnsServers' is string but the field is array" in p for p in problems(schemas, px))


@needs_crds
def test_checker_reports_missing_required_cluster_variables(schemas):
    cluster = {
        "apiVersion": "cluster.x-k8s.io/v1beta2", "kind": "Cluster", "metadata": {"name": "c1"},
        "spec": {"topology": {"classRef": {"name": "dciab-vsphere-r1", "namespace": NS}, "version": "v1.36.6",
                              "variables": [{"name": "apiVIP", "value": "10.0.0.10"}]}},
    }
    found = problems(schemas, class_docs() + [("cluster.yaml", "cluster-c1", cluster)])
    assert any("required variable 'vsphere'" in p for p in found)


@needs_crds
def test_checker_resolves_classes_by_namespace(schemas):
    cluster = {
        "apiVersion": "cluster.x-k8s.io/v1beta2", "kind": "Cluster", "metadata": {"name": "c1"},
        "spec": {"topology": {"classRef": {"name": "dciab-vsphere-r1", "namespace": "elsewhere"},
                              "version": "v1.36.6"}},
    }
    found = problems(schemas, class_docs() + [("cluster.yaml", "cluster-c1", cluster)])
    assert any("ClusterClass elsewhere/dciab-vsphere-r1 is not in the input" in p for p in found)


@needs_crds
def test_checker_catches_an_unknown_nested_variable_field(schemas):
    docs = class_docs()
    cc = clusterclass(docs)
    op = cc["spec"]["patches"][3]["definitions"][0]["jsonPatches"][0]
    assert op["valueFrom"]["variable"] == "vsphere.server"
    op["valueFrom"]["variable"] = "vsphere.servr"
    assert any("variable 'vsphere' has no field 'servr'" in p for p in problems(schemas, docs))


@needs_crds
def test_checker_evaluates_cel_rules(schemas):
    docs = class_docs(provider="proxmox")
    for _, _, d in docs:
        if d["kind"] == "ProxmoxMachineTemplate":
            del d["spec"]["template"]["spec"]["templateID"]
    assert any("CEL rule" in p and "templateID" in p for p in problems(schemas, docs))


def test_md_class_wildcards_match_like_capi():
    assert cm.md_name_matches("*", "default")
    assert cm.md_name_matches("def*", "default")
    assert cm.md_name_matches("*ault", "default")
    assert not cm.md_name_matches("gpu*", "default")


@needs_crds
def test_checker_enforces_api_server_formats(schemas):
    docs = class_docs(provider="metal3")
    for _, _, d in docs:
        if d["kind"] == "Metal3MachineTemplate":
            d["spec"]["template"]["spec"]["image"]["url"] = "set-by-patch"
    assert any("is not a 'uri'" in p for p in problems(schemas, docs))


@pytest.mark.parametrize("value,valid", [
    ("https://ironic.example.com/images/x.raw", True), ("file:///var/lib/images/x.raw", True),
    ("/var/lib/x", True), ("urn:isbn:123", True),
    ("set-by-patch", False), ("https://", False), ("https://bad%zz/x", False), ("https://a b/x", False),
])
def test_uri_format_matches_the_api_server(value, valid):
    assert cm._uri(value) is valid
