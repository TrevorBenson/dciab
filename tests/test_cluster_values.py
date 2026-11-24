"""dciab cluster-values: every example cluster renders values the capi-cluster chart accepts."""

import json
import shutil
import subprocess

import jsonschema
import pytest
import yaml

from conftest import EXAMPLES, ROOT, load_example

from dciab_cluster import cli
from dciab_cluster.values import ValuesError, cluster_names, cluster_values, unsupported

CA = "-----BEGIN CERTIFICATE-----\nMIIBdummyCertificateForTests\n-----END CERTIFICATE-----\n"
CHART = ROOT / "charts" / "capi-cluster"


def chart_defaults() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text()) or {}


def chart_schema() -> dict:
    return json.loads((CHART / "values.schema.json").read_text())


def every_cluster():
    for name in EXAMPLES:
        doc = load_example(name)
        for cluster in cluster_names(doc):
            yield pytest.param(name, cluster, id=f"{name}-{cluster}")


@pytest.mark.parametrize("example_name,cluster", list(every_cluster()))
def test_values_pass_the_chart_schema(example_name, cluster):
    values = {**chart_defaults(), **cluster_values(load_example(example_name), cluster, CA)}
    jsonschema.validate(values, chart_schema())


@pytest.mark.parametrize("example_name,cluster,ci_file", [
    ("proxmox", "infra", "proxmox-values.yaml"),
    ("vsphere", "apps01", "vsphere-values.yaml"),
    ("metal3-cluster", "metal01", "metal3-values.yaml"),
])
def test_values_match_the_chart_ci_values(example_name, cluster, ci_file):
    """The chart's CI values describe the same clusters as the examples; the generator must agree."""
    ci = yaml.safe_load((CHART / "ci" / ci_file).read_text())
    got = cluster_values(load_example(example_name), cluster, CA)
    for key in ci:
        if key in ("labels",):
            continue
        if key == "nodes":
            for k in ci["nodes"]:
                if k != "trustBundle":
                    assert got["nodes"][k] == ci["nodes"][k], f"nodes.{k}"
            continue
        assert got[key] == ci[key], key


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
@pytest.mark.parametrize("ci_file,kinds", [
    ("vsphere-values.yaml", {"Cluster", "InClusterIPPool", "Secret", "VSphereFailureDomain", "VSphereDeploymentZone"}),
    ("proxmox-values.yaml", {"Cluster", "InClusterIPPool", "Secret"}),
    ("metal3-values.yaml", {"Cluster", "IPPool", "Metal3DataTemplate", "Secret"}),
])
def test_rendered_objects_are_kept_by_helm(ci_file, kinds):
    """helm uninstall must not delete the Cluster (and with it the machines), the pools it takes addresses from,
    or the Secret encryption key."""
    out = subprocess.run(["helm", "template", "c", str(CHART), "--values", str(CHART / "ci" / ci_file)],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert {d["kind"] for d in docs} == kinds
    for d in docs:
        annotations = d["metadata"].get("annotations") or {}
        assert annotations.get("helm.sh/resource-policy") == "keep", d["kind"]
        assert annotations.get("argocd.argoproj.io/sync-options") == "Prune=false,Delete=false", d["kind"]


def test_roles_labels_and_authentication():
    doc = load_example("vsphere")
    mgmt = cluster_values(doc, "mgmt", CA)
    apps = cluster_values(doc, "apps01", CA)
    assert mgmt["labels"] == {"dciab.io/role": "mgmt"} and "authentication" not in mgmt["nodes"]
    assert apps["labels"] == {"dciab.io/role": "workload", "dciab.io/baseline": "true"}
    [issuer] = apps["nodes"]["authentication"]["issuers"]
    assert issuer["url"] == "https://auth.dc1.example.com/application/o/kubernetes/"
    assert issuer["clientID"] == "kubernetes"
    assert mgmt["nodes"]["registryMirror"] == "registry.dc1.example.com"


def test_trust_bundle_keeps_only_certificates():
    key = "-----BEGIN PRIVATE KEY-----\nMIIEsecret\n-----END PRIVATE KEY-----\n"
    values = cluster_values(load_example("proxmox"), "mgmt", key + CA + CA)
    assert "PRIVATE" not in values["nodes"]["trustBundle"]
    assert values["nodes"]["trustBundle"].count("BEGIN CERTIFICATE") == 2
    with pytest.raises(ValuesError, match="no PEM certificate"):
        cluster_values(load_example("proxmox"), "mgmt", key)


def test_unknown_cluster_names_the_choices():
    with pytest.raises(ValuesError, match="mgmt, infra, apps01"):
        cluster_values(load_example("vsphere"), "nope", CA)


def test_metal3_cluster_needs_one_image():
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["images"].append({"name": "other", "file": "os-images/other.raw"})
    doc["spec"]["workloadClusters"][0]["workerPools"][0]["image"] = "other"
    with pytest.raises(ValuesError, match="one image"):
        cluster_values(doc, "metal01", CA)


def test_data_disks_are_reported_as_unsupported():
    doc = load_example("proxmox")
    doc["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"] = 300
    notes = unsupported(doc, "infra")
    assert notes and "dataDiskGiB" in notes[0]
    assert unsupported(load_example("proxmox"), "mgmt") == []


def test_cli_uses_the_imported_chain_and_writes_mode_600(site, tmp_path):
    (site.dir / "secrets" / "pki" / "chain.pem").write_text(CA)
    config = site.dir / "dciab.yaml"
    config.write_text((ROOT / "examples" / "proxmox.yaml").read_text())
    out = tmp_path / "infra-values.yaml"
    out.write_text("old")
    out.chmod(0o644)
    assert cli.main([str(config), "infra", "--root", str(ROOT), "-o", str(out)]) == 0
    values = yaml.safe_load(out.read_text())
    assert values["nodes"]["trustBundle"] == CA
    assert out.stat().st_mode & 0o777 == 0o600
    assert "trustBundle: |" in out.read_text()


def test_cli_generate_root_needs_a_trust_bundle(site, capsys):
    config = site.dir / "dciab.yaml"
    config.write_text((ROOT / "examples" / "vsphere.yaml").read_text())
    assert cli.main([str(config), "mgmt", "--root", str(ROOT)]) == 1
    assert "--trust-bundle" in capsys.readouterr().err
    bundle = site.dir / "ca.pem"
    bundle.write_text(CA)
    assert cli.main([str(config), "mgmt", "--root", str(ROOT), "--trust-bundle", str(bundle)]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["name"] == "mgmt"


def test_cli_refuses_an_invalid_config(site, capsys):
    doc = load_example("proxmox")
    doc["spec"]["clusters"]["infra"]["apiVIP"] = doc["spec"]["clusters"]["mgmt"]["apiVIP"]
    config = site.dir / "dciab.yaml"
    config.write_text(yaml.safe_dump(doc))
    assert cli.main([str(config), "infra", "--root", str(ROOT)]) == 1
    assert "dciab validate" in capsys.readouterr().err


def test_cli_lists_clusters(site, capsys):
    config = site.dir / "dciab.yaml"
    config.write_text((ROOT / "examples" / "metal3-cluster.yaml").read_text())
    assert cli.main([str(config), "--list", "--root", str(ROOT)]) == 0
    assert capsys.readouterr().out.split() == ["mgmt", "infra", "metal01"]


def test_mirrored_registries_and_pod_security_come_from_the_lock_and_defaults():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    for name in EXAMPLES:
        doc = load_example(name)
        for cluster in cluster_names(doc):
            v = cluster_values(doc, cluster, CA)
            assert v["nodes"]["mirroredRegistries"] == lock["mirroredRegistries"]
            assert v["podSecurity"]["exemptNamespaces"][0] == "kube-system"
            assert v["encryption"] == {"createSecret": True}
    custom = dict(lock, mirroredRegistries=["docker.io", "code.forgejo.org"])
    v = cluster_values(load_example("proxmox"), "mgmt", CA, custom)
    assert v["nodes"]["mirroredRegistries"] == ["docker.io", "code.forgejo.org"]


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
def test_encryption_secret_holds_an_encryption_configuration():
    out = subprocess.run(["helm", "template", "c", str(CHART), "--values", str(CHART / "ci" / "proxmox-values.yaml")],
                         check=True, capture_output=True, text=True).stdout
    import base64
    secret = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "Secret")
    assert secret["metadata"]["name"] == "infra-encryption-config"
    assert secret["metadata"]["labels"]["clusterctl.cluster.x-k8s.io/move"] == ""
    config = yaml.safe_load(base64.b64decode(secret["data"]["config.yaml"]))
    assert config["kind"] == "EncryptionConfiguration"
    providers = config["resources"][0]["providers"]
    assert config["resources"][0]["resources"] == ["secrets"]
    key = providers[0]["secretbox"]["keys"][0]["secret"]
    assert len(base64.b64decode(key)) == 32
    assert providers[-1] == {"identity": {}}
    off = subprocess.run(["helm", "template", "c", str(CHART), "--values", str(CHART / "ci" / "proxmox-values.yaml"),
                          "--set", "encryption.createSecret=false"], check=True, capture_output=True, text=True).stdout
    assert not any(d and d["kind"] == "Secret" for d in yaml.safe_load_all(off))


# --- failure domains, storage networks and time (ADR 0010) ------------------------------

from dciab_cluster.values import place_pools, split_replicas, time_servers  # noqa: E402


@pytest.mark.parametrize("replicas,n,want", [(3, 3, [1, 1, 1]), (4, 3, [2, 1, 1]), (2, 3, [1, 1, 0]), (0, 2, [0, 0])])
def test_replicas_split_as_evenly_as_they_can(replicas, n, want):
    assert split_replicas(replicas, n) == want


def test_pools_become_one_machine_deployment_per_domain():
    pools = [{"name": "general", "replicas": 4}, {"name": "db", "replicas": 2, "failureDomains": ["b", "c"]}]
    placed = place_pools(pools, ["a", "b", "c"])
    assert [(p["name"], p["replicas"], p["failureDomain"]) for p in placed] == [
        ("general-a", 2, "a"), ("general-b", 1, "b"), ("general-c", 1, "c"), ("db-b", 1, "b"), ("db-c", 1, "c")]
    assert place_pools(pools[:1], ["a"]) == [{"name": "general", "replicas": 4, "failureDomain": "a"}]
    assert place_pools(pools[:1], []) == [{"name": "general", "replicas": 4}]


def test_proxmox_domains_place_pools_and_label_nodes():
    v = cluster_values(load_example("proxmox"), "infra", CA)
    assert [p["failureDomain"] for p in v["workerPools"]] == ["pve01", "pve02", "pve03"]
    assert v["topology"] == {"region": "dc3"}        # several domains: no cluster-wide zone
    assert v["proxmox"]["allowedNodes"] == ["pve01", "pve02", "pve03"]
    bare = cluster_values(load_example("proxmox-bare"), "infra", CA)
    assert bare["topology"] == {"region": "dc3", "zone": "pve01"}
    assert bare["proxmox"]["allowedNodes"] == ["pve01"]  # the one domain's node, not every node


def test_vsphere_zones_and_cluster_modules():
    doc = load_example("vsphere")
    v = cluster_values(doc, "infra", CA)
    assert [d["name"] for d in v["vsphereFailureDomains"]] == ["az1", "az2", "az3"]
    assert v["vsphereFailureDomains"][0]["hostGroup"] == "az1-hosts"
    assert "clusterModules" not in v
    doc["spec"]["platform"]["vsphere"]["clusterModules"] = False
    doc["spec"]["failureDomains"]["domains"] = []
    v = cluster_values(doc, "infra", CA)
    assert v["clusterModules"] is False and "vsphereFailureDomains" not in v
    assert [p["name"] for p in v["workerPools"]] == ["general"]


def test_metal3_domains_come_from_the_hosts():
    v = cluster_values(load_example("metal3-cluster"), "metal01", CA)
    assert v["metal3"]["failureDomains"] == ["rack-a", "rack-b", "rack-c"]
    assert {p["addressPool"] for p in v["workerPools"]} == {"gpu"}


def test_storage_networks_for_clusters_that_ask():
    doc = load_example("proxmox")
    infra = cluster_values(doc, "infra", CA)
    assert infra["storageNetworks"] == [
        {"name": "san-a", "ipRange": "10.40.10.30-10.40.10.49", "prefix": 24, "vlan": 410, "mtu": 9000},
        {"name": "san-b", "ipRange": "10.40.20.30-10.40.20.49", "prefix": 24, "vlan": 420, "mtu": 9000}]
    assert "storageNetworks" not in cluster_values(doc, "mgmt", CA)


def test_time_servers():
    assert time_servers(load_example("proxmox")["spec"]) == ["10.30.0.123"]
    bare = load_example("proxmox-bare")["spec"]
    assert time_servers(bare) == bare["network"]["dnsServiceIPs"]
    assert cluster_values(load_example("proxmox-bare"), "mgmt", CA)["nodes"]["ntpServers"] == ["10.30.110.53", "10.30.110.54"]


def test_proxmox_without_domains_is_one_zone_on_the_template_node():
    doc = load_example("proxmox")
    doc["spec"]["failureDomains"] = {"domains": [], "notHighlyAvailable": {"reason": "one host for now"}}
    v = cluster_values(doc, "infra", CA)
    assert v["topology"]["zone"] == doc["spec"]["platform"]["proxmox"]["templateNode"]
    assert v["proxmox"]["allowedNodes"] == [doc["spec"]["platform"]["proxmox"]["templateNode"]]


def test_issuers_are_checked_like_the_class_does():
    from dciab_cluster.values import check_issuers
    a = {"name": "authentik", "url": "https://a/", "clientID": "k"}
    b = {"name": "site", "url": "https://b/", "clientID": "k"}
    assert check_issuers([a, b]) == [a, b]
    with pytest.raises(ValuesError, match="url https://a/"):
        check_issuers([a, dict(b, url="https://a/")])
    with pytest.raises(ValuesError, match="usernamePrefix"):
        check_issuers([a, dict(b, usernamePrefix="authentik:")])
    with pytest.raises(ValuesError, match="1 to 4"):
        check_issuers([dict(a, name=f"i{n}", url=f"https://{n}/") for n in range(5)])
    v = cluster_values(load_example("vsphere"), "apps01", CA)
    [issuer] = v["nodes"]["authentication"]["issuers"]
    assert issuer["usernamePrefix"] == issuer["groupsPrefix"] == "authentik:"


def test_vsphere_storage_ci_values_come_from_the_example():
    """ci/vsphere-storage-values.yaml: vsphere.yaml's INFRA with storage networks and an MTU."""
    import copy
    doc = copy.deepcopy(load_example("vsphere"))
    doc["spec"]["network"]["nodes"]["mtu"] = 9000
    doc["spec"]["network"]["storage"] = [
        {"name": "san-a", "cidr": "10.40.10.0/24", "portGroup": "dvpg-san-a", "mtu": 9000},
        {"name": "san-b", "cidr": "10.40.20.0/24", "portGroup": "dvpg-san-b", "mtu": 9000}]
    doc["spec"]["clusters"]["infra"]["storageIPRanges"] = {"san-a": "10.40.10.30-10.40.10.49",
                                                           "san-b": "10.40.20.30-10.40.20.49"}
    ci = yaml.safe_load((CHART / "ci" / "vsphere-storage-values.yaml").read_text())
    got = cluster_values(doc, "infra", CA)
    for key in ci:
        if key in ("name", "nodes"):
            continue
        assert got[key] == ci[key], key
    assert ci["nodes"]["mtu"] == 9000 and len(ci["storageNetworks"]) == 2


def test_a_cluster_without_workers_gets_a_schedulable_control_plane():
    """#112: MGMT with workerPools: [] must be able to run cert-manager and the rest."""
    doc = load_example("proxmox-bare")
    assert cluster_values(doc, "mgmt", CA)["controlPlane"]["schedulable"] is True
    assert "schedulable" not in cluster_values(doc, "infra", CA)["controlPlane"]
    for pool in doc["spec"]["clusters"]["infra"]["workerPools"]:
        pool["replicas"] = 0
    assert cluster_values(doc, "infra", CA)["controlPlane"]["schedulable"] is True
    metal = load_example("metal3-cluster")
    assert cluster_values(metal, "mgmt", CA)["controlPlane"]["schedulable"] is True


def _render(values: dict):
    return subprocess.run(["helm", "template", "c", str(CHART), "--values", "-"], input=yaml.safe_dump(values),
                          capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
def test_chart_passes_schedulable_and_refuses_a_cluster_with_nowhere_to_run_pods():
    values = yaml.safe_load((CHART / "ci" / "proxmox-values.yaml").read_text())
    r = _render(values)
    assert r.returncode == 0, r.stderr
    assert "controlPlaneSchedulable" not in r.stdout
    values["workerPools"] = []
    r = _render(values)
    assert r.returncode != 0 and "controlPlane.schedulable" in r.stderr
    values["controlPlane"]["schedulable"] = True
    r = _render(values)
    assert r.returncode == 0, r.stderr
    cluster = next(d for d in yaml.safe_load_all(r.stdout) if d and d["kind"] == "Cluster")
    assert {"name": "controlPlaneSchedulable", "value": True} in cluster["spec"]["topology"]["variables"]
