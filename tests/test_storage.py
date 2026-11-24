"""Block storage (ADR 0008, decisions 4, 4a and 5): the provider per tier, the four
StorageClasses, Longhorn's data disk, and what validate says about them."""

import copy
import os
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, errors, load_example, longhorn_example, mgmt_longhorn, warnings

sys.path.insert(0, str(ROOT / "lib"))

import dciab_storage as st  # noqa: E402
from dciab_cluster.values import cluster_values, unsupported  # noqa: E402

from test_cluster_values import CA  # noqa: E402

CLASSES_SH = ROOT / "lib" / "steps" / "storage-classes.sh"


def by_name(classes):
    return {c["metadata"]["name"]: c for c in classes}


def legacy_doc(name="proxmox"):
    doc = longhorn_example(name)
    doc["spec"]["storage"] = {"longhorn": doc["spec"]["storage"]["block"]["longhorn"]}
    return doc


def proxmox_csi(doc):
    doc["spec"]["storage"]["block"] = {
        "provider": "proxmox-csi",
        "proxmoxCSI": {"storage": "ceph-vm", "shared": True,
                       "credentials": {"tokenID": "csi@pve!dciab", "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}},
    }
    return doc


# --- the provider per tier --------------------------------------------------------------

def test_old_form_reads_as_longhorn_for_infra_only():
    """The old form describes INFRA now: MGMT keeps no volumes (ADR 0008, decision 4a)."""
    doc = legacy_doc()
    assert st.legacy(doc["spec"])
    b = st.tier_block(doc["spec"], "infra")
    assert b == {"provider": "longhorn", "settings": doc["spec"]["storage"]["longhorn"]}
    assert st.tier_block(doc["spec"], "mgmt") is None


def test_mgmt_keeps_no_volumes_by_default():
    for name in ("vsphere", "proxmox", "proxmox-bare", "metal3-cluster"):
        spec = load_example(name)["spec"]
        assert st.tier_block(spec, "mgmt") is None, name
        assert st.cluster_block(spec, "mgmt") is None, name
        assert st.tier_block(spec, "infra") is not None, name
        with pytest.raises(st.StorageError, match="overrides.mgmt"):
            st.storage_classes(spec, "mgmt")


def test_override_replaces_the_provider_for_its_tier_only():
    spec = longhorn_example("vsphere")["spec"]
    lh = spec["storage"]["block"]["longhorn"]
    spec["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"storagePolicy": "gold"},
                                "overrides": {"workload": {"provider": "longhorn", "longhorn": lh}}}
    assert st.tier_block(spec, "infra") == {"provider": "vsphere-csi", "settings": {"storagePolicy": "gold"}}
    assert st.tier_block(spec, "mgmt") is None
    assert st.tier_block(spec, "workload")["provider"] == "longhorn"
    assert st.cluster_block(spec, "apps01")["settings"]["replicaCount"] == 3


def test_infra_override_without_a_provider_merges_settings():
    spec = longhorn_example("vsphere")["spec"]
    spec["storage"]["block"]["overrides"] = {"infra": {"longhorn": {"replicaCount": 2}}}
    m = st.tier_block(spec, "infra")["settings"]
    assert m["replicaCount"] == 2
    spec["storage"]["block"]["longhorn"]["backupTarget"] = "s3://lh@s3.example.net"
    assert st.tier_block(spec, "infra")["settings"]["backupTarget"].startswith("s3://")
    # The base is never changed by a merge.
    assert spec["storage"]["block"]["longhorn"]["replicaCount"] == 3


def test_mgmt_override_stands_alone():
    """overrides.mgmt is written in full, as overrides.workload: nothing comes from block."""
    spec = longhorn_example("vsphere")["spec"]
    spec["storage"]["block"]["longhorn"]["backupTarget"] = "s3://lh@s3.example.net"
    spec["storage"]["block"]["overrides"] = {"mgmt": {"provider": "longhorn", "longhorn": {"replicaCount": 2}}}
    assert st.tier_block(spec, "mgmt") == {"provider": "longhorn", "settings": {"replicaCount": 2}}
    assert st.tier_block(spec, "infra")["settings"]["replicaCount"] == 3
    spec["storage"]["block"]["overrides"] = {"mgmt": {"provider": "vsphere-csi"}}
    assert st.tier_block(spec, "mgmt") == {"provider": "vsphere-csi", "settings": {}}
    assert [c["metadata"]["name"] for c in st.storage_classes(spec, "mgmt")][0] == "replicated"


def test_workload_clusters_dont_inherit_block():
    """MGMT and INFRA's block is theirs: without overrides.workload, DCiaB gives workload
    clusters no block storage, and tenants bring their own."""
    doc = longhorn_example("vsphere")
    spec = doc["spec"]
    assert st.tier_block(spec, "workload") is None
    assert st.cluster_block(spec, "apps01") is None
    assert st.cluster_block(spec, "infra")["provider"] == "longhorn"
    with pytest.raises(st.StorageError, match="overrides.workload"):
        st.storage_classes(spec, "apps01")
    assert not cluster_values(doc, "apps01", CA)["workerPools"][0]["machine"].get("dataDiskGiB")


def test_workload_override_stands_alone():
    """The workload override is never merged over block: only its own settings count."""
    spec = load_example("metal3-cluster")["spec"]
    w = st.cluster_block(spec, "metal01")
    assert w["provider"] == "longhorn" and w["settings"]["replicaCount"] == 2
    spec["storage"]["block"]["longhorn"]["backupTarget"] = "s3://lh@s3.example.net/dc2"
    assert "backupTarget" not in st.cluster_block(spec, "metal01")["settings"]
    assert st.tier_block(spec, "infra")["settings"]["replicaCount"] == 3


def test_unknown_cluster_and_tier():
    spec = longhorn_example("vsphere")["spec"]
    with pytest.raises(st.StorageError):
        st.tier_of(spec, "nope")
    with pytest.raises(st.StorageError):
        st.tier_block(spec, "apps01")


# --- the four classes --------------------------------------------------------------------

def test_every_provider_makes_the_four_classes_with_one_default():
    vs = longhorn_example("vsphere")["spec"]
    vs["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"storagePolicy": "gold"}}
    source = {"kind": "StorageClass", "provisioner": "csi.example.com", "parameters": {"tier": "1"},
              "allowVolumeExpansion": True, "volumeBindingMode": "WaitForFirstConsumer", "mountOptions": ["noatime"]}
    for classes in (st.storage_classes(longhorn_example("proxmox")["spec"], "infra"),
                    st.storage_classes(vs, "infra"),
                    st.storage_classes(proxmox_csi(longhorn_example("proxmox"))["spec"], "infra"),
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
    spec = longhorn_example("vsphere")["spec"]
    c = by_name(st.storage_classes(spec, "infra"))
    assert c["replicated"]["parameters"]["numberOfReplicas"] == "3"
    assert c["replicated"]["parameters"]["dataLocality"] == "disabled"
    for name in ("local", "local-retain"):
        assert c[name]["parameters"]["numberOfReplicas"] == "1"
        assert c[name]["parameters"]["dataLocality"] == "strict-local"
        assert c[name]["volumeBindingMode"] == "WaitForFirstConsumer"
    assert {x["provisioner"] for x in c.values()} == {"driver.longhorn.io"}


def test_array_classes_use_the_array_four_times():
    spec = proxmox_csi(longhorn_example("proxmox"))["spec"]
    c = by_name(st.storage_classes(spec, "infra"))
    assert {x["provisioner"] for x in c.values()} == {"csi.proxmox.sinextra.dev"}
    assert all(x["parameters"]["storage"] == "ceph-vm" for x in c.values())
    vs = longhorn_example("vsphere")["spec"]
    vs["storage"]["block"] = {"provider": "vsphere-csi", "vsphereCSI": {"datastoreURL": "ds:///vmfs/volumes/x/"}}
    c = by_name(st.storage_classes(vs, "infra"))
    assert c["local"]["parameters"] == {"csi.storage.k8s.io/fstype": "ext4", "datastoreurl": "ds:///vmfs/volumes/x/"}
    vs["storage"]["block"] = {"provider": "vsphere-csi"}
    assert "storagepolicyname" not in by_name(st.storage_classes(vs, "infra"))["replicated"]["parameters"]


def test_existing_copies_the_source_and_needs_it():
    spec = longhorn_example("vsphere")["spec"]
    spec["storage"]["block"] = {"provider": "existing", "existing": {"storageClass": "site-gold"}}
    with pytest.raises(st.StorageError, match="read StorageClass site-gold"):
        st.storage_classes(spec, "infra")
    source = {"kind": "StorageClass", "provisioner": "csi.example.com", "parameters": {"a": "b"},
              "allowedTopologies": [{"matchLabelExpressions": []}]}
    c = by_name(st.storage_classes(spec, "infra", source))
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
    spec = longhorn_example("vsphere")["spec"]
    spec["storage"]["block"] = {"provider": provider, key: settings}
    with pytest.raises(st.StorageError, match="not installed by DCiaB yet"):
        st.storage_classes(spec, "infra")


def test_cli_prints_provider_and_classes(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "lib")}
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "provider", str(ROOT / "examples" / "vsphere.yaml"),
                        "apps01"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "none"
    cfg = ROOT / "examples" / "metal3-cluster.yaml"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "provider", str(cfg), "metal01"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "longhorn"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "provider", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "none"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "provider", str(cfg), "infra"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "longhorn"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "classes", str(cfg), "infra"],
                       capture_output=True, text=True, env=env)
    assert [d["metadata"]["name"] for d in yaml.safe_load_all(r.stdout)][0] == "replicated"
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "classes", str(cfg), "mgmt"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "overrides.mgmt" in r.stderr
    r = subprocess.run([sys.executable, "-m", "dciab_storage", "existing", str(cfg), "infra"],
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
    r = subprocess.run([str(CLASSES_SH), str(ROOT / "examples" / "vsphere.yaml"), "infra", str(kc)],
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
    r = subprocess.run([str(CLASSES_SH), str(ROOT / "examples" / "vsphere.yaml"), "infra", str(kc)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "creating it again" in r.stdout
    assert "kubectl delete storageclass replicated --wait=true" in log.read_text()


def test_storage_classes_copies_an_existing_class(tmp_path, site):
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["block"] = {"provider": "existing", "existing": {"storageClass": "site-gold"}}
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc))
    env, log = fake_kubectl(tmp_path)
    kc = tmp_path / "kc"
    kc.write_text("x")
    r = subprocess.run([str(CLASSES_SH), str(cfg), "infra", str(kc)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "kubectl get storageclass site-gold -o json" in log.read_text()


BLOCK_SH = ROOT / "lib" / "steps" / "block-storage.sh"


def test_mgmt_storage_step_runs_block_storage_for_mgmt():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    step = text.split("\nstep storage\n", 1)[1].split("finish_if storage", 1)[0]
    assert 'lib/steps/block-storage.sh" "$config" mgmt "$mgmt_kubeconfig"' in step
    assert "addons/" not in step   # every install goes through block-storage.sh


def test_block_storage_dispatches_on_the_provider():
    text = BLOCK_SH.read_text()
    assert "python3 -m dciab_storage provider" in text
    assert 'addons/longhorn/install.sh" "$config" "$kubeconfig" --cluster "$cluster"' in text
    assert 'addons/csi/install.sh" "$config" "$cluster" "$kubeconfig"' in text
    assert 'lib/steps/storage-classes.sh" "$config" "$cluster" "$kubeconfig"' in text
    assert "isn't installed by DCiaB yet" in text


def block_storage(tmp_path, cfg, cluster="mgmt", left=""):
    """Run block-storage.sh with a kubectl that logs every call and lists `left` as the
    dciab StorageClasses on the cluster, and addons that must never run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "kubectl.log"
    (bin_dir / "kubectl").write_text(f'#!/bin/sh\necho "kubectl $*" >> {log}\n'
                                     f'case "$*" in *"get storageclass -l"*) printf "{left}" ;; esac\nexit 0\n')
    (bin_dir / "kubectl").chmod(0o755)
    (bin_dir / "helm").write_text(f'#!/bin/sh\necho "helm $*" >> {log}\nexit 0\n')
    (bin_dir / "helm").chmod(0o755)
    kc = tmp_path / "kc"
    kc.write_text("x")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run([str(BLOCK_SH), str(cfg), cluster, str(kc)], capture_output=True, text=True, env=env)
    return r, (log.read_text().splitlines() if log.exists() else [])


@pytest.mark.parametrize("name", ["vsphere", "proxmox", "proxmox-bare", "metal3-cluster"])
def test_mgmt_without_the_override_installs_and_applies_nothing(tmp_path, name):
    """ADR 0008, decision 4a: no Longhorn, no CSI driver, no StorageClass on MGMT, and the step
    says so. The cluster is only read."""
    r, calls = block_storage(tmp_path, ROOT / "examples" / f"{name}.yaml")
    assert r.returncode == 0, r.stderr
    assert "block storage: none" in r.stdout
    assert "no Longhorn, no CSI driver and no StorageClass on mgmt" in r.stdout
    assert "overrides.mgmt" in r.stdout
    assert all(c.startswith("kubectl get storageclass -l app.kubernetes.io/managed-by=dciab") for c in calls), calls
    assert "earlier dciab" not in r.stdout


def test_mgmt_without_the_override_names_classes_an_earlier_run_left(tmp_path):
    r, calls = block_storage(tmp_path, ROOT / "examples" / "proxmox.yaml",
                             left="storageclass.storage.k8s.io/replicated\\n")
    assert r.returncode == 0, r.stderr
    assert "earlier dciab applied: storageclass.storage.k8s.io/replicated" in r.stdout
    assert "leaves them" in r.stdout
    assert not [c for c in calls if " apply " in c or " delete " in c or c.startswith("helm")]


def test_mgmt_with_the_override_installs_its_provider(tmp_path):
    doc = mgmt_longhorn(load_example("proxmox"))
    doc["spec"]["storage"]["block"]["overrides"]["mgmt"] = {"provider": "existing",
                                                             "existing": {"storageClass": "site-gold"}}
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc))
    env, log = fake_kubectl(tmp_path)
    kc = tmp_path / "kc"
    kc.write_text("x")
    r = subprocess.run([str(BLOCK_SH), str(cfg), "mgmt", str(kc)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "block storage: existing" in r.stdout
    calls = log.read_text()
    assert "kubectl get storageclass site-gold -o json" in calls
    assert calls.count("apply --server-side") == 4


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
    doc = mgmt_longhorn(longhorn_example("vsphere"))
    mgmt = cluster_values(doc, "mgmt", CA)
    assert mgmt["controlPlane"]["machine"]["dataDiskGiB"] == 200
    infra = cluster_values(doc, "infra", CA)
    assert "dataDiskGiB" not in infra["controlPlane"]["machine"]
    assert {p["machine"]["dataDiskGiB"] for p in infra["workerPools"]} == {500}
    assert unsupported(doc, "infra") == []


def test_data_disk_is_dropped_without_longhorn_or_on_proxmox():
    doc = longhorn_example("vsphere")
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"] = 200
    v = cluster_values(doc, "mgmt", CA)   # MGMT keeps no volumes: no Longhorn, no data disk
    assert "dataDiskGiB" not in v["controlPlane"]["machine"]
    doc["spec"]["storage"]["block"] = {"provider": "vsphere-csi"}
    v = cluster_values(doc, "infra", CA)
    assert all("dataDiskGiB" not in p["machine"] for p in v["workerPools"])
    assert "isn't Longhorn" in unsupported(doc, "infra")[0]
    px = longhorn_example("proxmox")
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
    assert "INFRA's storage now" in w[0]["fix"] and "overrides.mgmt" in w[0]["fix"]


def test_both_forms_at_once_is_an_error(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["longhorn"] = doc["spec"]["storage"]["block"]["longhorn"]
    _, report = site.validate(doc)
    assert errors(report, "spec.storage")


def test_settings_of_another_provider_are_rejected(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["block"]["vsphereCSI"] = {}
    _, report = site.validate(doc)
    assert any("keep only" in e["fix"] for e in errors(report))
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"infra": {"proxmoxCSI": {
        "storage": "x", "shared": False, "credentials": {"tokenID": "a@pve!b", "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}}}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.infra.proxmoxCSI")


def test_override_to_a_provider_without_its_settings(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"infra": {"provider": "existing"}}
    _, report = site.validate(doc)
    assert "add existing" in errors(report, "spec.storage.block.existing")[0]["fix"]


@pytest.mark.parametrize("name,usual", [("vsphere", "vsphere-csi"), ("proxmox", "proxmox-csi")])
def test_provider_is_required_and_the_error_names_the_usual_choice(site, name, usual):
    doc = load_example(name)
    del doc["spec"]["storage"]["block"]["provider"]
    _, report = site.validate(doc)
    e = errors(report, "spec.storage.block.provider")
    assert e and e[0]["found"] == "missing" and f"the usual choice is {usual}" in e[0]["fix"]


def test_mgmt_override_needs_its_own_provider_and_takes_no_overrides(site):
    doc = longhorn_example("vsphere")
    lh = doc["spec"]["storage"]["block"]["longhorn"]
    doc["spec"]["storage"]["block"]["overrides"] = {"mgmt": {"longhorn": lh}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.mgmt.provider")
    doc["spec"]["storage"]["block"]["overrides"] = {"mgmt": {
        "provider": "longhorn", "longhorn": lh, "overrides": {"infra": {"provider": "longhorn"}}}}
    _, report = site.validate(doc)
    assert any("takes no overrides" in e["fix"] for e in errors(report, "spec.storage.block.overrides.mgmt"))


def test_mgmt_without_the_override_gets_no_storage_checks_and_a_note(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["replicas"] = 1
    _, report = site.validate(doc)
    assert not [f for f in report["findings"] if "mgmt" in f["path"] and "storage" in f["path"]]
    assert not [w for w in warnings(report) if "for mgmt" in w["found"]]
    assert any("MGMT keeps no volumes" in n for n in report["notes"])
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"] = 200
    _, report = site.validate(doc)
    w = warnings(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")
    assert w and "MGMT, which keeps no volumes" in w[0]["found"] and "overrides.mgmt" in w[0]["fix"]


def test_mgmt_override_brings_the_checks_and_keeps_the_tier_rule(site):
    doc = mgmt_longhorn(longhorn_example("vsphere"))
    _, report = site.validate(copy.deepcopy(doc))
    assert errors(report) == []
    assert not any("MGMT keeps no volumes" in n for n in report["notes"])
    # MGMT's Longhorn backs up to the site target only, never INFRA (ADR 0009, decision 1).
    doc["spec"]["storage"]["block"]["overrides"]["mgmt"]["longhorn"]["backupTarget"] = "nfs://elsewhere:/x"
    _, report = site.validate(copy.deepcopy(doc))
    assert errors(report, "spec.storage.block.overrides.mgmt.longhorn.backupTarget")
    # A provider DCiaB doesn't install yet is an error on MGMT, as before.
    doc["spec"]["storage"]["block"]["overrides"]["mgmt"] = {"provider": "vendor-csi", "vendorCSI": {"driver": "trident"}}
    _, report = site.validate(copy.deepcopy(doc))
    assert "not yet installed" in errors(report, "spec.storage.block.overrides.mgmt.provider")[0]["found"]
    # A Longhorn on vSphere MGMT needs its data disk.
    doc = mgmt_longhorn(longhorn_example("vsphere"))
    del doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"]
    _, report = site.validate(doc)
    assert errors(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")


def test_proxmox_csi_token_is_needed_only_where_the_driver_runs():
    """INFRA's Proxmox CSI token is in the config, but MGMT never reads it: the mgmt
    storage step installs nothing without overrides.mgmt."""
    doc = proxmox_csi(longhorn_example("proxmox"))
    assert st.cluster_block(doc["spec"], "mgmt") is None
    assert st.cluster_block(doc["spec"], "infra")["settings"]["credentials"]["tokenID"] == "csi@pve!dciab"


def test_workload_override_needs_its_own_provider(site):
    doc = longhorn_example("vsphere")
    lh = doc["spec"]["storage"]["block"]["longhorn"]
    doc["spec"]["storage"]["block"]["overrides"] = {"workload": {"longhorn": lh}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.workload.provider")
    doc["spec"]["storage"]["block"]["overrides"] = {"workload": {
        "provider": "longhorn", "longhorn": lh, "overrides": {"mgmt": {"provider": "longhorn"}}}}
    _, report = site.validate(doc)
    assert any("takes no overrides" in e["fix"] for e in errors(report, "spec.storage.block.overrides.workload"))


def test_workload_clusters_without_the_override_get_no_storage_checks(site):
    doc = longhorn_example("vsphere")
    pool = doc["spec"]["workloadClusters"][0]["workerPools"][0]
    del pool["dataDiskGiB"]
    _, report = site.validate(doc)
    assert not [f for f in report["findings"] if "workloadClusters" in f["path"]]
    pool["dataDiskGiB"] = 200
    _, report = site.validate(doc)
    w = warnings(report, "spec.workloadClusters[0].workerPools[0].dataDiskGiB")
    assert w and "no block storage" in w[0]["found"] and "overrides.workload" in w[0]["fix"]
    # Opting in with Longhorn brings its checks to the workload clusters.
    lh = dict(doc["spec"]["storage"]["block"]["longhorn"])
    doc["spec"]["storage"]["block"]["overrides"] = {"workload": {"provider": "longhorn", "longhorn": lh}}
    del pool["dataDiskGiB"]
    _, report = site.validate(doc)
    assert errors(report, "spec.workloadClusters[0].workerPools[0].dataDiskGiB")


def test_overrides_are_keyed_by_tier_only(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["storage"]["block"]["overrides"] = {"apps01": {"provider": "longhorn"}}
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.overrides.apps01")


@pytest.mark.parametrize("provider,key,settings", [
    ("ceph-csi", "cephCSI", {"clusterID": "x", "monitors": ["m:6789"], "pool": "rbd", "userID": "k8s",
                             "key": {"env": "DCIAB_PVE_TOKEN"}}),
    ("vendor-csi", "vendorCSI", {"driver": "trident"}),
])
def test_providers_not_installed_yet(site, provider, key, settings):
    doc = longhorn_example("vsphere")
    lh = doc["spec"]["storage"]["block"]["longhorn"]
    doc["spec"]["storage"]["block"] = {"provider": "longhorn", "longhorn": lh,
                                       "overrides": {"infra": {"provider": provider, key: settings}}}
    _, report = site.validate(doc)
    w = warnings(report, "spec.storage.block.overrides.infra.provider")
    assert w and "not yet installed by DCiaB" in w[0]["found"]
    # INFRA's own provider: a warning, as INFRA isn't built yet; on MGMT it is an error.
    doc["spec"]["storage"]["block"] = {"provider": provider, key: settings}
    _, report = site.validate(doc)
    assert "not yet installed by DCiaB" in warnings(report, "spec.storage.block.provider")[0]["found"]
    doc["spec"]["storage"]["block"]["overrides"] = {"mgmt": {"provider": provider, key: settings}}
    _, report = site.validate(doc)
    assert "not yet installed by DCiaB" in errors(report, "spec.storage.block.overrides.mgmt.provider")[0]["found"]


def test_array_provider_only_on_its_platform(site):
    doc = proxmox_csi(longhorn_example("vsphere"))
    _, report = site.validate(doc)
    assert any("works only on proxmox clusters" in e["fix"] for e in errors(report, "spec.storage.block.provider"))


def test_proxmox_csi_needs_its_own_token(site):
    doc = proxmox_csi(longhorn_example("proxmox"))
    doc["spec"]["storage"]["block"]["proxmoxCSI"]["credentials"]["tokenID"] = "dciab@pve!capmox"
    _, report = site.validate(doc)
    assert errors(report, "spec.storage.block.proxmoxCSI.credentials")


def test_longhorn_on_vsphere_needs_data_disks(site):
    doc = longhorn_example("vsphere")
    del doc["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"]
    _, report = site.validate(doc)
    assert errors(report, "spec.clusters.infra.workerPools[0].dataDiskGiB")
    assert not errors(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")


def test_data_disk_rules(site):
    doc = mgmt_longhorn(longhorn_example("vsphere"))
    doc["spec"]["clusters"]["mgmt"]["controlPlane"]["dataDiskGiB"] = 100   # = diskGiB
    _, report = site.validate(doc)
    assert "another size" in errors(report, "spec.clusters.mgmt.controlPlane.dataDiskGiB")[0]["fix"]
    doc = longhorn_example("proxmox")
    doc["spec"]["clusters"]["infra"]["workerPools"][0]["dataDiskGiB"] = 200
    _, report = site.validate(doc)
    assert "CAPMOX" in errors(report, "spec.clusters.infra.workerPools[0].dataDiskGiB")[0]["fix"]


def test_longhorn_on_proxmox_warns_about_the_root_disk(site):
    _, report = site.validate(longhorn_example("proxmox"))
    assert "root disk" in warnings(report, "spec.clusters.infra")[0]["found"]
    assert not warnings(report, "spec.clusters.mgmt")
    _, report = site.validate(mgmt_longhorn(longhorn_example("proxmox")))
    assert "root disk" in warnings(report, "spec.clusters.mgmt")[0]["found"]


def test_more_replicas_than_longhorn_nodes(site):
    doc = longhorn_example("vsphere")
    doc["spec"]["clusters"]["infra"]["workerPools"][0]["replicas"] = 1
    _, report = site.validate(doc)
    found = [w["found"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount")]
    assert any("spec.clusters.infra has 1 node(s)" in f for f in found)


def test_double_replication_hint_skips_local_proxmox_storage(site):
    doc = longhorn_example("proxmox")
    doc["spec"]["storage"]["block"]["longhorn"]["replicaCount"] = 2
    _, report = site.validate(doc)
    assert any("ceph-vm" in w["found"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount"))
    doc["spec"]["platform"]["proxmox"]["storage"] = "local-lvm"
    _, report = site.validate(doc)
    assert not any("local-lvm" in w["found"] for w in warnings(report))


def test_existing_needs_a_class_name(site):
    doc = longhorn_example("vsphere")
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
    assert warnings(report, "spec.clusters.infra.workerPools[0].dataDiskGiB")


def test_one_replica_on_vms_warns_that_a_replaced_node_takes_it(site):
    doc = longhorn_example("proxmox")
    doc["spec"]["storage"]["block"]["longhorn"]["replicaCount"] = 1
    _, report = site.validate(doc)
    fix = next(w["fix"] for w in warnings(report, "spec.storage.block.longhorn.replicaCount") if w["found"] == "1")
    assert "deleted when a node is replaced" in fix


def test_until_longhorn_still_means_storage():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    assert 'if [[ "$until" == longhorn ]]; then' in text and "until=storage" in text
    assert text.index("until=storage") < text.index("--until must be one of")
