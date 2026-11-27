import pytest
import os
import subprocess

from conftest import ROOT, errors, load_example, longhorn_example, warnings


# --- the shipped examples ---------------------------------------------------------

# Warnings an example is meant to show on every run (ADR 0010): a site that isn't highly
# available says so, and so does a Proxmox control plane that r1 can't spread.
# proxmox-bare runs Longhorn on INFRA, for its backups, on VM root disks (CAPMOX gives no
# data disk), which warns. MGMT keeps no volumes (ADR 0008, decision 4a), so it doesn't.
# metal3-cluster runs Longhorn on INFRA VMs on a vSAN datastore, which replicates twice: the
# example keeps it to show Longhorn on VMs with data disks.
EXPECTED_WARNINGS = {
    "metal3-cluster": {"spec.storage.block.longhorn.replicaCount"},
    "proxmox": {"spec.failureDomains.domains"},
    "proxmox-bare": {"spec.failureDomains.notHighlyAvailable", "spec.time.serveLocalClock",
                     "spec.clusters.infra"},
}


def test_examples_are_valid(site, example):
    name, doc = example
    code, report = site.validate(doc)
    assert errors(report) == [], f"{name}: {errors(report)}"
    assert {w["path"] for w in warnings(report)} == EXPECTED_WARNINGS.get(name, set()), f"{name}: {warnings(report)}"
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
    del doc["spec"]["time"]["servers"]
    code, report = site.validate(doc)
    assert code == 1
    assert errors(report, "spec.time.servers")[0]["found"] == "missing"


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
    doc = longhorn_example("vsphere")
    del doc["spec"]["backup"]["targets"]["site"]["credentials"]
    _, report = site.validate(doc)
    assert errors(report, "spec.backup.targets.site.credentials")


def test_nfs_target_rejects_credentials(site):
    doc = longhorn_example("proxmox")
    doc["spec"]["backup"]["targets"]["site"]["credentials"] = {"file": "./secrets/longhorn-s3.env"}
    _, report = site.validate(doc)
    assert "remove it" in errors(report, "spec.backup.targets.site.credentials")[0]["fix"]


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
    doc["spec"]["bundleVersion"] = "9.0.0"   # a valid release form, so the archive comparison runs
    _, report = site.validate(doc)
    assert any("the version of this archive" in f["fix"] for f in errors(report, "spec.bundleVersion"))


def test_bundle_version_outside_the_release_forms(site):
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


def tagged_example():
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["managementVLAN"] = "tagged"
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = doc["spec"]["network"]["nodes"]["vlan"]
    return doc


def test_tagged_hypervisor_management_vlan_is_accepted(site):
    doc = tagged_example()
    _, report = site.validate(doc)
    assert not errors(report, "spec.hypervisors.managementVLAN")
    assert not errors(report, "spec.network.nodes.vlan") and not errors(report, "spec.platform.proxmox.bridge")
    assert not errors(report, "spec.platform.proxmox.vlanTag")


@pytest.mark.parametrize("tag", [None, 999])
def test_tagged_hypervisor_management_vlan_needs_node_vms_tagged_on_the_node_vlan(site, tag):
    doc = tagged_example()
    if tag is None:
        del doc["spec"]["platform"]["proxmox"]["vlanTag"]
    else:
        doc["spec"]["platform"]["proxmox"]["vlanTag"] = tag
    _, report = site.validate(doc)
    assert "set vlanTag: 110" in errors(report, "spec.platform.proxmox.vlanTag")[0]["fix"]


def test_tagged_hypervisor_management_vlan_refuses_vlan_1(site):
    doc = tagged_example()
    doc["spec"]["network"]["nodes"]["vlan"] = 1
    _, report = site.validate(doc)
    fix = errors(report, "spec.network.nodes.vlan")[0]["fix"]
    assert "not supported: VLAN 1 is the bridge's default PVID; use managementVLAN: untagged" in fix


def test_tagged_hypervisor_management_vlan_needs_vmbr0(site):
    doc = tagged_example()
    doc["spec"]["platform"]["proxmox"]["bridge"] = "vmbr1"
    _, report = site.validate(doc)
    assert "use vmbr0" in errors(report, "spec.platform.proxmox.bridge")[0]["fix"]


def test_tagged_hypervisor_management_vlan_without_a_vlan_is_refused(site):
    doc = tagged_example()
    del doc["spec"]["network"]["nodes"]["vlan"]
    code, _ = site.validate(doc)
    assert code == 1                    # the schema requires network.nodes.vlan (1-4094)


@pytest.mark.parametrize("vids", ["2-4094", "110", "100-120 300", " 110  200-210 "])
def test_bridge_vids_accepted(site, vids):
    doc = tagged_example()
    doc["spec"]["hypervisors"]["bridgeVids"] = vids
    _, report = site.validate(doc)
    assert not errors(report, "spec.hypervisors.bridgeVids")


@pytest.mark.parametrize("vids", ["1-4094", "2-4095", "120-110", "0", "110,120", ""])
def test_bridge_vids_malformed_or_out_of_range_is_refused(site, vids):
    doc = tagged_example()
    doc["spec"]["hypervisors"]["bridgeVids"] = vids
    code, report = site.validate(doc)
    assert code == 1
    assert errors(report, "spec.hypervisors.bridgeVids") or errors(report, "spec.hypervisors")


def test_tagged_bridge_vids_must_carry_the_node_vlan(site):
    doc = tagged_example()
    doc["spec"]["hypervisors"]["bridgeVids"] = "200-210"
    _, report = site.validate(doc)
    found = errors(report, "spec.hypervisors.bridgeVids")
    assert any("add 110" in f["fix"] for f in found)


def test_untagged_bridge_vids_must_carry_the_vm_vlan_tag(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = 150
    doc["spec"]["hypervisors"]["bridgeVids"] = "200-210"
    _, report = site.validate(doc)
    assert any("add 150" in f["fix"] for f in errors(report, "spec.hypervisors.bridgeVids"))
    doc["spec"]["hypervisors"]["bridgeVids"] = "150 200-210"
    _, report = site.validate(doc)
    assert not errors(report, "spec.hypervisors.bridgeVids")


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


def test_unclustered_hypervisors_list_only_the_template_node(site):
    # ADR 0010: the installed hosts aren't clustered, so nodes: [pve01] is right and the
    # other hosts get a note: notHighlyAvailable already warns about the single host.
    doc = load_example("proxmox-bare")
    assert doc["spec"]["platform"]["proxmox"]["nodes"] == ["pve01"]
    code, report = site.validate(doc)
    assert code == 0
    assert not errors(report, "spec.platform.proxmox.nodes")
    assert not warnings(report, "spec.hypervisors.hosts")
    assert any(n.startswith("pve02, pve03 are installed but not in") for n in report["notes"])
    assert not any("won't install" in n for n in report["notes"])


def test_unclustered_hosts_in_nodes_on_local_storage_is_an_error(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["platform"]["proxmox"]["nodes"] = ["pve01", "pve02", "pve03"]
    code, report = site.validate(doc)
    assert code != 0
    [e] = errors(report, "spec.platform.proxmox.nodes")
    assert "[pve01]" in e["fix"] and "pve02, pve03" in e["fix"]
    assert not any("installed but not in" in n for n in report["notes"])


def test_more_nodes_on_shared_storage_are_accepted(site):
    doc = load_example("proxmox-bare")
    px = doc["spec"]["platform"]["proxmox"]
    px["storage"], px["nodes"] = "ceph-rbd", ["pve01", "pve02", "pve09"]
    _, report = site.validate(doc)
    assert not errors(report, "spec.platform.proxmox.nodes")
    assert any(n.startswith("pve03 is installed but not in") for n in report["notes"])
    assert any("pve09" in n and "won't install" in n for n in report["notes"])


def test_hypervisor_root_password_must_exist_when_given(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["hypervisors"]["rootPassword"] = {"file": "./secrets/hv/missing"}
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.rootPassword.file")


def test_proxmox_hypervisors_need_installer_locale(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["hypervisors"]["installer"]
    _, report = site.validate(doc)
    assert errors(report, "spec.hypervisors.installer")


def test_time_served_by_dciab_uses_the_service_ips(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["time"]["servers"] = ["10.30.0.123"]
    _, report = site.validate(doc)
    assert "10.30.110.53, 10.30.110.54" in errors(report, "spec.time.servers")[0]["fix"]
    doc["spec"]["time"]["servers"] = ["10.30.110.54", "10.30.110.53"]
    _, report = site.validate(doc)
    assert not errors(report, "spec.time.servers")


def test_site_time_servers_must_not_be_service_ips(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["time"] = {"servedBy": "site", "servers": ["10.30.110.53"]}
    _, report = site.validate(doc)
    assert "servedBy: dciab" in errors(report, "spec.time.servers")[0]["fix"]


def test_no_upstream_requires_serving_local_clock(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["time"]["serveLocalClock"] = False
    _, report = site.validate(doc)
    assert errors(report, "spec.time.serveLocalClock")


def test_local_clock_only_without_upstream(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["time"]["upstream"] = ["ntp.example.org"]
    _, report = site.validate(doc)
    assert errors(report, "spec.time.upstream")


def test_site_time_takes_no_upstream(site):
    doc = load_example("proxmox")
    doc["spec"]["time"]["upstream"] = ["ntp.example.org"]
    _, report = site.validate(doc)
    assert errors(report, "spec.time")


def test_local_clock_is_a_warning(site):
    _, report = site.validate(load_example("proxmox-bare"))
    assert "one time source" in warnings(report, "spec.time.serveLocalClock")[0]["found"]


def test_old_ntp_fields_are_gone(site):
    doc = load_example("proxmox")
    doc["spec"]["network"]["ntpServers"] = ["10.30.0.123"]
    _, report = site.validate(doc)
    assert errors(report, "spec.network.ntpServers")


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


def test_time_served_by_dciab_needs_hypervisors(site):
    doc = load_example("proxmox-bare")
    del doc["spec"]["hypervisors"]
    _, report = site.validate(doc)
    assert "spec.hypervisors" in errors(report, "spec.time.servedBy")[0]["fix"]


def test_time_upstream_must_not_be_a_service_ip(site):
    doc = load_example("proxmox-bare")
    doc["spec"]["time"]["upstream"] = ["10.30.110.53"]
    doc["spec"]["time"]["serveLocalClock"] = False
    _, report = site.validate(doc)
    assert errors(report, "spec.time.upstream[0]")


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


def test_intermediate_without_cert_sign_usage_is_an_error(site):
    _, report = _pki_site(site, key_usage="critical,digitalSignature")
    [f] = errors(report, "spec.pki.certificate")
    assert "keyCertSign" in f["found"]


def test_intermediate_with_cert_sign_usage_is_valid(site):
    _, report = _pki_site(site, key_usage="critical,keyCertSign,cRLSign")
    assert not errors(report, "spec.pki.certificate")


def test_not_yet_valid_intermediate_is_an_error(site, monkeypatch):
    import datetime as dt
    import dciab_validate.checks as checks

    class Past(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2000, 1, 1, tzinfo=tz)
    monkeypatch.setattr(checks, "datetime", Past)
    _, report = _pki_site(site)
    assert "not valid until" in errors(report, "spec.pki.certificate")[0]["found"]


def test_openssl_timeout_is_noted(site, monkeypatch):
    import subprocess as sp
    import dciab_validate.checks as checks
    real = sp.run

    def slow(cmd, *a, **kw):
        if cmd[0] == "openssl":
            raise sp.TimeoutExpired(cmd, 30)
        return real(cmd, *a, **kw)
    from conftest import make_cert
    make_cert(site.dir / "secrets" / "pki" / "intermediate.crt")
    monkeypatch.setattr(checks.subprocess, "run", slow)
    code, report = site.validate(load_example("proxmox"))
    assert code == 0 and any("openssl timed out" in n for n in report["notes"])


# --- failure domains (ADR 0010) --------------------------------------------------------

def test_failure_domains_are_required(site):
    doc = load_example("proxmox")
    del doc["spec"]["failureDomains"]
    _, report = site.validate(doc)
    assert errors(report, "spec.failureDomains")[0]["found"] == "missing"


def test_fewer_than_three_domains_need_a_reason(site):
    doc = load_example("proxmox")
    doc["spec"]["failureDomains"]["domains"] = doc["spec"]["failureDomains"]["domains"][:2]
    _, report = site.validate(doc)
    assert "notHighlyAvailable" in errors(report, "spec.failureDomains.notHighlyAvailable")[0]["fix"]
    doc["spec"]["failureDomains"]["notHighlyAvailable"] = {"reason": "two hosts at a branch office"}
    _, report = site.validate(doc)
    assert not errors(report)
    assert "two failure domains" in warnings(report, "spec.failureDomains.notHighlyAvailable")[0]["fix"]


def test_single_domain_warning_names_the_host(site):
    _, report = site.validate(load_example("proxmox-bare"))
    assert "losing pve01 stops MGMT, INFRA" in warnings(report, "spec.failureDomains.notHighlyAvailable")[0]["fix"]


def test_proxmox_domains_name_their_node(site):
    doc = load_example("proxmox")
    d = doc["spec"]["failureDomains"]["domains"]
    d[0]["name"] = "rack1"
    d[1]["proxmox"]["node"] = "pve09"
    d[2]["proxmox"]["node"] = "pve01"
    _, report = site.validate(doc)
    assert "after its PVE node" in errors(report, "spec.failureDomains.domains[0].name")[0]["fix"]
    assert errors(report, "spec.failureDomains.domains[1].proxmox.node")
    assert "one domain" in errors(report, "spec.failureDomains.domains[2].proxmox.node")[0]["fix"]


def test_domains_match_the_platform(site):
    doc = load_example("proxmox")
    doc["spec"]["failureDomains"]["domains"][0]["vsphere"] = {"computeCluster": "c1"}
    _, report = site.validate(doc)
    assert errors(report, "spec.failureDomains.domains[0]")


def test_proxmox_control_plane_is_reported_unspread(site):
    _, report = site.validate(load_example("proxmox"))
    assert "control plane isn't in r1" in warnings(report, "spec.failureDomains.domains")[0]["fix"]


def test_references_name_declared_domains(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["hosts"][0]["failureDomain"] = "rack-z"
    doc["spec"]["workloadClusters"][0]["workerPools"][0]["failureDomains"] = ["rack-z"]
    _, report = site.validate(doc)
    assert errors(report, "spec.baremetal.hosts[0].failureDomain")
    assert errors(report, "spec.workloadClusters[0].workerPools[0].failureDomains[0]")


def test_vsphere_cluster_can_not_use_a_metal_domain(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["failureDomains"]["domains"].append({"name": "rack-d"})   # bare metal only
    doc["spec"]["clusters"]["infra"]["failureDomains"] = ["rack-d"]
    _, report = site.validate(doc)
    assert "vsphere cluster" in errors(report, "spec.clusters.infra.failureDomains[0]")[0]["fix"]


def test_generated_pool_names_stay_short(site):
    doc = load_example("proxmox")
    doc["spec"]["clusters"]["infra"]["workerPools"][0]["name"] = "a-very-long-pool-nam"
    for d in doc["spec"]["failureDomains"]["domains"]:
        d["name"] = d["proxmox"]["node"] = d["name"] + "-node-long"
    doc["spec"]["platform"]["proxmox"]["nodes"] = [d["name"] for d in doc["spec"]["failureDomains"]["domains"]]
    doc["spec"]["platform"]["proxmox"]["templateNode"] = doc["spec"]["platform"]["proxmox"]["nodes"][0]
    _, report = site.validate(doc)
    assert "<pool>-<domain>" in errors(report, "spec.clusters.infra.workerPools[0].name")[0]["fix"]


def test_narrowed_cluster_warns_about_quorum(site):
    doc = load_example("proxmox")
    doc["spec"]["clusters"]["infra"]["failureDomains"] = ["pve01", "pve02"]
    _, report = site.validate(doc)
    assert "3 control plane nodes over 2" in warnings(report, "spec.clusters.infra.controlPlane.replicas")[0]["found"]


def test_metal3_domains_need_enough_hosts(site):
    doc = load_example("metal3-cluster")
    doc["spec"]["baremetal"]["hosts"][4]["failureDomain"] = "rack-a"     # r760-05 leaves rack-b
    _, report = site.validate(doc)
    assert "0 matching host(s) in rack-b" in warnings(report, "spec.workloadClusters[0].workerPools[0].hostSelector")[0]["found"]


def test_cluster_modules_off_is_a_warning(site):
    doc = load_example("vsphere")
    doc["spec"]["platform"]["vsphere"]["clusterModules"] = False
    _, report = site.validate(doc)
    assert warnings(report, "spec.platform.vsphere.clusterModules")


# --- storage networks (ADR 0010, decision 8a) -------------------------------------------

def test_storage_networks_and_ranges(site):
    doc = load_example("proxmox")
    infra = doc["spec"]["clusters"]["infra"]
    infra["storageIPRanges"]["san-c"] = "10.40.30.10-10.40.30.20"
    infra["storageIPRanges"]["san-b"] = "10.40.10.30-10.40.10.49"      # in san-a's network
    doc["spec"]["clusters"]["mgmt"]["storageIPRanges"] = {"san-a": "10.40.10.40-10.40.10.45"}
    _, report = site.validate(doc)
    assert errors(report, "spec.clusters.infra.storageIPRanges.san-c")
    assert "outside spec.network.storage san-b" in errors(report, "spec.clusters.infra.storageIPRanges.san-b")[0]["found"]
    assert "overlaps spec.clusters.mgmt.storageIPRanges.san-a" in errors(
        report, "spec.clusters.infra.storageIPRanges.san-a")[0]["found"]


def test_storage_networks_per_platform(site):
    doc = load_example("vsphere")
    doc["spec"]["network"]["storage"] = [{"name": "san-a", "cidr": "10.40.10.0/24", "vlan": 410}]
    _, report = site.validate(doc)
    assert errors(report, "spec.network.storage[0].portGroup")
    assert errors(report, "spec.network.storage[0].vlan")
    doc = load_example("proxmox")
    doc["spec"]["network"]["storage"][0]["cidr"] = "10.30.110.0/24"
    _, report = site.validate(doc)
    assert errors(report, "spec.network.storage[0].cidr")                  # overlaps the node network


def test_unused_storage_network_is_noted(site):
    doc = load_example("proxmox")
    del doc["spec"]["clusters"]["infra"]["storageIPRanges"]
    _, report = site.validate(doc)
    assert any("no cluster has storageIPRanges" in n for n in report["notes"])


def test_only_domains_mgmt_and_infra_can_use_count(site):
    """A Proxmox node and two Metal3 racks are not three domains for a Proxmox control plane."""
    doc = load_example("proxmox")
    doc["spec"]["failureDomains"] = {"domains": [{"name": "pve01", "proxmox": {"node": "pve01"}},
                                                 {"name": "rack-a"}, {"name": "rack-b"}]}
    _, report = site.validate(doc)
    assert "1 failure domain(s) MGMT and INFRA can use" in errors(report, "spec.failureDomains.notHighlyAvailable")[0]["found"]


def test_generated_names_never_collide(site):
    doc = load_example("proxmox")
    nodes = ["c", "b-c", "pve03"]
    doc["spec"]["platform"]["proxmox"].update(nodes=nodes, templateNode="c")
    doc["spec"]["failureDomains"]["domains"] = [{"name": n, "proxmox": {"node": n}} for n in nodes]
    pool = doc["spec"]["clusters"]["infra"]["workerPools"][0]
    doc["spec"]["clusters"]["infra"]["workerPools"] = [dict(pool, name="ab", failureDomains=["b-c", "pve03"]),
                                                       dict(pool, name="ab-b", failureDomains=["c", "pve03"])]
    _, report = site.validate(doc)
    assert any("also a MachineDeployment" in e["found"] for e in errors(report))


# --- upstream DNS -------------------------------------------------------------------

def test_empty_upstream_resolvers_without_isolated_is_a_warning(site):
    doc = load_example("proxmox")
    del doc["spec"]["network"]["isolated"]
    code, report = site.validate(doc)
    assert code == 0
    [w] = warnings(report, "spec.network.upstreamResolvers")
    assert "isolated" in w["fix"] and "kubeadm init" in w["fix"]


def test_isolated_site_with_no_resolvers_is_quiet(site):
    _, report = site.validate(load_example("proxmox"))
    assert warnings(report, "spec.network.upstreamResolvers") == []


def test_isolated_with_resolvers_is_a_warning(site):
    doc = load_example("vsphere")
    doc["spec"]["network"]["isolated"] = True
    code, report = site.validate(doc)
    assert code == 0
    assert warnings(report, "spec.network.isolated")


# --- a bare site's Proxmox VE API before dciab hypervisors (#276) --------------------------------

def _record(site, **fields):
    import json
    d = site.dir / ".dciab" / "state" / "installs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pve01.json").write_text(json.dumps(fields))


def test_bare_site_token_and_fingerprint_are_pending_before_install(site):
    # examples/proxmox-bare.yaml: the URL names pve01, no tlsFingerprint, and no token file yet.
    code, report = site.validate(load_example("proxmox-bare"))
    assert code == 0 and errors(report) == []
    notes = report["notes"]
    assert any("pending: the CAPMOX token" in n and "./secrets/pve/token" in n and "pve01" in n for n in notes)
    assert any("pending: the Proxmox API's certificate on pve01" in n for n in notes)
    # Still pending once installed, until post is done.
    _record(site, outcome="installed", iso="http://x/pve01.iso")
    code, report = site.validate(load_example("proxmox-bare"))
    assert code == 0 and any("pending: the CAPMOX token" in n for n in report["notes"])


def test_bare_site_after_post_uses_the_pin_and_needs_the_token(site):
    _record(site, outcome="installed", iso="http://x/pve01.iso", post="done", api_fingerprint=":".join(["AB"] * 32))
    code, report = site.validate(load_example("proxmox-bare"))
    [e] = errors(report)
    assert e["path"] == "spec.platform.proxmox.credentials.tokenSecret.file"
    assert "pve01.token.age" in e["fix"] and "age" in e["fix"]
    assert any("trusted by the certificate dciab hypervisors pinned" in n and "AB:AB" in n for n in report["notes"])
    token = site.dir / "secrets" / "pve" / "token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("s3cret\n")
    token.chmod(0o600)
    code, report = site.validate(load_example("proxmox-bare"))
    assert code == 0 and not any("pending" in n for n in report["notes"])


def test_bare_site_env_token_is_pending_until_set(site, monkeypatch):
    doc = load_example("proxmox-bare")
    doc["spec"]["platform"]["proxmox"]["credentials"]["tokenSecret"] = {"env": "DCIAB_BARE_TOKEN"}
    monkeypatch.delenv("DCIAB_BARE_TOKEN", raising=False)
    code, report = site.validate(doc)
    assert code == 0 and any("environment variable DCIAB_BARE_TOKEN" in n and "pending" in n for n in report["notes"])


def test_fingerprint_is_required_for_a_proxmox_dciab_doesnt_install(site):
    doc = load_example("proxmox")
    del doc["spec"]["platform"]["proxmox"]["tlsFingerprint"]
    code, report = site.validate(doc)
    [e] = errors(report)
    assert code == 1 and e["path"] == "spec.platform.proxmox.tlsFingerprint" and "spec.hypervisors" in e["fix"]
    # A URL that names none of spec.hypervisors' hosts: the token is required as before.
    doc = load_example("proxmox-bare")
    doc["spec"]["platform"]["proxmox"]["url"] = "https://pve-api.dc3.example.com:8006"
    code, report = site.validate(doc)
    assert {e["path"] for e in errors(report)} == {"spec.platform.proxmox.tlsFingerprint",
                                                   "spec.platform.proxmox.credentials.tokenSecret.file"}


def test_an_empty_secret_file_is_an_error(site):
    (site.dir / "secrets" / "bmc" / "pve02").write_text("")
    code, report = site.validate(load_example("proxmox-bare"))
    [e] = errors(report)
    assert code == 1 and e["path"] == "spec.hypervisors.hosts[1].bmc.credentials.password.file"
    assert e["found"] == "./secrets/bmc/pve02 is empty" and e["fix"] == "write the secret into the file"


def test_validate_state_option_finds_the_install_records(site, tmp_path):
    import json
    state = tmp_path / "elsewhere"
    (state / "installs").mkdir(parents=True)
    (state / "installs" / "pve01.json").write_text(json.dumps({"outcome": "installed", "post": "done",
                                                               "api_fingerprint": ":".join(["CD"] * 32)}))
    from dciab_validate import cli
    path = site.dir / "dciab.yaml"
    import yaml
    path.write_text(yaml.safe_dump(load_example("proxmox-bare"), sort_keys=False))
    assert cli.main([str(path), "--root", str(ROOT), "--output", "json", "--state", str(state)]) == 1
    report = json.loads(site.capsys.readouterr().out)
    assert [e["path"] for e in errors(report)] == ["spec.platform.proxmox.credentials.tokenSecret.file"]
