import os
import subprocess

from conftest import ROOT, errors, load_example, warnings


# --- the shipped examples ---------------------------------------------------------

def test_examples_are_valid(site, example):
    name, doc = example
    code, report = site.validate(doc)
    assert errors(report) == [], f"{name}: {errors(report)}"
    assert warnings(report) == [], f"{name}: {warnings(report)}"
    assert code == 0 and report["valid"] is True


def test_live_checks_are_reported_as_not_run(site):
    _, report = site.validate(load_example("vsphere"))
    assert any("live checks" in n for n in report["notes"])


# --- YAML problems ------------------------------------------------------------------

def test_duplicate_key_is_rejected(site):
    text = (ROOT / "examples" / "proxmox.yaml").read_text()
    text = text.replace("  kubernetes:\n", "  kubernetes:\n    version: v1.33.0\n", 1)
    code, report = site.validate_text(text)
    assert code == 1
    [f] = errors(report)
    assert "duplicate key 'version'" in f["found"]
    assert f["line"] is not None


def test_unquoted_numeric_mac_gets_a_quote_hint(site):
    text = (ROOT / "examples" / "metal3-cluster.yaml").read_text()
    text = text.replace('bootMACAddress: "b8:ca:3a:01:02:01"', "bootMACAddress: 10:20:30:40:50:59")
    code, report = site.validate_text(text)
    assert code == 1
    [f] = errors(report, "spec.baremetal.hosts[0].bootMACAddress")
    assert "YAML read this as a number" in f["found"]
    assert f["fix"] == "put the value in quotes"


# --- schema ---------------------------------------------------------------------------

def test_inline_secret_is_rejected_without_echoing_it(site):
    doc = load_example("vsphere")
    doc["spec"]["platform"]["vsphere"]["credentials"]["password"] = "hunter2-SUPERSECRET"
    code, report = site.validate(doc)
    assert code == 1
    [f] = errors(report, "spec.platform.vsphere.credentials.password")
    assert "hunter2" not in f["found"] and "SUPERSECRET" not in f["found"]
    assert "{file: <path>}" in f["fix"]


def test_missing_field_is_named(site):
    doc = load_example("vsphere")
    del doc["spec"]["network"]["ntpServers"]
    code, report = site.validate(doc)
    assert code == 1
    assert errors(report, "spec.network.ntpServers")[0]["found"] == "missing"


def test_unknown_field_is_rejected(site):
    doc = load_example("vsphere")
    doc["spec"]["network"]["ntpServer"] = ["10.0.0.123"]
    _, report = site.validate(doc)
    assert errors(report, "spec.network.ntpServer")


def test_bgp_mode_needs_bgp_block(site):
    doc = load_example("vsphere")
    del doc["spec"]["network"]["loadBalancer"]["bgp"]
    _, report = site.validate(doc)
    assert errors(report, "spec.network.loadBalancer.bgp")


def test_l2_mode_rejects_bgp_block(site):
    doc = load_example("vsphere")
    doc["spec"]["network"]["loadBalancer"]["mode"] = "l2"
    _, report = site.validate(doc)
    assert "remove bgp" in errors(report, "spec.network.loadBalancer")[0]["fix"]


def test_both_platform_blocks_rejected(site):
    doc = load_example("vsphere")
    doc["spec"]["platform"]["proxmox"] = load_example("proxmox")["spec"]["platform"]["proxmox"]
    _, report = site.validate(doc)
    assert errors(report, "spec.platform")


def test_baremetal_disabled_rejects_other_fields(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["enabled"] = False
    del doc["spec"]["workloadClusters"]
    _, report = site.validate(doc)
    assert errors(report, "spec.baremetal.hosts")


def test_one_recipient_is_not_enough(site):
    doc = load_example("vsphere")
    doc["spec"]["bootstrap"]["recipients"] = doc["spec"]["bootstrap"]["recipients"][:1]
    _, report = site.validate(doc)
    assert errors(report, "spec.bootstrap.recipients")


# --- semantic checks ------------------------------------------------------------------

def test_vip_inside_node_range(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["infra"]["apiVIP"] = "10.20.110.25"
    code, report = site.validate(doc)
    assert code == 1
    found = [f for f in errors(report) if "10.20.110.25" in f["found"] or "apiVIP" in f["found"]]
    assert found


def test_node_range_overlapping_dns_ips(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["infra"]["nodeIPRange"] = "10.20.110.30-10.20.110.59"
    _, report = site.validate(doc)
    hits = errors(report, "spec.clusters.infra.nodeIPRange")
    assert any("dnsServiceIPs" in f["found"] for f in hits)


def test_address_outside_node_network(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["mgmt"]["apiVIP"] = "10.99.0.10"
    _, report = site.validate(doc)
    assert "outside" in errors(report, "spec.clusters.mgmt.apiVIP")[0]["found"]


def test_node_range_too_small(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["infra"]["nodeIPRange"] = "10.20.110.30-10.20.110.34"
    _, report = site.validate(doc)
    assert "widen it to at least 8" in errors(report, "spec.clusters.infra.nodeIPRange")[0]["fix"]


def test_overlapping_pod_cidrs(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["infra"]["podCIDR"] = "10.240.0.0/16"
    _, report = site.validate(doc)
    assert errors(report, "spec.clusters.infra.podCIDR")


def test_cidr_with_host_bits(site):
    doc = load_example("vsphere")
    doc["spec"]["network"]["nodes"]["cidr"] = "10.20.110.5/24"
    _, report = site.validate(doc)
    assert "network address is 10.20.110.0/24" in errors(report, "spec.network.nodes.cidr")[0]["fix"]


def test_vm_class_must_match_platform(site):
    doc = load_example("vsphere")
    doc["spec"]["workloadClusters"][0]["class"] = "proxmox"
    _, report = site.validate(doc)
    assert "use class: vsphere" in errors(report, "spec.workloadClusters[0].class")[0]["fix"]


def test_metal3_class_needs_baremetal(site):
    doc = load_example("vsphere")
    metal = load_example("metal3-cluster")["spec"]["workloadClusters"][0]
    doc["spec"]["workloadClusters"].append(metal)
    _, report = site.validate(doc)
    assert errors(report, "spec.workloadClusters[1].class")


def test_unknown_network_reference(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["workloadClusters"][0]["workerPools"][0]["network"] = "metal-999"
    _, report = site.validate(doc)
    assert errors(report, "spec.workloadClusters[0].workerPools[0].network")


def test_not_enough_hosts(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["workloadClusters"][0]["workerPools"][0]["replicas"] = 5
    _, report = site.validate(doc)
    f = errors(report, "spec.workloadClusters[0].workerPools[0].hostSelector")[0]
    assert "3 host(s) match" in f["found"]


def test_no_spare_host_is_a_warning(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["workloadClusters"][0]["workerPools"][0]["replicas"] = 3
    code, report = site.validate(doc)
    assert code == 0
    assert warnings(report, "spec.workloadClusters[0].workerPools[0].hostSelector")


def test_duplicate_boot_mac(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["hosts"][1]["bootMACAddress"] = "B8:CA:3A:01:02:01"
    _, report = site.validate(doc)
    assert errors(report, "spec.baremetal.hosts[1].bootMACAddress")


def test_ironic_ip_must_be_in_mgmt_pool(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["ironicIP"] = "10.20.110.60"
    _, report = site.validate(doc)
    assert errors(report, "spec.baremetal.ironicIP")


def test_s3_target_needs_credentials(site):
    doc = load_example("vsphere")
    del doc["spec"]["storage"]["longhorn"]["backupCredentials"]
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.longhorn.backupCredentials")


def test_nfs_target_rejects_credentials(site):
    doc = load_example("proxmox")
    doc["spec"]["storage"]["longhorn"]["backupCredentials"] = {"file": "./secrets/longhorn-s3.env"}
    _, report = site.validate(doc)
    assert "remove it" in errors(report, "spec.storage.longhorn.backupCredentials")[0]["fix"]


def test_missing_secret_file(site):
    doc = load_example("vsphere")
    doc["spec"]["platform"]["vsphere"]["credentials"]["password"] = {"file": "./secrets/nope.pw"}
    _, report = site.validate(doc)
    assert "no such file" in errors(report, "spec.platform.vsphere.credentials.password.file")[0]["found"]


def test_unset_env_secret(site, monkeypatch):
    monkeypatch.delenv("DCIAB_PVE_TOKEN")
    _, report = site.validate(load_example("proxmox"))
    assert errors(report, "spec.platform.proxmox.credentials.tokenSecret.env")


def test_world_readable_secret_is_a_warning(site):
    (site.dir / "secrets" / "vsphere.pw").chmod(0o644)
    code, report = site.validate(load_example("vsphere"))
    assert code == 0
    assert warnings(report, "spec.platform.vsphere.credentials.password.file")


def test_bundle_version_mismatch(site):
    doc = load_example("vsphere")
    doc["spec"]["bundleVersion"] = "9.9.9"
    _, report = site.validate(doc)
    assert errors(report, "spec.bundleVersion")


def test_reserved_cluster_name(site):
    doc = load_example("vsphere")
    doc["spec"]["workloadClusters"][0]["name"] = "infra"
    _, report = site.validate(doc)
    assert errors(report, "spec.workloadClusters[0].name")


# --- the entrypoint --------------------------------------------------------------------

def _run(*args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run([str(ROOT / "dciab"), *args], capture_output=True, text=True, env=env)


def test_entrypoint_validate_ok(site):
    path = site.dir / "dciab.yaml"
    path.write_text((ROOT / "examples" / "vsphere.yaml").read_text())
    r = _run("validate", str(path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "The config is valid." in r.stdout


def test_entrypoint_requires_a_config():
    r = _run("validate")
    assert r.returncode == 2
    assert "give the config file" in r.stderr


def test_entrypoint_unimplemented_step():
    r = _run("infra")
    assert r.returncode == 2
    assert "not implemented yet" in r.stderr


# --- bundle.lock ---------------------------------------------------------------------

def test_kubernetes_version_must_match_bundle_lock(site):
    import yaml
    pinned = yaml.safe_load((ROOT / "bundle.lock").read_text())["kubernetes"]["version"]
    major, minor, patch = pinned.lstrip("v").split(".")
    doc = load_example("vsphere")
    doc["spec"]["kubernetes"]["version"] = f"v{major}.{int(minor) - 1}.{patch}"
    _, report = site.validate(doc)
    assert f"use {pinned}" in errors(report, "spec.kubernetes.version")[0]["fix"]


def test_examples_use_the_bundle_kubernetes_version():
    import yaml
    pinned = yaml.safe_load((ROOT / "bundle.lock").read_text())["kubernetes"]["version"]
    for name in ("vsphere", "proxmox", "metal3-cluster"):
        assert load_example(name)["spec"]["kubernetes"]["version"] == pinned, name


def test_proxmox_template_node_must_be_listed(site):
    doc = load_example("proxmox")
    doc["spec"]["platform"]["proxmox"]["templateNode"] = "pve09"
    _, report = site.validate(doc)
    assert errors(report, "spec.platform.proxmox.templateNode")


# --- Day 0: hypervisors and bootstrap NTP -------------------------------------------

def test_hypervisor_type_must_match_platform(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["type"] = "esxi"
    doc["spec"]["hypervisors"]["installerFile"] = "./isos/esxi.iso"
    _, report = site.validate(doc)
    assert "use type: proxmox" in errors(report, "spec.hypervisors.type")[0]["fix"]


def test_esxi_needs_installer_file(site):
    doc = load_example("vsphere")
    doc["spec"]["hypervisors"] = load_example("proxmox-bare")["spec"]["hypervisors"]
    doc["spec"]["hypervisors"]["type"] = "esxi"
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.installerFile")


def test_proxmox_rejects_installer_file(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["installerFile"] = "./isos/pve.iso"
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors")


def test_only_virtualmedia_for_now(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["method"] = "pxe"
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.method")


def test_hypervisor_address_must_not_overlap(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["hosts"][1]["address"] = "10.30.110.25"  # inside mgmt nodeIPRange
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.hosts[1].address")


def test_hypervisor_address_inside_node_network(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["hosts"][0]["address"] = "10.99.0.6"
    _, report = site.validate(doc)
    assert "outside" in errors(report, "spec.hypervisors.hosts[0].address")[0]["found"]


def test_hypervisor_duplicate_mac_and_bmc(site):
    doc = load_example("proxmox-bare")
    hosts = doc["spec"]["hypervisors"]["hosts"]
    hosts[2]["bootMACAddress"] = hosts[0]["bootMACAddress"].upper()
    hosts[2]["bmc"]["address"] = hosts[0]["bmc"]["address"]
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.hosts[2].bootMACAddress")
    assert errors(report, "spec.hypervisors.hosts[2].bmc.address")


def test_hypervisor_not_in_proxmox_nodes_is_a_warning(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["hosts"][2]["name"] = "pve09"
    code, report = site.validate(doc)
    assert code == 0
    assert warnings(report, "spec.hypervisors.hosts[2].name")
    assert any("pve03" in n for n in report["notes"])


def test_hypervisor_root_password_must_exist(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["rootPassword"] = {"file": "./secrets/hv/missing"}
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.rootPassword.file")


def test_bootstrap_ntp_needs_service_ips_as_ntp_servers(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["network"]["ntpServers"] = ["10.30.0.123"]
    _, report = site.validate(doc)
    assert "10.30.110.53, 10.30.110.54" in errors(report, "spec.network.ntpServers")[0]["fix"]


def test_service_ips_as_ntp_servers_need_bootstrap_ntp(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["bootstrap"]["ntp"]
    _, report = site.validate(doc)
    assert "spec.bootstrap.ntp" in errors(report, "spec.network.ntpServers")[0]["fix"]


def test_no_upstream_requires_serving_local_clock(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["ntp"]["serveLocalClock"] = False
    _, report = site.validate(doc)
    assert errors(report, "spec.bootstrap.ntp.serveLocalClock")


def test_no_upstream_is_noted(site):
    _, report = site.validate(load_example("proxmox-bare"))
    assert any("bootstrap host's clock" in n for n in report["notes"])


# --- ESXi installer pin (needs its own archive root with a pinned bundle.lock) ----------

def _esxi_site(site, tmp_path, iso_bytes: bytes, pin_bytes):
    import hashlib
    import shutil

    import yaml

    root = tmp_path / "archive"
    (root / "schema").mkdir(parents=True)
    shutil.copy(ROOT / "VERSION", root / "VERSION")
    shutil.copy(ROOT / "schema" / "dciab.schema.json", root / "schema" / "dciab.schema.json")
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    if pin_bytes is not None:
        lock["hypervisors"]["esxi"].update(
            version="test", isoFile="esxi-test.iso", isoSHA256=hashlib.sha256(pin_bytes).hexdigest())
    (root / "bundle.lock").write_text(yaml.safe_dump(lock))
    (site.dir / "isos").mkdir()
    (site.dir / "isos" / "esxi.iso").write_bytes(iso_bytes)

    doc = load_example("vsphere")
    doc["spec"]["hypervisors"] = load_example("proxmox-bare")["spec"]["hypervisors"]
    doc["spec"]["hypervisors"].update(type="esxi", installerFile="./isos/esxi.iso")
    for i, h in enumerate(doc["spec"]["hypervisors"]["hosts"]):
        h["address"] = f"10.20.110.{6 + i}"
    return root, doc


def _validate_with_root(site, root, doc):
    import json

    from dciab_validate import cli

    path = site.dir / "dciab.yaml"
    path.write_text(__import__("yaml").safe_dump(doc, sort_keys=False))
    code = cli.main([str(path), "--root", str(root), "--output", "json"])
    return code, json.loads(site.capsys.readouterr().out)


def test_esxi_installer_matching_pin(site, tmp_path):
    root, doc = _esxi_site(site, tmp_path, b"esxi", b"esxi")
    code, report = _validate_with_root(site, root, doc)
    assert errors(report) == [], errors(report)
    assert any("vCenter" in n for n in report["notes"])


def test_esxi_installer_wrong_build(site, tmp_path):
    root, doc = _esxi_site(site, tmp_path, b"other", b"esxi")
    _, report = _validate_with_root(site, root, doc)
    assert "esxi-test.iso" in errors(report, "spec.hypervisors.installerFile")[0]["fix"]


def test_esxi_installer_without_a_pin(site, tmp_path):
    root, doc = _esxi_site(site, tmp_path, b"esxi", None)
    _, report = _validate_with_root(site, root, doc)
    assert "no tested ESXi build" in errors(report, "spec.hypervisors.installerFile")[0]["fix"]


def test_bmc_hostnames_compare_case_insensitively(site):
    doc = load_example("proxmox-bare")
    hosts = doc["spec"]["hypervisors"]["hosts"]
    hosts[0]["bmc"]["address"] = "redfish-virtualmedia+https://idrac-a.example.net/redfish/v1/Systems/1"
    hosts[1]["bmc"]["address"] = "redfish-virtualmedia+https://IDRAC-A.example.net/redfish/v1/Systems/1"
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.hosts[1].bmc.address")


def test_bootstrap_ntp_needs_hypervisors(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["hypervisors"]
    _, report = site.validate(doc)
    assert "spec.hypervisors" in errors(report, "spec.bootstrap.ntp")[0]["fix"]


def test_bootstrap_ntp_upstream_must_not_be_a_service_ip(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["ntp"]["upstream"] = ["10.30.110.53"]
    _, report = site.validate(doc)
    assert errors(report, "spec.bootstrap.ntp.upstream[0]")


def test_hypervisors_need_a_boot_mode(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["hypervisors"]["bootMode"]
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.bootMode")


def test_workdisk_disk_or_absolute_path(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"]["workDisk"] = {"path": "/srv/dciab"}
    code, _ = site.validate(doc)
    assert code == 0
    doc["spec"]["bootstrap"]["workDisk"] = {"path": "relative/dir"}
    _, report = site.validate(doc)
    assert errors(report, "spec.bootstrap.workDisk")
    doc["spec"]["bootstrap"]["workDisk"] = {"disk": {}}
    _, report = site.validate(doc)
    assert errors(report, "spec.bootstrap.workDisk")


def test_missing_workdisk_is_noted(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["bootstrap"]["workDisk"]
    _, report = site.validate(doc)
    assert any("workDisk" in n for n in report["notes"])


# --- the imported intermediate CA ------------------------------------------------------

def _pki_site(site, **cert):
    from conftest import make_cert
    make_cert(site.dir / "secrets" / "pki" / "intermediate.crt", **cert)
    return site.validate(load_example("proxmox"))


def test_intermediate_without_path_length_limit_is_valid(site):
    code, report = _pki_site(site)
    assert code == 0 and not errors(report, "spec.pki.certificate") and not warnings(report, "spec.pki.certificate")


def test_intermediate_with_path_length_3_is_valid(site):
    _, report = _pki_site(site, pathlen=3)
    assert not errors(report, "spec.pki.certificate")


def test_intermediate_with_short_path_length_is_an_error(site):
    code, report = _pki_site(site, pathlen=2)
    assert code == 1
    [f] = errors(report, "spec.pki.certificate")
    assert "path length is 2" in f["found"] and "generate-root" in f["fix"]


def test_leaf_certificate_is_not_an_intermediate(site):
    _, report = _pki_site(site, ca=False)
    [f] = errors(report, "spec.pki.certificate")
    assert "not a CA" in f["found"]


def test_intermediate_expiring_within_a_year_is_a_warning(site):
    code, report = _pki_site(site, days=90)
    assert code == 0
    [f] = warnings(report, "spec.pki.certificate")
    assert "within a year" in f["found"]


def test_unreadable_certificate_is_an_error(site):
    (site.dir / "secrets" / "pki" / "intermediate.crt").write_text("not-a-certificate\n")
    _, report = site.validate(load_example("proxmox"))
    [f] = errors(report, "spec.pki.certificate")
    assert "not a PEM certificate" in f["found"]


def test_intermediate_from_env(site, monkeypatch):
    from conftest import make_cert
    pem = make_cert(site.dir / "env.crt", pathlen=0).read_text()
    monkeypatch.setenv("DCIAB_TEST_INTERMEDIATE", pem)
    doc = load_example("proxmox")
    doc["spec"]["pki"]["certificate"] = {"env": "DCIAB_TEST_INTERMEDIATE"}
    _, report = site.validate(doc)
    assert "path length is 0" in errors(report, "spec.pki.certificate")[0]["found"]


def test_without_openssl_the_path_length_is_noted(site, monkeypatch):
    import dciab_validate.checks as checks
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    code, report = site.validate(load_example("proxmox"))
    assert code == 0 and any("openssl is not installed" in n for n in report["notes"])


def test_generate_root_does_not_read_a_certificate(site):
    (site.dir / "secrets" / "pki" / "intermediate.crt").write_text("not-a-certificate\n")
    _, report = site.validate(load_example("vsphere"))
    assert not errors(report, "spec.pki.certificate")
