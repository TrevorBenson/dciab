import copy
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CLASSES = ROOT / "charts" / "capi-clusterclasses" / "classes"


def _load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load(name):
    return _load_path(name, ROOT / "hack" / f"{name}.py")


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
    assert re.fullmatch(r"(|[0-9a-f]{64})", lock["chartDigests"]["cilium"])


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


KEEP = {"helm.sh/resource-policy": "keep", "argocd.argoproj.io/sync-options": "Prune=false,Delete=false"}


def test_installed_classes_are_kept_by_helm():
    """Deleting a class or template MGMT uses breaks MGMT. Helm must leave them on uninstall or prune."""
    for v in installed_revisions():
        for f in (CLASSES / v).glob("*.yaml"):
            for d in yaml.safe_load_all(f.read_text()):
                if d:
                    assert KEEP.items() <= (d["metadata"].get("annotations") or {}).items(), \
                        f"{f.name}: {d['kind']} {d['metadata']['name']}"


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
def test_rendered_classes_are_kept_by_helm():
    out = subprocess.run(["helm", "template", "classes", str(ROOT / "charts" / "capi-clusterclasses"),
                          "--namespace", NS], check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert sum(d["kind"] == "ClusterClass" for d in docs) == 3 * len(installed_revisions())
    for d in docs:
        assert KEEP.items() <= (d["metadata"].get("annotations") or {}).items(), f"{d['kind']} {d['metadata']['name']}"


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


# --- API server audit logging ----------------------------------------------------------

AUDIT_POLICY = "/etc/kubernetes/audit/policy.yaml"
AUDIT_DIR = "/var/log/kubernetes/audit"
AUDIT_ARGS = {
    "audit-policy-file": AUDIT_POLICY,
    "audit-log-path": f"{AUDIT_DIR}/audit.log",
    "audit-log-maxage": "30",
    "audit-log-maxbackup": "10",
    "audit-log-maxsize": "100",
}


def _installed_class_files():
    return [f for rev in installed_revisions() for f in sorted((CLASSES / rev).glob("*.yaml"))]


def _audit_policy(path):
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    cc = next(d for d in docs if d["kind"] == "ClusterClass")
    names = [p["name"] for p in cc["spec"]["patches"]]
    # nodeBase creates the files list, so the audit file must be appended after it.
    assert names.index("apiServerAudit") > names.index("nodeBase"), path
    ops = cc["spec"]["patches"][names.index("apiServerAudit")]["definitions"][0]["jsonPatches"]
    files = [op["value"] for op in ops if op["path"] == "/spec/template/spec/kubeadmConfigSpec/files/-"]
    policy = next(f for f in files if f["path"] == AUDIT_POLICY)
    return policy, yaml.safe_load(policy["content"])


def _level(policy, user="alice", groups=("system:authenticated",), verb="get", group="", resource=None,
           namespace="default", name="", url=None):
    """The level of the first matching rule, as the API server picks it."""
    for rule in policy["rules"]:
        if rule.get("users") and user not in rule["users"]:
            continue
        if rule.get("userGroups") and not set(groups) & set(rule["userGroups"]):
            continue
        if rule.get("verbs") and verb not in rule["verbs"]:
            continue
        if url is not None:
            if rule.get("resources") or rule.get("namespaces"):
                continue
            if rule.get("nonResourceURLs") and not any(
                    url == u or (u.endswith("*") and url.startswith(u[:-1])) for u in rule["nonResourceURLs"]):
                continue
        else:
            if rule.get("nonResourceURLs"):
                continue
            if rule.get("namespaces") and namespace not in rule["namespaces"]:
                continue
            if rule.get("resources") and not any(
                    r.get("group", "") == group and (not r.get("resources") or resource in r["resources"])
                    and (not r.get("resourceNames") or name in r["resourceNames"])
                    for r in rule["resources"]):
                continue
        return rule["level"]
    return "None"


def test_installed_classes_set_api_server_audit_flags_and_mounts():
    for f in _installed_class_files():
        kcp = next(d for d in yaml.safe_load_all(f.read_text()) if d and d["kind"] == "KubeadmControlPlaneTemplate")
        api = kcp["spec"]["template"]["spec"]["kubeadmConfigSpec"]["clusterConfiguration"]["apiServer"]
        args = {a["name"]: a["value"] for a in api["extraArgs"]}
        assert {k: args.get(k) for k in AUDIT_ARGS} == AUDIT_ARGS, f.name
        vols = {v["mountPath"]: v for v in api.get("extraVolumes", [])}
        assert vols[AUDIT_POLICY]["hostPath"] == AUDIT_POLICY and vols[AUDIT_POLICY]["readOnly"] is True, f.name
        assert vols[AUDIT_DIR]["hostPath"] == AUDIT_DIR and vols[AUDIT_DIR]["readOnly"] is False, f.name


def test_installed_classes_write_the_audit_policy():
    for f in _installed_class_files():
        file, policy = _audit_policy(f)
        assert file["permissions"] == "0600", f.name
        assert policy["apiVersion"] == "audit.k8s.io/v1" and policy["kind"] == "Policy", f.name
        assert policy["omitStages"] == ["RequestReceived"], f.name
        assert policy["rules"][-1] == {"level": "Metadata"}, f.name


@pytest.mark.parametrize("request_, level", [
    ({"verb": "create", "resource": "secrets"}, "Metadata"),
    ({"verb": "update", "resource": "configmaps"}, "Metadata"),
    ({"verb": "create", "group": "authentication.k8s.io", "resource": "tokenreviews"}, "Metadata"),
    ({"verb": "create", "resource": "serviceaccounts/token"}, "Metadata"),
    ({"verb": "list", "resource": "secrets", "user": "system:kube-controller-manager"}, "Metadata"),
    ({"verb": "create", "group": "rbac.authorization.k8s.io", "resource": "clusterrolebindings"}, "RequestResponse"),
    ({"verb": "delete", "group": "rbac.authorization.k8s.io", "resource": "roles"}, "RequestResponse"),
    ({"verb": "get", "group": "rbac.authorization.k8s.io", "resource": "roles"}, "Metadata"),
    ({"verb": "delete", "group": "apps", "resource": "deployments"}, "Metadata"),
    ({"verb": "get", "resource": "pods"}, "Metadata"),
    ({"verb": "get", "url": "/healthz"}, "None"),
    ({"verb": "get", "url": "/readyz/etcd"}, "None"),
    ({"verb": "get", "url": "/apis"}, "Metadata"),
    ({"verb": "create", "resource": "events"}, "None"),
    ({"verb": "update", "group": "coordination.k8s.io", "resource": "leases",
      "user": "system:node:n1", "groups": ("system:nodes",), "namespace": "kube-node-lease"}, "None"),
    ({"verb": "update", "group": "coordination.k8s.io", "resource": "leases",
      "user": "kubernetes-admin", "namespace": "kube-system", "name": "plndr-cp-lock"}, "None"),
    ({"verb": "update", "group": "coordination.k8s.io", "resource": "leases", "namespace": "team-a"}, "Metadata"),
    ({"verb": "watch", "resource": "endpoints", "user": "system:kube-proxy"}, "None"),
    ({"verb": "get", "resource": "nodes", "user": "system:node:n1", "groups": ("system:nodes",)}, "None"),
    ({"verb": "patch", "resource": "nodes", "user": "system:node:n1", "groups": ("system:nodes",)}, "Metadata"),
])
def test_audit_policy_levels(request_, level):
    for f in _installed_class_files():
        assert _level(_audit_policy(f)[1], **request_) == level, f"{f.name}: {request_}"


def test_audit_policy_is_the_same_in_every_class():
    policies = {f.name: _audit_policy(f)[0]["content"] for f in _installed_class_files()}
    assert len(set(policies.values())) == 1, sorted(policies)


def test_e2e_verifier_accepts_the_class_audit_settings():
    """hack/e2e/verify.py checks the same audit settings the classes set."""
    pytest.importorskip("tomllib")  # verify.py runs on Python 3.11+
    verify = _load_path("e2e_verify", ROOT / "hack" / "e2e" / "verify.py")
    assert verify.AUDIT_ARGS == AUDIT_ARGS
    for f in _installed_class_files():
        kcp = next(d for d in yaml.safe_load_all(f.read_text()) if d and d["kind"] == "KubeadmControlPlaneTemplate")
        api = kcp["spec"]["template"]["spec"]["kubeadmConfigSpec"]["clusterConfiguration"]["apiServer"]
        args = {a["name"]: a["value"] for a in api["extraArgs"]}
        file, _ = _audit_policy(f)
        v = verify.Verifier({"name": f.stem})
        v.check_audit({file["path"]: file["content"]}, api, args)
        assert v.failures == [], f.name
        v.check_audit({}, {}, {})
        assert any("audit policy file" in m for m in v.failures)
        assert any("audit-log-path" in m for m in v.failures)


def test_proxmox_kubelet_sets_provider_id():
    """Without it, Nodes have no spec.providerID and Machines never reach Running (task 0003 run 3)."""
    docs = list(yaml.safe_load_all((CLASSES / "r1" / "proxmox.yaml").read_text()))
    want = {"name": "provider-id", "value": "proxmox://{{ ds.meta_data.instance_id }}"}
    regs = []
    for d in docs:
        if not d:
            continue
        if d["kind"] == "KubeadmControlPlaneTemplate":
            spec = d["spec"]["template"]["spec"]["kubeadmConfigSpec"]
            regs += [spec["initConfiguration"]["nodeRegistration"], spec["joinConfiguration"]["nodeRegistration"]]
        elif d["kind"] == "KubeadmConfigTemplate":
            regs.append(d["spec"]["template"]["spec"]["joinConfiguration"]["nodeRegistration"])
    assert len(regs) == 3
    for r in regs:
        assert want in r.get("kubeletExtraArgs", [])
