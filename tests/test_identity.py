"""spec.identity (ADR 0008, decision 3): the schema, validate's rules, the Kubernetes issuers
per tier, the platform role bindings, the Authentik blueprints and the clients list."""

import copy
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, errors, load_example, warnings

import dciab_identity as idp
from dciab_cluster.values import ValuesError, cluster_values

CA = "-----BEGIN CERTIFICATE-----\nMIIBdummyCertificateForTests\n-----END CERTIFICATE-----\n"
ISSUER_CA = "-----BEGIN CERTIFICATE-----\nMIIBdummyIssuerCAForCIOnly\n-----END CERTIFICATE-----\n"
CHART = ROOT / "charts" / "capi-cluster"
ALL_CLIENTS = {c: {"clientID": f"dc3-{c}", **({} if c == "kubernetes" else {"clientSecret": {"file": "./secrets/vsphere.pw"}})}
               for c in idp.OIDC_CONSUMERS if c != "proxmox"}


def oidc_site() -> dict:
    """examples/proxmox.yaml with the site's own IdP (provider external) for Kubernetes, the
    three roles, and a second site issuer: the config behind ci/proxmox-oidc-values.yaml."""
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["identity"].update({
        "provider": "external",
        "roles": {"operator": "dc3-platform-operators", "viewer": "dc3-platform-viewers"},
        "consumers": ["kubernetes"],
        "external": {"issuer": "https://sso.example.com/realms/dc3",
                     "claims": {"username": "email", "groups": "groups"},
                     "caBundle": {"file": "./secrets/sso-ca.pem"},
                     "clients": {"kubernetes": {"clientID": "dc3-kubernetes"}}},
        "kubernetes": {"issuers": [{"name": "corp", "url": "https://login.corp.example.com/", "clientID": "k8s-dc3"}]},
    })
    return doc


def with_ca(tmp_path):
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "sso-ca.pem").write_text(ISSUER_CA)
    return tmp_path


def names(issuers):
    return [i["name"] for i in issuers]


# --- issuers per tier ------------------------------------------------------------------

def test_authentik_default_serves_workload_clusters_only():
    """No provider: today's behaviour. MGMT and INFRA never trust Authentik, which runs on INFRA."""
    spec = load_example("vsphere")["spec"]
    assert idp.provider(spec) == "authentik"
    assert idp.tier_issuers(spec, "mgmt") == [] and idp.tier_issuers(spec, "infra") == []
    [a] = idp.tier_issuers(spec, "workload")
    assert a["url"] == "https://auth.dc1.example.com/application/o/kubernetes/" and a["clientID"] == "kubernetes"


def test_external_issuer_serves_every_tier_with_its_prefix(tmp_path):
    doc = oidc_site()
    for tier in idp.TIERS:
        assert names(idp.tier_issuers(doc["spec"], tier)) == ["site", "corp"]
    v = cluster_values(doc, "mgmt", CA, config_dir=with_ca(tmp_path))
    site, corp = v["nodes"]["authentication"]["issuers"]
    assert site == {"name": "site", "url": "https://sso.example.com/realms/dc3", "clientID": "dc3-kubernetes",
                    "usernameClaim": "email", "usernamePrefix": "site:", "groupsClaim": "groups",
                    "groupsPrefix": "site:", "certificateAuthority": ISSUER_CA}
    assert corp["usernamePrefix"] == corp["groupsPrefix"] == "corp:" and corp["usernameClaim"] == "preferred_username"
    assert "certificateAuthority" not in corp


def test_authentik_with_a_site_issuer_gives_mgmt_only_the_site_issuer():
    doc = load_example("vsphere")
    doc["spec"]["identity"]["kubernetes"] = {"issuers": [
        {"name": "corp", "url": "https://login.corp.example.com/", "clientID": "k8s"}]}
    assert names(idp.tier_issuers(doc["spec"], "mgmt")) == ["corp"]
    assert names(idp.tier_issuers(doc["spec"], "infra")) == ["corp"]
    assert names(idp.tier_issuers(doc["spec"], "workload")) == ["authentik", "corp"]
    assert names(cluster_values(doc, "mgmt", CA)["nodes"]["authentication"]["issuers"]) == ["corp"]


def test_authentik_as_an_extra_issuer_defaults_to_workload_clusters():
    """Pivoting from Authentik to the site's IdP: Authentik stays on workload clusters for a while."""
    doc = oidc_site()
    doc["spec"]["identity"]["kubernetes"]["issuers"].append(
        {"name": "authentik", "url": "https://auth.dc3.example.com/application/o/kubernetes/", "clientID": "kubernetes"})
    assert names(idp.tier_issuers(doc["spec"], "mgmt")) == ["site", "corp"]
    assert names(idp.tier_issuers(doc["spec"], "workload")) == ["site", "corp", "authentik"]


def test_kubernetes_left_out_of_consumers_drops_the_providers_issuer():
    doc = load_example("vsphere")
    doc["spec"]["identity"]["consumers"] = ["argocd"]
    assert idp.tier_issuers(doc["spec"], "workload") == []
    assert "authentication" not in cluster_values(doc, "apps01", CA)["nodes"]


def test_a_ca_bundle_file_needs_the_config_folder():
    with pytest.raises(ValuesError, match="no config folder"):
        cluster_values(oidc_site(), "mgmt", CA)


def test_ci_values_come_from_the_oidc_site(tmp_path):
    """ci/proxmox-oidc-values.yaml: MGMT on Proxmox trusting the site's IdP and a second site issuer."""
    ci = yaml.safe_load((CHART / "ci" / "proxmox-oidc-values.yaml").read_text())
    got = cluster_values(oidc_site(), "mgmt", CA, config_dir=with_ca(tmp_path))
    for key in ci:
        if key == "nodes":
            for k in ci["nodes"]:
                if k != "trustBundle":
                    assert got["nodes"][k] == ci["nodes"][k], f"nodes.{k}"
            continue
        assert got[key] == ci[key], key


# --- role bindings ---------------------------------------------------------------------

def test_role_bindings_per_issuer_and_role():
    doc = oidc_site()
    got = {b["metadata"]["name"]: b for b in idp.role_bindings(doc["spec"], "mgmt")}
    assert sorted(got) == sorted(f"dciab-{r}-{i}" for r in idp.ROLES for i in ("site", "corp"))
    admin = got["dciab-admin-site"]
    assert admin["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}
    assert admin["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group",
                                  "name": "site:dc3-platform-admins"}]
    assert got["dciab-operator-corp"]["roleRef"]["name"] == "edit"
    assert got["dciab-viewer-corp"]["subjects"][0]["name"] == "corp:dc3-platform-viewers"
    assert all(b["metadata"]["labels"]["dciab.io/identity"] == "roles" for b in got.values())


def test_no_issuer_no_bindings_and_admin_only_without_roles():
    spec = load_example("vsphere")["spec"]
    assert idp.role_bindings(spec, "mgmt") == [] and idp.role_bindings(spec, "infra") == []
    [b] = idp.role_bindings(spec, "apps01")
    assert b["metadata"]["name"] == "dciab-admin-authentik"
    assert b["subjects"][0]["name"] == "authentik:" + spec["identity"]["adminGroup"]


def test_cli_rolebindings_names_and_issuers(tmp_path):
    config = tmp_path / "dciab.yaml"
    config.write_text(yaml.safe_dump(oidc_site()))

    def run(*args):
        r = subprocess.run([sys.executable, "-m", "dciab_identity", *args], capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "lib"), "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        return r.stdout

    docs = list(yaml.safe_load_all(run("rolebindings", str(config), "mgmt")))
    assert len(docs) == 6 and {d["kind"] for d in docs} == {"ClusterRoleBinding"}
    assert run("names", str(config), "mgmt").split() == [d["metadata"]["name"] for d in docs]
    assert run("issuers", str(config), "infra").splitlines() == [
        "site https://sso.example.com/realms/dc3", "corp https://login.corp.example.com/"]
    config.write_text(yaml.safe_dump(load_example("proxmox")))
    assert run("issuers", str(config), "mgmt").strip() == "none"
    assert run("rolebindings", str(config), "mgmt").strip() == ""


def test_mgmt_step_applies_the_bindings_and_removes_stale_ones():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    assert "adopt identity storage" in text
    step = text.split("step identity\n", 1)[1].split("finish_if identity", 1)[0]
    assert "dciab_identity rolebindings" in step and "--server-side" in step
    assert "-l dciab.io/identity=roles" in step and "delete" in step


# --- Authentik blueprints and the clients list -------------------------------------------

class TagLoader(yaml.SafeLoader):
    pass


TagLoader.add_multi_constructor("!", lambda loader, suffix, node: (
    idp.Tag("!" + suffix, loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
            else loader.construct_scalar(node))))


def test_blueprints_round_trip_with_their_tags():
    doc = load_example("vsphere")
    doc["spec"]["identity"]["roles"] = {"viewer": "dc1-viewers"}
    cm = idp.blueprint_configmap(doc["spec"])
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["namespace"] == "authentik"
    files = {name: yaml.load(text, Loader=TagLoader) for name, text in cm["data"].items()}
    assert sorted(files) == sorted(["dciab-groups.yaml"] + [f"dciab-client-{c}.yaml" for c in idp.DEFAULT_CONSUMERS
                                                            if c in idp.OIDC_CONSUMERS])
    assert files == idp.blueprints(doc["spec"])
    for bp in files.values():
        assert bp["version"] == 1 and bp["entries"]
    groups = files["dciab-groups.yaml"]["entries"]
    assert [e["identifiers"] for e in groups if e["model"] == "authentik_core.group"] == [
        {"name": doc["spec"]["identity"]["adminGroup"]}, {"name": "dc1-viewers"}]
    [user] = [e for e in groups if e["model"] == "authentik_core.user"]
    assert user["attrs"]["groups"] == [idp.Tag("!KeyOf", "group-admin")]


def test_kubernetes_client_matches_the_issuer_and_is_public():
    spec = load_example("vsphere")["spec"]
    provider, app = idp.blueprints(spec)["dciab-client-kubernetes.yaml"]["entries"]
    assert app["identifiers"] == {"slug": "kubernetes"}   # .../application/o/kubernetes/
    assert provider["attrs"]["client_id"] == "kubernetes" and provider["attrs"]["client_type"] == "public"
    assert "client_secret" not in provider["attrs"]
    argo = idp.blueprints(spec)["dciab-client-argocd.yaml"]["entries"][0]["attrs"]
    assert argo["client_type"] == "confidential" and argo["client_secret"] == idp.Tag("!Env", "DCIAB_OIDC_ARGOCD_SECRET")
    assert argo["redirect_uris"] == [{"matching_mode": "strict", "url": "https://argocd.dc1.example.com/auth/callback"}]


def test_no_blueprints_for_an_external_idp():
    with pytest.raises(idp.IdentityError, match="external"):
        idp.blueprints(oidc_site()["spec"])


def test_clients_list_for_the_site_idp_team():
    doc = load_example("proxmox")
    doc["spec"]["identity"]["consumers"] = ["kubernetes", "argocd", "openvox", "proxmox"]
    got = {c["consumer"]: c for c in idp.clients(doc["spec"])}
    assert sorted(got) == ["argocd", "kubernetes", "proxmox"]   # openvox reads LDAP
    assert got["kubernetes"]["public"] and not got["argocd"]["public"]
    assert got["proxmox"]["redirects"] == ["https://pve01.dc3.example.net:8006"]


# --- schema and validate ----------------------------------------------------------------

def external(doc, **ext):
    doc["spec"]["identity"]["provider"] = "external"
    doc["spec"]["identity"]["external"] = dict({"issuer": "https://sso.example.com/realms/dc1",
                                                "claims": {"username": "preferred_username", "groups": "groups"},
                                                "clients": copy.deepcopy(ALL_CLIENTS),
                                                "ldap": {"url": "ldaps://ad.example.com", "bindDN": "CN=dciab",
                                                         "bindPassword": {"file": "./secrets/vsphere.pw"},
                                                         "userBaseDN": "OU=People", "groupBaseDN": "OU=Groups"}}, **ext)
    return doc


def test_oidc_site_is_valid(site):
    with_ca(site.dir)
    code, report = site.validate(oidc_site())
    assert errors(report) == [] and code == 0
    assert not any("trusts no OIDC issuer" in n for n in report["notes"])


def test_external_with_every_client_is_valid(site):
    code, report = site.validate(external(load_example("vsphere")))
    assert errors(report) == [] and warnings(report) == [] and code == 0


def test_examples_note_that_mgmt_uses_certificates_only(site):
    _, report = site.validate(load_example("vsphere"))
    assert any("trusts no OIDC issuer" in n for n in report["notes"])


@pytest.mark.parametrize("change,path", [
    (lambda i: i.update(provider="external"), "spec.identity.external"),
    (lambda i: i.update(external={"issuer": "https://x/", "claims": {"username": "u", "groups": "g"}, "clients": {}}),
     "spec.identity"),
    (lambda i: i.update(provider="keycloak"), "spec.identity.provider"),
    (lambda i: i.update(consumers=["jenkins"]), "spec.identity.consumers[0]"),
    (lambda i: i.update(kubernetes={"issuers": [{"name": "Corp", "url": "https://x/", "clientID": "k"}]}),
     "spec.identity.kubernetes.issuers[0].name"),
    (lambda i: i.update(kubernetes={"issuers": [{"name": "corp", "url": "http://x/", "clientID": "k"}]}),
     "spec.identity.kubernetes.issuers[0].url"),
])
def test_schema_rejects(site, change, path):
    doc = load_example("vsphere")
    change(doc["spec"]["identity"])
    code, report = site.validate(doc)
    assert code == 1 and any(f["path"].startswith(path) for f in errors(report)), errors(report)


def test_external_needs_a_client_per_consumer_and_secrets(site):
    doc = external(load_example("vsphere"))
    clients = doc["spec"]["identity"]["external"]["clients"]
    del clients["grafana"]
    del clients["argocd"]["clientSecret"]
    code, report = site.validate(doc)
    assert code == 1
    assert errors(report, "spec.identity.external.clients.grafana")[0]["found"] == "missing"
    assert errors(report, "spec.identity.external.clients.argocd.clientSecret")


def test_external_ldap_and_vcenter_rules(site):
    doc = external(load_example("vsphere"))
    del doc["spec"]["identity"]["external"]["ldap"]
    doc["spec"]["identity"]["consumers"] = list(idp.DEFAULT_CONSUMERS) + ["vcenter"]
    code, report = site.validate(doc)
    assert "break-glass" in warnings(report, "spec.identity.external")[0]["found"]
    assert "vCenter" in errors(report, "spec.identity.external")[0]["found"]
    doc["spec"]["identity"]["external"]["vcenterFederation"] = True
    _, report = site.validate(doc)
    assert errors(report) == []


def test_external_issuer_cant_be_dciabs_authentik(site):
    doc = external(load_example("vsphere"), issuer="https://auth.dc1.example.com/application/o/kubernetes/")
    code, report = site.validate(doc)
    assert code == 1 and errors(report, "spec.identity.external.issuer")


def test_tier_rule_mgmt_and_infra_never_trust_authentik(site):
    doc = load_example("vsphere")
    doc["spec"]["identity"]["kubernetes"] = {"issuers": [
        {"name": "old", "url": "https://auth.dc1.example.com/application/o/k8s/", "clientID": "k8s",
         "tiers": ["mgmt", "workload"]}]}
    code, report = site.validate(doc)
    [f] = errors(report, "spec.identity.kubernetes.issuers[0].tiers")
    assert "INFRA" in f["found"] and "decision 2" in f["fix"]


def test_issuer_names_and_urls_are_unique(site):
    doc = load_example("vsphere")
    doc["spec"]["identity"]["kubernetes"] = {"issuers": [
        {"name": "authentik", "url": "https://login.corp.example.com/", "clientID": "k"},
        {"name": "corp", "url": "https://login.corp.example.com/", "clientID": "k"}]}
    code, report = site.validate(doc)
    assert code == 1
    assert errors(report, "spec.identity.kubernetes.issuers[0].name")
    assert errors(report, "spec.identity.kubernetes.issuers[1].url")


def test_at_most_four_issuers_per_tier(site):
    doc = external(load_example("vsphere"))
    doc["spec"]["identity"]["kubernetes"] = {"issuers": [
        {"name": f"i{n}", "url": f"https://i{n}.example.com/", "clientID": "k"} for n in range(3)] + []}
    doc["spec"]["identity"]["kubernetes"]["issuers"][2]["tiers"] = ["workload"]
    _, report = site.validate(doc)
    assert errors(report) == []   # four on every tier
    doc["spec"]["identity"]["consumers"] = list(idp.DEFAULT_CONSUMERS)
    doc["spec"]["identity"]["kubernetes"]["issuers"].append(
        {"name": "authentik", "url": "https://auth.dc1.example.com/application/o/kubernetes/", "clientID": "kubernetes"})
    code, report = site.validate(doc)
    assert code == 1 and "workload clusters would trust 5" in errors(report, "spec.identity.kubernetes.issuers")[0]["found"]


def test_roles_need_groups_of_their_own(site):
    doc = load_example("vsphere")
    admins = doc["spec"]["identity"]["adminGroup"]
    doc["spec"]["identity"]["roles"] = {"operator": admins, "viewer": "v"}
    code, report = site.validate(doc)
    assert code == 1 and errors(report, "spec.identity.roles.operator")
    doc["spec"]["identity"]["roles"] = {"operator": "v", "viewer": "v"}
    _, report = site.validate(doc)
    assert errors(report, "spec.identity.roles.viewer")


def test_hypervisor_consumers_on_the_wrong_platform_warn(site):
    doc = load_example("vsphere")
    doc["spec"]["identity"]["consumers"] = ["kubernetes", "proxmox"]
    _, report = site.validate(doc)
    assert "proxmox" in warnings(report, "spec.identity.consumers")[0]["found"]


def test_ca_bundle_must_hold_a_certificate(site):
    (site.dir / "secrets" / "sso-ca.pem").write_text("not a certificate\n")
    (site.dir / "secrets" / "sso-ca.pem").chmod(0o600)
    code, report = site.validate(oidc_site())
    assert code == 1 and errors(report, "spec.identity.external.caBundle")


# --- system: stays Kubernetes' own ---------------------------------------------------------

@pytest.mark.parametrize("where", ["external", "kubernetes"])
@pytest.mark.parametrize("name", ["system"])
def test_issuer_names_cant_reach_system(site, where, name):
    """An issuer named system would map the IdP's group masters to system:masters."""
    with_ca(site.dir)
    doc = oidc_site()
    ident = doc["spec"]["identity"]
    if where == "external":
        ident["external"]["name"] = name
        path = "spec.identity.external.name"
    else:
        ident["kubernetes"]["issuers"][0]["name"] = name
        path = "spec.identity.kubernetes.issuers[0].name"
    code, report = site.validate(doc)
    [f] = errors(report, path)
    assert code == 1 and "system" in f["fix"]


@pytest.mark.parametrize("prefix", ["system:", "system:x:", "sys", "s", "system"])
def test_values_refuse_a_prefix_that_reaches_system(prefix):
    from dciab_cluster.values import check_issuers
    a = {"name": "site", "url": "https://a/", "clientID": "k"}
    with pytest.raises(ValuesError, match="system:"):
        check_issuers([dict(a, groupsPrefix=prefix)])
    with pytest.raises(ValuesError, match="system:"):
        check_issuers([dict(a, usernamePrefix=prefix)])
    assert check_issuers([dict(a, groupsPrefix="sso:")])


def test_chart_schema_refuses_a_system_issuer_name():
    import json
    import jsonschema
    schema = json.loads((CHART / "values.schema.json").read_text())
    values = yaml.safe_load((CHART / "ci" / "proxmox-oidc-values.yaml").read_text())
    values = {**(yaml.safe_load((CHART / "values.yaml").read_text()) or {}), **values}
    jsonschema.validate(values, schema)
    values["nodes"]["authentication"]["issuers"][1]["name"] = "system"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(values, schema)
    for name in ("systemd", "systems-team"):   # only system itself gives the prefix system:
        values["nodes"]["authentication"]["issuers"][1]["name"] = name
        jsonschema.validate(values, schema)


@pytest.mark.parametrize("cls", ["vsphere", "proxmox", "metal3"])
def test_classes_refuse_prefixes_that_reach_system(cls):
    """The r1 classes' CEL rules, per issuer: neither prefix starts with system: nor is the start of it."""
    docs = [d for d in yaml.safe_load_all((ROOT / "charts/capi-clusterclasses/classes/r1" / f"{cls}.yaml").read_text()) if d]
    cc = next(d for d in docs if d["kind"] == "ClusterClass")
    var = next(v for v in cc["spec"]["variables"] if v["name"] == "authentication")
    items = var["schema"]["openAPIV3Schema"]["properties"]["issuers"]["items"]
    rules = [r["rule"] for r in items["x-kubernetes-validations"]]
    assert len(rules) == 2
    for rule, key in zip(rules, ("usernamePrefix", "groupsPrefix")):
        assert f"self.{key}" in rule and ".startsWith('system:')" in rule and "'system:'.startsWith(" in rule
    # Bounded cost: the prefixes and names they read have a maxLength.
    for k in ("name", "usernamePrefix", "groupsPrefix"):
        assert items["properties"][k]["maxLength"] <= 63


@pytest.mark.parametrize("name", ["systemd", "systems-team"])
def test_names_that_only_begin_with_system_are_fine(site, name):
    """Their prefixes (systemd:, systems-team:) don't reach system:."""
    with_ca(site.dir)
    doc = oidc_site()
    doc["spec"]["identity"]["external"]["name"] = name
    code, report = site.validate(doc)
    assert errors(report) == [] and code == 0
    from dciab_cluster.values import check_issuers
    assert check_issuers([{"name": name, "url": "https://a/", "clientID": "k"}])
