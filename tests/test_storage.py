"""Block storage (ADR 0008, decisions 4 and 5): the provider per tier, the four StorageClasses,
Longhorn's data disk, and what validate says about them."""

import copy
import os
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, errors, load_example, warnings

sys.path.insert(0, str(ROOT / "lib"))

import dciab_storage as st  # noqa: E402
from dciab_cluster.values import cluster_values, unsupported  # noqa: E402

from test_cluster_values import CA  # noqa: E402

CLASSES_SH = ROOT / "lib" / "steps" / "storage-classes.sh"


def by_name(classes):
    return {c["metadata"]["name"]: c for c in classes}


def legacy_doc(name="proxmox"):
    doc = load_example(name)
    doc["spec"]["storage"] = {"longhorn": doc["spec"]["storage"]["block"]["longhorn"]}
    return doc


def proxmox_csi(doc):
    doc["spec"]["storage"]["block"] = {
        "provider": "proxmox-csi",
        "proxmoxCSI": {"storage": "ceph-vm",
                       "credentials": {"tokenID": "dciab@pve!csi", "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}},
    }
    return doc


# --- the provider per tier --------------------------------------------------------------

def test_old_form_reads_as_longhorn():
    doc = legacy_doc()
    assert st.legacy(doc["spec"])
    b = st.tier_block(doc["spec"], "mgmt")
    assert b == {"provider": "longhorn", "settings": doc["spec"]["storage"]["longhorn"]}


def test_override_replaces_the_provider_for_its_tier_only():
    spec = load_example("vsphere")["spec"]
    lh = spec["storage"]["block"]["longhorn"]
    spec["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"storagePolicy": "gold"},
                                "overrides": {"workload": {"provider": "longhorn", "longhorn": lh}}}
    assert st.tier_block(spec, "mgmt") == {"provider": "vsphere-csi", "settings": {"storagePolicy": "gold"}}
    assert st.tier_block(spec, "workload")["provider"] == "longhorn"
    assert st.cluster_block(spec, "apps01")["settings"]["replicaCount"] == 3
    assert st.cluster_block(spec, "infra")["provider"] == "vsphere-csi"


def test_override_without_a_provider_merges_settings():
    spec = load_example("vsphere")["spec"]
    spec["storage"]["block"]["overrides"] = {"mgmt": {"longhorn": {"replicaCount": 2}}}
    m = st.tier_block(spec, "mgmt")["settings"]
    assert m["replicaCount"] == 2 and m["backupTarget"].startswith("s3://")
    assert st.tier_block(spec, "infra")["settings"]["replicaCount"] == 3
    # The base is never changed by a merge.
    assert spec["storage"]["block"]["longhorn"]["replicaCount"] == 3


def test_unknown_cluster_and_tier():
    spec = load_example("vsphere")["spec"]
    with pytest.raises(st.StorageError):
        st.tier_of(spec, "nope")
    with pytest.raises(st.StorageError):
        st.tier_block(spec, "apps01")


# --- the four classes --------------------------------------------------------------------

def test_every_provider_makes_the_four_classes_with_one_default():
    vs = load_example("vsphere")["spec"]
    vs["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"storagePolicy": "gold"}}
    source = {"kind": "StorageClass", "provisioner": "csi.example.com", "parameters": {"tier": "1"},
              "allowVolumeExpansion": True, "volumeBindingMode": "WaitForFirstConsumer", "mountOptions": ["noatime"]}
    for classes in (st.storage_classes(load_example("proxmox")["spec"], "mgmt"),
                    st.storage_classes(vs, "mgmt"),
                    st.storage_classes(proxmox_csi(load_example("proxmox"))["spec"], "infra"),
                    st.copied_classes(source)):
        names = [c["metadata"]["name"] for c in classes]
        assert names == ["replicated", "replicated-retain", "local", "local-retain"]
        defaults = [c["metadata"]["name"] for c in classes
                    if c["metadata"]["annotations"][st.DEFAULT_ANNOTATION] == "true"]
        assert defaults == ["replicated"]
        for c in classes:
            want = "Retain" if c["metadata"]["name"].endswith("-retain") else "Delete"
            assert c["reclaimPolicy"] == want
            assert c["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "dciab"}


def test_longhorn_local_classes_keep_one_replica_on_the_node():
    spec = load_example("vsphere")["spec"]
    c = by_name(st.storage_classes(spec, "mgmt"))
    assert c["replicated"]["parameters"]["numberOfReplicas"] == "3"
    assert c["replicated"]["parameters"]["dataLocality"] == "disabled"
    for name in ("local", "local-retain"):
        assert c[name]["parameters"]["numberOfReplicas"] == "1"
        assert c[name]["parameters"]["dataLocality"] == "strict-local"
        assert c[name]["volumeBindingMode"] == "WaitForFirstConsumer"
    assert {x["provisioner"] for x in c.values()} == {"driver.longhorn.io"}


def test_array_classes_use_the_array_four_times():
    spec = proxmox_csi(load_example("proxmox"))["spec"]
    c = by_name(st.storage_classes(spec, "mgmt"))
    assert {x["provisioner"] for x in c.values()} == {"csi.proxmox.sinextra.dev"}
    assert all(x["parameters"]["storage"] == "ceph-vm" for x in c.values())
    vs = load_example("vsphere")["spec"]
    vs["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"datastoreURL": "ds:///vmfs/volumes/x/"}}
    c = by_name(st.storage_classes(vs, "mgmt"))
    assert c["local"]["parameters"] == {"csi.storage.k8s.io/fstype": "ext4", "datastoreurl": "ds:///vmfs/volumes/x/"}
    vs["storage"]["block"] = {"provider": "vsphere-csi"}
    assert "storagepolicyname" not in by_name(st.storage_classes(vs, "mgmt"))["replicated"]["parameters"]


def test_existing_copies_the_source_and_needs_it():
    spec = load_example("vsphere")["spec"]
    spec["storage"]["block"] = {"provider": "existing", "existing": {"storageClass": "site-gold"}}
    with pytest.raises(st.StorageError, match="read StorageClass site-gold"):
        st.storage_classes(spec, "mgmt")
    source = {"kind": "StorageClass", "provisioner": "csi.example.com", "parameters": {"a": "b"},
              "allowedTopologies": [{"matchLabelExpressions": []}]}
    c = by_name(st.storage_classes(spec, "mgmt", source))
    assert c["local-retain"]["provisioner"] == "csi.example.com"
    assert c["local-retain"]["parameters"] == {"a": "b"}
    assert c["replicated"]["volumeBindingMode"] == "Immediate"
    assert "allowedTopologies" in c["replicated"]
    with pytest.raises(st.StorageError):
        st.copied_classes({"kind": "ConfigMap"})


@pytest.mark.parametrize("provider,key,settings", [
    ("ceph-csi", "cephCSI", {"clusterID": "x", "monitors": ["m"], "pool": "p", "userID": "k", "key": {"env": "K"}}),
    ("vendor-csi", "vendorCSI", {"driver": "trident"}),
])
def test_providers_not_installed_yet_make_no_classes(provider, key, settings):
    spec = load_example("vsphere")["spec"]
    spec["storage"]["block"] = {"provider": provider, key: settings}
    with pytest.raises(st.StorageError, match="not installed by DCiaB yet"):
        st.storage_classes(spec, "mgmt")


def test_cli_prints_provider_and_classes(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "lib")}
    cfg = ROOT / "examples" / "vsphere.yaml"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "provider", str(cfg), "apps01"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "longhorn"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "classes", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert [d["metadata"]["name"] for d in yaml.safe_load_all(r.stdout)][0] == "replicated"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "existing", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "not existing" in r.stderr


# --- storage-classes.sh -----------------------------------------------------------------

def fake_kubectl(tmp_path, immutable=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "kubectl.log"
    body = f'#!/bin/sh\necho "kubectl $*" >> {log}\n'
    if immutable:
        # The first apply of replicated fails as the API server does for a changed parameter.
        body += (f'case "$*" in *"apply --server-side --field-manager=dciab --force-conflicts"*)\n'
                 f'  if grep -q "name: replicated$" "$6" && [ ! -e {tmp_path}/once ]; then touch {tmp_path}/once; '
                 'echo "StorageClass.storage.k8s.io \\"replicated\\" is invalid: parameters: Forbidden: updates to parameters are forbidden." >&2; exit 1; fi ;;\nesac\n')
    body += ('case "$*" in\n'
             '  *"get storageclass -o jsonpath"*) printf "longhorn true\\nreplicated true\\n" ;;\n'
             '  *"get storageclass site-gold -o json"*) echo \'{"kind": "StorageClass", "provisioner": "csi.example.com"}\' ;;\n'
             "esac\nexit 0\n")
    (bin_dir / "kubectl").write_text(body)
    (bin_dir / "kubectl").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, log


def test_storage_classes_applies_server_side_and_leaves_one_default(tmp_path):
    env, log = fake_kubectl(tmp_path)
    kc = tmp_path / "kc"
    kc.write_text("x")
    r = subprocess.run([str(CLASSES_SH), str(ROOT / "examples" / "vsphere.yaml"), "mgmt", str(kc)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    applies = [c for c in calls if "apply" in c]
    assert len(applies) == 4 and all("--server-side --field-manager=dciab" in c for c in applies)
    assert "kubectl annotate storageclass longhorn storageclass.kubernetes.io/is-default-class=false --overwrite" in calls
    assert not any("annotate storageclass replicated" in c for c in calls)


def test_storage_classes_recreates_a_class_whose_parameters_changed(tmp_path):
    env, log = fake_kubectl(tmp_path, immutable=True)
    kc = tmp_path / "kc"
    kc.write_text("x")
    r = subprocess.run([str(CLASSES_SH), str(ROOT / "examples" / "vsphere.yaml"), "mgmt", str(kc)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "creating it again" in r.stdout
    assert "kubectl delete storageclass replicated --wait=true" in log.read_text()


def test_storage_classes_copies_an_existing_class(tmp_path, site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"] = {"provider": "existing", "existing": {"storageClass": "site-gold"}}
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc))
    env, log = fake_kubectl(tmp_path)
    kc = tmp_path / "kc"
    kc.write_text("x")
    r = subprocess.run([str(CLASSES_SH), str(cfg), "mgmt", str(kc)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "kubectl get storageclass site-gold -o json" in log.read_text()


def test_mgmt_storage_step_dispatches_on_the_provider():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    step = text.split("step storage", 1)[1]
    assert "python3 -m dciab_storage provider" in step
    assert 'addons/longhorn/install.sh" "$config" "$mgmt_kubeconfig"' in step
    assert 'lib/steps/storage-classes.sh" "$config" mgmt "$mgmt_kubeconfig"' in step
    assert "isn't installed by DCiaB yet" in step


def test_longhorn_install_applies_its_secret_server_side_and_the_classes():
    text = (ROOT / "addons" / "longhorn" / "install.sh").read_text()
    pipe = text.split('longhorn_values.py" secret', 1)[1].split("\n    else", 1)[0]
    assert "kubectl apply --server-side" in pipe
    assert "last-applied-configuration-" in pipe   # removes the copy an older install left
    assert "lib/steps/storage-classes.sh" in text
    # The only client-side apply left is the namespace's, which holds no secret.
    assert [ln for ln in text.splitlines() if "kubectl apply -f -" in ln] == [
        '    kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -']


# --- the data disk ----------------------------------------------------------------------

def test_vsphere_longhorn_machines_get_their_data_disk():
    doc = load_example("vsphere")
    mgmt = cluster_values(doc, "mgmt", CA)
    assert mgmt["controlPlane"]["machine"]["dataDiskGiB"] == 200
    infra = cluster_values(doc, "infra", CA)
    assert "dataDiskGiB" not in infra["controlPlane"]["machine"]
    assert {p["machine"]["dataDiskGiB"] for p in infra["workerPools"]} == {500}
    assert unsupported(doc, "infra") == []


def test_data_disk_is_dropped_without_longhorn_or_on_proxmox():
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"] = {"provider": "vsphere-csi"}
    v = cluster_values(doc, "mgmt", CA)
    assert "dataDiskGiB" not in v["controlPlane"]["machine"]
    assert "isn't Longhorn" in unsupported(doc, "mgmt")[0]
    px = load_example("proxmox")
    px["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"] = 100
    v = cluster_values(px, "infra", CA)
    assert all("dataDiskGiB" not in p["machine"] for p in v["workerPools"])
    assert "CAPMOX" in unsupported(px, "infra")[0]


def _cc(provider):
    docs = [d for d in yaml.safe_load_all((ROOT / "charts" / "capi-clusterclasses" / "classes" / "r1"
                                            / f"{provider}.yaml").read_text()) if d]
    return next(d for d in docs if d["kind"] == "ClusterClass")


def test_vsphere_class_adds_and_mounts_the_data_disk():
    cc = _cc("vsphere")
    machine = next(v for v in cc["spec"]["variables"] if v["name"] == "machine")
    assert "dataDiskGiB" in machine["schema"]["openAPIV3Schema"]["properties"]
    assert "dataDiskGiB" not in machine["schema"]["openAPIV3Schema"]["required"]
    patches = {p["name"]: p for p in cc["spec"]["patches"]}
    for d in patches["vsphereMachines"]["definitions"]:
        op = next(o for o in d["jsonPatches"] if o["path"] == "/spec/template/spec/additionalDisksGiB")
        assert ".machine.dataDiskGiB" in op["valueFrom"]["template"]
    disk = patches["dataDisk"]
    assert "enabledIf" not in disk   # per machine: a pool's machine variable decides
    assert len(disk["definitions"]) == 2
    for d in disk["definitions"]:
        script = next(o for o in d["jsonPatches"] if o["path"].endswith("/files/-"))["value"]
        assert script["path"] == "/etc/dciab/data-disk.sh"
        body = script["content"]
        assert "/var/lib/longhorn" in body and "mkfs.xfs" in body and "/etc/fstab" in body
        assert "exit 1" in body   # no disk: the bootstrap fails instead of using the root disk
        cmd = next(o for o in d["jsonPatches"] if o["path"].endswith("/preKubeadmCommands/-"))
        assert cmd["valueFrom"]["template"] == "/etc/dciab/data-disk.sh {{ .machine.dataDiskGiB | default 0 }}"


@pytest.mark.parametrize("size,disks,expect", [
    (0, [], 0),
    (200, [("sda", "disk", 100 << 30, "xfs"), ("sdb", "disk", 200 << 30, "")], "/dev/sdb"),
    (200, [("sda", "disk", 100 << 30, "xfs")], 1),
    (200, [("sdb", "disk", 200 << 30, "xfs")], 1),   # the right size, but not blank
])
def test_data_disk_script_picks_the_blank_disk_of_its_size(tmp_path, size, disks, expect):
    cc = _cc("vsphere")
    disk = next(p for p in cc["spec"]["patches"] if p["name"] == "dataDisk")
    body = next(o for o in disk["definitions"][0]["jsonPatches"] if o["path"].endswith("/files/-"))["value"]["content"]
    root = tmp_path / "root"
    body = body.replace("/etc/fstab", str(tmp_path / "fstab")).replace("/var/lib/longhorn", str(root))
    script = tmp_path / "data-disk.sh"
    script.write_text(body)
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listing = "\\n".join(f"/dev/{n} {t} {s}" for n, t, s, _ in disks)
    fs = {f"/dev/{n}": f for n, _, _, f in disks}
    tools = {
        "blkid": "#!/bin/sh\nexit 2\n",
        "lsblk": ('#!/bin/sh\ncase "$*" in\n'
                  f'  -dbnpo*) printf "{listing}\\n" ;;\n'
                  + "".join(f'  *{d}) echo "{f}" ;;\n' for d, f in fs.items()) +
                  "esac\n"),
        "mkfs.xfs": f'#!/bin/sh\necho "$*" > {tmp_path}/mkfs\n',
        "mountpoint": "#!/bin/sh\nexit 1\n",
        "mount": f'#!/bin/sh\necho "$*" > {tmp_path}/mount\n',
    }
    for name, text in tools.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run([str(script), str(size)], capture_output=True, text=True, env=env)
    if isinstance(expect, int):
        assert r.returncode == expect, r.stderr
        assert not (tmp_path / "mkfs").exists()
    else:
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "mkfs").read_text().split() == ["-q", "-L", "dciab-data", expect]
        assert f"LABEL=dciab-data {root} xfs" in (tmp_path / "fstab").read_text()
        assert (tmp_path / "mount").read_text().strip() == str(root)


# --- validate ---------------------------------------------------------------------------

def test_old_form_still_validates_with_a_warning(site):
    _, report = site.validate(legacy_doc("vsphere"))
    assert errors(report) == []
    w = warnings(report, "spec.storage.longhorn")
    assert w and "block" in w[0]["fix"] and "provider: longhorn" in w[0]["fix"]


def test_both_forms_at_once_is_an_error(site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["longhorn"] = doc["spec"]["storage"]["block"]["longhorn"]
    _, report = site.validate(doc)
    assert errors(report, "spec.storage")


def test_settings_of_another_provider_are_rejected(site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"]["vsphereCSI"] = {}
    _, report = site.validate(doc)
    assert any("keep only" in e["fix"] for e in errors(report))
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"mgmt": {"proxmoxCSI": {
        "storage": "x", "credentials": {"tokenID": "a@pve!b", "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}}}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.mgmt.proxmoxCSI")


def test_override_to_a_provider_without_its_settings(site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"infra": {"provider": "existing"}}
    _, report = site.validate(doc)
    assert "add existing" in errors(report, "spec.storage.block.existing")[0]["fix"]


def test_overrides_are_keyed_by_tier_only(site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"apps01": {"provider": "longhorn"}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.apps01")


@pytest.mark.parametrize("provider,key,settings", [
    ("ceph-csi", "cephCSI", {"clusterID": "x", "monitors": ["m:6789"], "pool": "rbd", "userID": "k8s",
                             "key": {"env": "DCIAB_PVE_TOKEN"}}),
    ("vendor-csi", "vendorCSI", {"driver": "trident"}),
])
def test_providers_not_installed_yet(site, provider, key, settings):
    doc = load_example("vsphere")
    lh = doc["spec"]["storage"]["block"]["longhorn"]
    doc["spec"]["storage"]["block"] = {"provider": "longhorn", "longhorn": lh,
                                       "overrides": {"infra": {"provider": provider, key: settings}}}
    _, report = site.validate(doc)
    w = warnings(report, "spec.storage.block.overrides.infra.provider")
    assert w and "not yet installed by DCiaB" in w[0]["found"]
    doc["spec"]["storage"]["block"] = {"provider": provider, key: settings}
    _, report = site.validate(doc)
    assert "not yet installed by DCiaB" in errors(report, "spec.storage.block.provider")[0]["found"]


def test_array_provider_only_on_its_platform(site):
    doc = proxmox_csi(load_example("vsphere"))
    _, report = site.validate(doc)
    assert any("works only on proxmox clusters" in e["fix"] for e in errors(report, "spec.storage.block.provider"))


def test_proxmox_csi_needs_its_own_token(site):
    doc = proxmox_csi(load_example("proxmox"))
    doc["spec"]["storage"]["block"]["proxmoxCSI"]["credentials"]["tokenID"] = "dciab@pve!capmox"
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.proxmoxCSI.credentials")


def test_longhorn_on_vsphere_needs_data_disks(site):
    doc = load_example("vsphere")
    del doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"]
    del doc["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"]
    _, report = site.validate(doc)
    assert errors(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")
    assert errors(report, "spec.clusters.infra.workerPools[0].dataDiskGiB")


def test_data_disk_rules(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"] = 100   # = diskGiB
    _, report = site.validate(doc)
    assert "another size" in errors(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")[0]["fix"]
    doc = load_example("proxmox")
    doc["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"] = 200
    _, report = site.validate(doc)
    assert "CAPMOX" in errors(report, "spec.clusters.infra.workerPools[0].dataDiskGiB")[0]["fix"]


def test_longhorn_on_proxmox_warns_about_the_root_disk(site):
    _, report = site.validate(load_example("proxmox"))
    assert "root disk" in warnings(report, "spec.clusters.mgmt")[0]["found"]


def test_more_replicas_than_longhorn_nodes(site):
    doc = load_example("vsphere")
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["replicas"] = 1
    _, report = site.validate(doc)
    found = [w["found"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount")]
    assert any("spec.clusters.mgmt has 1 node(s)" in f for f in found)


def test_double_replication_hint_skips_local_proxmox_storage(site):
    doc = load_example("proxmox")
    doc["spec"]["storage"]["block"]["longhorn"]["replicaCount"] = 2
    _, report = site.validate(doc)
    assert any("ceph-vm" in w["found"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount"))
    doc["spec"]["platform"]["proxmox"]["storage"] = "local-lvm"
    _, report = site.validate(doc)
    assert not any("local-lvm" in w["found"] for w in warnings(report))


def test_existing_needs_a_class_name(site):
    doc = load_example("vsphere")
    doc["spec"]["storage"]["block"] = {"provider": "existing", "existing": {}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.existing.storageClass")
    doc["spec"]["storage"]["block"]["existing"] = {"storageClass": "site-gold"}
    for c in (doc["spec"]["clusters"]["mgmt"]["controlPlane"], doc["spec"]["clusters"]["infra"]["workerPools"][0],
              doc["spec"]["workloadClusters"][0]["workerPools"][0]):
        c.pop("dataDiskGiB", None)
    _, report = site.validate(copy.deepcopy(doc))
    assert errors(report) == []


def test_old_form_on_vsphere_without_data_disks_only_warns(site):
    doc = legacy_doc("vsphere")
    for c in (doc["spec"]["clusters"]["mgmt"]["controlPlane"], doc["spec"]["clusters"]["infra"]["workerPools"][0],
              doc["spec"]["workloadClusters"][0]["workerPools"][0]):
        c.pop("dataDiskGiB", None)
    code, report = site.validate(doc)
    assert errors(report) == [] and code == 0
    assert warnings(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")


def test_one_replica_on_vms_warns_that_a_replaced_node_takes_it(site):
    doc = load_example("proxmox")
    doc["spec"]["storage"]["block"]["longhorn"]["replicaCount"] = 1
    _, report = site.validate(doc)
    fix = next(w["fix"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount") if w["found"] == "1")
    assert "deleted when a node is replaced" in fix


def test_until_longhorn_still_means_storage():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    assert 'if [[ "$until" == longhorn ]]; then' in text and "until=storage" in text
    assert text.index("until=storage") < text.index("--until must be one of")
