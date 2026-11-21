"""dciab cluster-values: every example cluster renders values the capi-cluster chart accepts."""

import json

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


def test_roles_labels_and_oidc():
    doc = load_example("vsphere")
    mgmt = cluster_values(doc, "mgmt", CA)
    apps = cluster_values(doc, "apps01", CA)
    assert mgmt["labels"] == {"dciab.io/role": "mgmt"} and "oidc" not in mgmt["nodes"]
    assert apps["labels"] == {"dciab.io/role": "workload", "dciab.io/baseline": "true"}
    assert apps["nodes"]["oidc"]["issuerURL"] == "https://auth.dc1.example.com/application/o/kubernetes/"
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
    notes = unsupported(load_example("proxmox"), "infra")
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
