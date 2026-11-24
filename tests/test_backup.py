"""spec.backup (ADR 0009): targets, tiers, schedules, Longhorn's recurring jobs, and the
validate checks that enforce the tier rule."""

import json
import os
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, errors, load_example, longhorn_example, warnings

import dciab_backup as bk  # noqa: E402
import dciab_storage as st  # noqa: E402

OTHER = "age1" + "p" * 58


# --- the model ---------------------------------------------------------------------------

def test_defaults_follow_adr_0009():
    spec = load_example("vsphere")["spec"]
    s = bk.schedules(spec)
    assert s["etcd"]["mgmt"] == {"cron": "0 * * * *", "keep": {"hourly": 24, "daily": 14}}
    assert s["etcd"]["workloads"] == {"cron": "0 */6 * * *", "keep": {"days": 7}}
    assert s["capiExport"]["keep"] == {"daily": 14}
    assert s["longhorn"]["backup"]["retain"] == 14
    assert bk.tier_target(spec, "mgmt") == "site"
    assert bk.tier_target(spec, "infra") == "objectStore"
    t = bk.tiers(spec)["workloads"]
    assert t["etcdSnapshots"] is True and t["volumeBackups"] is False
    assert bk.offsite(spec)["retentionDays"] == 90


def test_schedules_and_retention_are_configurable():
    spec = load_example("vsphere")["spec"]
    spec["backup"]["schedules"] = {"etcd": {"mgmt": {"keep": {"daily": 30}}},
                                   "longhorn": {"backup": {"retain": 30}}}
    s = bk.schedules(spec)
    assert s["etcd"]["mgmt"] == {"cron": "0 * * * *", "keep": {"hourly": 24, "daily": 30}}
    assert s["longhorn"]["backup"] == {"cron": "0 1 * * *", "retain": 30}
    assert s["etcd"]["infra"]["keep"] == {"hourly": 24, "daily": 14}   # untouched
    # The defaults are never changed by a merge.
    assert bk.DEFAULT_SCHEDULES["etcd"]["mgmt"]["keep"]["daily"] == 14


def test_removable_defaults_to_every_seven_days():
    spec = load_example("proxmox")["spec"]
    spec["backup"]["targets"]["offsite"] = {"mode": "removable"}
    assert bk.offsite(spec)["everyDays"] == 7


def test_old_form_reads_mgmts_longhorn_target():
    spec = longhorn_example("proxmox")["spec"]
    del spec["backup"]
    spec["storage"]["block"]["longhorn"]["backupTarget"] = "nfs://nas:/lh"
    assert bk.legacy(spec)
    assert bk.site_target(spec) == {"url": "nfs://nas:/lh"}
    assert bk.offsite(spec) is None
    assert bk.longhorn_target(spec, "mgmt") == {"url": "nfs://nas:/lh"}
    # An array provider has no Longhorn target to fall back on.
    spec = load_example("proxmox")["spec"]
    del spec["backup"]
    assert bk.site_target(spec) is None


def test_mgmts_longhorn_always_uses_the_site_target():
    spec = longhorn_example("vsphere")["spec"]
    spec["storage"]["block"]["longhorn"]["backupTarget"] = "s3://other@s3.example.net"
    assert bk.longhorn_target(spec, "mgmt")["url"] == spec["backup"]["targets"]["site"]["url"]
    # INFRA on the object provider, which isn't built: its own target, else an error.
    assert bk.longhorn_target(spec, "infra")["url"] == "s3://other@s3.example.net"
    del spec["storage"]["block"]["longhorn"]["backupTarget"]
    with pytest.raises(bk.BackupError, match="object provider"):
        bk.longhorn_target(spec, "infra")
    spec["backup"]["tiers"] = {"infra": {"target": "site"}}
    assert bk.longhorn_target(spec, "infra")["url"] == spec["backup"]["targets"]["site"]["url"]


# --- Longhorn ----------------------------------------------------------------------------

def test_longhorn_classes_name_their_groups():
    classes = {c["metadata"]["name"]: c["parameters"] for c in st.storage_classes(longhorn_example("vsphere")["spec"], "mgmt")}
    assert "recurringJobSelector" not in classes["replicated"]   # Longhorn's default group
    groups = {n: json.loads(p["recurringJobSelector"]) for n, p in classes.items() if n != "replicated"}
    assert groups == {"replicated-retain": [{"name": "platform", "isGroup": True}],
                      "local-retain": [{"name": "local", "isGroup": True}],
                      "local": [{"name": "scratch", "isGroup": True}]}


def test_array_classes_have_no_selector():
    for c in st.storage_classes(load_example("vsphere")["spec"], "mgmt"):
        assert "recurringJobSelector" not in c["parameters"]


def jobs(spec, cluster="mgmt"):
    return {j["spec"]["task"]: j for j in bk.longhorn_jobs(spec, cluster)}


def test_mgmt_jobs():
    spec = longhorn_example("vsphere")["spec"]
    j = jobs(spec)
    assert set(j) == {"snapshot", "backup"}
    snap, backup = j["snapshot"], j["backup"]
    assert snap["apiVersion"] == "longhorn.io/v1beta2" and snap["metadata"]["namespace"] == "longhorn-system"
    assert snap["spec"]["cron"] == "0 * * * *" and snap["spec"]["retain"] == 24
    assert sorted(snap["spec"]["groups"]) == ["default", "local", "platform"]
    # local-retain: snapshots only; the services back themselves up. scratch: nothing.
    assert sorted(backup["spec"]["groups"]) == ["default", "platform"]
    assert backup["spec"]["cron"] == "0 1 * * *" and backup["spec"]["retain"] == 14
    assert all("scratch" not in x["spec"]["groups"] for x in j.values())


def test_workload_volume_backups_are_opt_in():
    spec = load_example("metal3-cluster")["spec"]
    j = jobs(spec, "metal01")
    assert "default" not in j["backup"]["spec"]["groups"]
    assert "default" not in j["snapshot"]["spec"]["groups"]
    assert bk.TENANT_GROUP in j["backup"]["spec"]["groups"]
    spec["backup"]["tiers"]["workloads"]["volumeBackups"] = True
    assert "default" in jobs(spec, "metal01")["backup"]["spec"]["groups"]


def test_array_providers_get_no_longhorn_jobs():
    with pytest.raises(bk.BackupError, match="decision 6"):
        bk.longhorn_jobs(load_example("vsphere")["spec"], "mgmt")


def test_cli_prints_the_jobs(tmp_path):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(longhorn_example("proxmox")))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "lib")}
    r = subprocess.run([sys.executable, "-m", "dciab_backup", "longhorn-jobs", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert [d["kind"] for d in yaml.safe_load_all(r.stdout)] == ["RecurringJob", "RecurringJob"]
    r = subprocess.run([sys.executable, "-m", "dciab_backup", "target", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "nfs://nas01.dc3.example.net:/exports/dciab-backup"
    cfg.write_text(yaml.safe_dump(load_example("proxmox")))
    r = subprocess.run([sys.executable, "-m", "dciab_backup", "longhorn-jobs", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "isn't longhorn" in r.stderr


def test_longhorn_install_applies_the_jobs_server_side():
    text = (ROOT / "addons" / "longhorn" / "install.sh").read_text()
    apply = text.split("python3 -m dciab_backup longhorn-jobs", 1)[1].split("\n", 2)[1]
    assert "kubectl apply --server-side --field-manager=dciab" in apply
    assert text.index("storage-classes.sh") < text.index("dciab_backup longhorn-jobs")


# --- validate ----------------------------------------------------------------------------

def test_mgmt_must_back_up_to_the_site_target(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["tiers"] = {"mgmt": {"target": "objectStore"}}
    _, report = site.validate(doc)
    assert "never to INFRA" in errors(report, "spec.backup.tiers.mgmt.target")[0]["fix"]


def test_offsite_none_needs_a_reason(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["targets"]["offsite"] = {"mode": "none"}
    code, _ = site.validate(doc)
    assert code != 0
    doc["spec"]["backup"]["targets"]["offsite"] = {"mode": "none", "reason": "single-room site"}
    doc["spec"]["backup"]["tiers"] = {"infra": {"target": "site"}}
    _, report = site.validate(doc)
    assert not errors(report)
    assert warnings(report, "spec.backup.targets.offsite.mode")


def test_infra_on_its_own_object_store_needs_an_offsite_copy(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["targets"]["offsite"] = {"mode": "none", "reason": "single-room site"}
    _, report = site.validate(doc)
    assert "tiers.infra.target: site" in errors(report, "spec.backup.tiers")[0]["fix"]


@pytest.mark.parametrize("offsite", [
    {"mode": "copy"},                                                    # no url
    {"mode": "removable", "url": "s3://x@y"},
    {"mode": "site-replicated", "reason": "x" * 5},
    {"mode": "none", "reason": "no second room", "retentionDays": 30},
    {"mode": "copy", "url": "s3://a@b", "everyDays": 3},
])
def test_offsite_modes_take_only_their_fields(site, offsite):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["targets"]["offsite"] = offsite
    code, report = site.validate(doc)
    assert code != 0 and errors(report)


def test_offsite_copy_checks(site):
    doc = load_example("vsphere")
    off = doc["spec"]["backup"]["targets"]["offsite"]
    off["url"] = doc["spec"]["backup"]["targets"]["site"]["url"]
    _, report = site.validate(doc)
    assert errors(report, "spec.backup.targets.offsite.url")
    doc = load_example("vsphere")
    doc["spec"]["backup"]["targets"]["offsite"] = {"mode": "copy", "url": "nfs://dr:/x", "retentionDays": 30}
    _, report = site.validate(doc)
    assert "write-once" in warnings(report, "spec.backup.targets.offsite.url")[0]["fix"]
    assert warnings(report, "spec.backup.targets.offsite.retentionDays")
    doc["spec"]["backup"]["targets"]["offsite"]["credentials"] = {"file": "./secrets/backup-offsite.env"}
    _, report = site.validate(doc)
    assert errors(report, "spec.backup.targets.offsite.credentials")


def test_backup_key_must_not_be_a_recipient(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["key"] = {"publicKey": doc["spec"]["bootstrap"]["recipients"][0]}
    _, report = site.validate(doc)
    assert errors(report, "spec.backup.key.publicKey")
    doc["spec"]["backup"]["key"] = {"publicKey": OTHER}
    _, report = site.validate(doc)
    assert not errors(report)


def test_ca_bundle_only_for_s3(site):
    doc = load_example("proxmox")
    doc["spec"]["backup"]["targets"]["site"]["caBundle"] = {"file": "./secrets/backup-site.env"}
    _, report = site.validate(doc)
    assert errors(report, "spec.backup.targets.site.caBundle")


def test_no_backup_and_no_longhorn_target_is_an_error(site):
    doc = load_example("vsphere")
    del doc["spec"]["backup"]
    _, report = site.validate(doc)
    assert "targets.site" in errors(report, "spec.backup")[0]["fix"]


def test_old_form_warns_with_the_new_form(site):
    doc = longhorn_example("proxmox")
    del doc["spec"]["backup"]
    doc["spec"]["storage"]["block"]["longhorn"]["backupTarget"] = "nfs://nas:/lh"
    _, report = site.validate(doc)
    w = warnings(report, "spec.backup")
    assert w and "nfs://nas:/lh" in w[0]["fix"]
    assert not errors(report, "spec.backup")


def test_old_longhorn_target_beside_spec_backup(site):
    doc = longhorn_example("proxmox")
    lh = doc["spec"]["storage"]["block"]["longhorn"]
    lh["backupTarget"] = doc["spec"]["backup"]["targets"]["site"]["url"]
    _, report = site.validate(doc)
    assert warnings(report, "spec.storage.block.longhorn.backupTarget")
    lh["backupTarget"] = "nfs://elsewhere:/lh"
    _, report = site.validate(doc)
    assert "always backs up" in errors(report, "spec.storage.block.longhorn.backupTarget")[0]["fix"]


def test_workload_etcd_snapshots_off_warns(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["tiers"] = {"workloads": {"etcdSnapshots": False}}
    _, report = site.validate(doc)
    assert warnings(report, "spec.backup.tiers.workloads.etcdSnapshots")
    assert any("workload clusters get etcd snapshots (off)" in n for n in report["notes"])


def test_bad_cron_is_rejected(site):
    doc = load_example("vsphere")
    doc["spec"]["backup"]["schedules"] = {"etcd": {"mgmt": {"cron": "hourly"}}}
    code, _ = site.validate(doc)
    assert code != 0
