"""MGMT's backup jobs (lib/dciab_backup/jobs.py) and the runner they run
(lib/dciab_backup/runner.py): retention, targets, encryption, CAPI exports, manifests."""

import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, load_example

from dciab_backup import jobs  # noqa: E402
from dciab_backup import runner as rn  # noqa: E402

RUNNER = ROOT / "lib" / "dciab_backup" / "runner.py"
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
PINNED = {**LOCK, "images": {**LOCK["images"], "backup": "ghcr.io/example/dciab-backup:abc@sha256:" + "d" * 64}}
HAVE_AGE = shutil.which("age") and shutil.which("age-keygen")
needs_age = pytest.mark.skipif(not HAVE_AGE, reason="age is not installed")


def name(stamp, kind="etcd", cluster="mgmt", ext=".db"):
    return f"{kind}-{cluster}-{stamp}{ext}.age"


# --- retention ---------------------------------------------------------------------------

def hourly_for(days):
    return [name(f"202611{d:02d}T{h:02d}0000Z") for d in range(1, days + 1) for h in range(24)]


def test_keep_hourly_and_daily():
    names = hourly_for(20)
    kept = rn.to_keep(names, {"hourly": 24, "daily": 14})
    assert set(name(f"20261120T{h:02d}0000Z") for h in range(24)) <= kept
    # The newest of each of the 14 newest days.
    assert {name(f"202611{d:02d}T230000Z") for d in range(7, 21)} <= kept
    assert len(kept) == 24 + 13   # the 20th's newest is counted by both rules
    assert name("20261106T230000Z") not in kept


def test_keep_days_counts_back_from_the_newest_copy():
    names = [name(f"202611{d:02d}T{h:02d}0000Z") for d in range(1, 11) for h in (0, 6, 12, 18)]
    kept = rn.to_keep(names, {"days": 7})
    assert name("20261110T180000Z") in kept and name("20261103T180000Z") not in kept
    assert name("20261104T000000Z") in kept
    # An outage: nothing new for a month. The last copies stay.
    assert rn.to_keep(names[:8], {"days": 7}) == set(names[:8])


def test_foreign_files_are_never_pruned_and_no_rule_keeps_everything():
    names = hourly_for(3) + ["README", "etcd-other.db"]
    assert {"README", "etcd-other.db"} <= rn.to_keep(names, {"hourly": 1})
    assert rn.to_keep(names, {}) == set(names)


# --- targets and storing -----------------------------------------------------------------

def keys_file(tmp_path, n=3):
    keys = []
    for _ in range(n):
        out = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        keys.append((out.split("# public key: ")[1].split()[0], out))
    f = tmp_path / "recipients.txt"
    f.write_text("\n".join(k for k, _ in keys) + "\n")
    return f, keys


@needs_age
def test_store_encrypts_uploads_and_prunes_on_nfs(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    rec, keys = keys_file(tmp_path)
    t = rn.Target("nfs://nas:/backups", str(root))
    old = root / "dciab" / "mgmt" / "etcd"
    old.mkdir(parents=True)
    for h in range(3):
        (old / name(f"20261101T0{h}0000Z")).write_text("old")
    src = tmp_path / "etcd.db"
    src.write_bytes(b"snapshot bytes")
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    rel = rn.store("etcd", "mgmt", src, ".db", t, rec, {"hourly": 2}, now=now)
    assert rel == "dciab/mgmt/etcd/etcd-mgmt-20261123T120000Z.db.age"
    assert sorted(p.name for p in old.iterdir()) == [name("20261101T020000Z"), name("20261123T120000Z")]
    blob = (root / rel).read_bytes()
    assert b"snapshot bytes" not in blob
    # Any one key opens it: each recipient, and the backup key.
    for _, identity in keys:
        idf = tmp_path / "id.txt"
        idf.write_text(identity)
        out = subprocess.run(["age", "-d", "-i", str(idf), str(root / rel)], capture_output=True, check=True).stdout
        assert out == b"snapshot bytes"


@needs_age
def test_store_refuses_fewer_than_two_recipients(tmp_path):
    rec, _ = keys_file(tmp_path, n=1)
    (tmp_path / "x").mkdir()
    src = tmp_path / "f"
    src.write_text("x")
    with pytest.raises(rn.RunnerError, match="fewer than two"):
        rn.store("etcd", "mgmt", src, ".db", rn.Target("nfs://n:/x", str(tmp_path / "x")), rec, {})


def test_nfs_target_must_be_mounted(tmp_path):
    with pytest.raises(rn.RunnerError, match="isn't mounted"):
        rn.Target("nfs://nas:/x", str(tmp_path / "missing"))


FAKE_RCLONE = r'''
import os, shutil, sys
args = [a for a in sys.argv[1:]]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
if "--ca-cert" in args:
    i = args.index("--ca-cert"); del args[i:i + 2]
store = os.environ["FAKE_S3"]
def local(remote):
    assert remote.startswith("dciab:"), remote
    return os.path.join(store, remote[len("dciab:"):])
assert os.environ["RCLONE_CONFIG_DCIAB_TYPE"] == "s3"
verb = args[0]
if verb == "copyto":
    os.makedirs(os.path.dirname(local(args[2])), exist_ok=True); shutil.copyfile(args[1], local(args[2]))
elif verb == "lsf":
    d = local(args[-1])
    print("\n".join(sorted(os.listdir(d))) if os.path.isdir(d) else "")
elif verb == "deletefile":
    os.unlink(local(args[1]))
'''


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(f"#!{sys.executable}\n{FAKE_RCLONE}")
    (bin_dir / "rclone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "rclone.log"))
    monkeypatch.setenv("FAKE_S3", str(tmp_path / "s3"))
    return bin_dir


@needs_age
def test_s3_target_through_rclone(tmp_path, fake_tools):
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "AWS_CERT": "PEM"}
    t = rn.Target("s3://bkp@s3.example.net:8333/site", env=env, workdir=tmp_path)
    assert t.env["RCLONE_CONFIG_DCIAB_ENDPOINT"] == "https://s3.example.net:8333"
    assert t.env["RCLONE_CONFIG_DCIAB_PROVIDER"] == "Other"
    assert t.env["RCLONE_CONFIG_DCIAB_NO_CHECK_BUCKET"] == "true"
    assert t.flags == ["--ca-cert", str(tmp_path / "target-ca.pem")]
    rec, _ = keys_file(tmp_path)
    src = tmp_path / "capi.tar"
    src.write_text("tar")
    rel = rn.store("capi", "mgmt", src, ".tar", t, rec, {"daily": 14})
    assert (tmp_path / "s3" / "bkp" / "site" / rel).is_file()
    log = (tmp_path / "rclone.log").read_text()
    assert "copyto --ca-cert" in log and "lsf --files-only" in log
    aws = rn.Target("s3://bkp@s3.eu-west-2.amazonaws.com", env=env, workdir=tmp_path)
    assert aws.env["RCLONE_CONFIG_DCIAB_REGION"] == "eu-west-2" and aws.prefix == "dciab:bkp"


def test_s3_target_needs_credentials():
    with pytest.raises(rn.RunnerError, match="AWS_ACCESS_KEY_ID"):
        rn.Target("s3://bkp@s3.example.net", env={})


@needs_age
def test_runner_etcd_checks_and_deletes_the_plaintext(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    rec, _ = keys_file(tmp_path)
    src = tmp_path / "etcd.db"
    src.write_bytes(b"x")
    bin_dir = tmp_path / "ebin"
    bin_dir.mkdir()
    (bin_dir / "etcdutl").write_text('#!/bin/sh\n[ -n "$FAKE_BAD" ] && exit 1\necho "| HASH | REVISION |"\n')
    (bin_dir / "etcdutl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DCIAB_CLUSTER": "mgmt", "DCIAB_TARGET_URL": "nfs://nas:/b", "DCIAB_TARGET_ROOT": str(root),
           "DCIAB_RECIPIENTS": str(rec), "DCIAB_KEEP": '{"hourly": 24, "daily": 14}'}
    r = subprocess.run([sys.executable, str(RUNNER), "etcd", "--src", str(src)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not src.exists()
    assert len(list((root / "dciab" / "mgmt" / "etcd").iterdir())) == 1
    # A failed upload deletes it too, and fails.
    src.write_bytes(b"x")
    r = subprocess.run([sys.executable, str(RUNNER), "etcd", "--src", str(src)],
                       env={**env, "DCIAB_TARGET_ROOT": str(tmp_path / "gone")}, capture_output=True, text=True)
    assert r.returncode == 1 and not src.exists()
    # A snapshot etcdutl can't read is never uploaded, and is deleted.
    src.write_bytes(b"x")
    r = subprocess.run([sys.executable, str(RUNNER), "etcd", "--src", str(src)], env={**env, "FAKE_BAD": "1"},
                       capture_output=True, text=True)
    assert r.returncode == 1 and "etcdutl" in r.stderr and not src.exists()
    assert len(list((root / "dciab" / "mgmt" / "etcd").iterdir())) == 1


def test_runner_is_standard_library_only():
    text = RUNNER.read_text()
    assert "import yaml" not in text and "dciab_" not in text.split('"""', 2)[2].split("def ")[0]


# --- CAPI exports ------------------------------------------------------------------------

FAKE_KUBECTL = r'''
import json, os, sys
state = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write("kubectl " + " ".join(args[2:]) + "\n")
if "machines.cluster.x-k8s.io" in args:
    busy = state["busy"].pop(0) if state["busy"] else False
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
    phase = "Provisioning" if busy else "Running"
    print(json.dumps({"items": [{"metadata": {"namespace": "cluster-mgmt", "name": "m0"}, "status": {"phase": phase}}]}))
elif "get" in args and "clusterclasses.cluster.x-k8s.io" in args:
    notes = {"cluster.x-k8s.io/paused": ""} if state.get("ccpaused") else {}
    print(json.dumps({"items": [{"metadata": {"namespace": "dciab-clusterclasses", "name": "dciab-proxmox-r1",
                                              "annotations": notes}}]}))
elif "annotate" in args and "clusterclasses.cluster.x-k8s.io" in args:
    state["ccpaused"] = False
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
elif "get" in args:
    paused = state.get("paused", False)
    print(json.dumps({"items": [{"metadata": {"namespace": "cluster-mgmt", "name": "mgmt", "annotations": {}},
                                 "spec": {"paused": paused}}]}))
elif "patch" in args:
    state["paused"] = False
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
'''
FAKE_CLUSTERCTL = r'''
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write("clusterctl " + " ".join(args) + "\n")
d = args[args.index("--to-directory") + 1]
open(os.path.join(d, "Cluster_cluster-mgmt_mgmt.yaml"), "w").write("kind: Cluster\n")
if os.environ.get("FAKE_LEAVE_PAUSED"):
    state = json.load(open(os.environ["FAKE_STATE"])); state["paused"] = True
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
if os.environ.get("FAKE_LEAVE_CLASS_PAUSED"):
    state = json.load(open(os.environ["FAKE_STATE"])); state["ccpaused"] = True
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
'''


@pytest.fixture
def capi(tmp_path, monkeypatch):
    bin_dir = tmp_path / "cbin"
    bin_dir.mkdir()
    for n, body in (("kubectl", FAKE_KUBECTL), ("clusterctl", FAKE_CLUSTERCTL)):
        (bin_dir / n).write_text(f"#!{sys.executable}\n{body}")
        (bin_dir / n).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "capi.log"))
    st = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_STATE", str(st))

    def setup(busy=(), paused=False, ccpaused=False):
        st.write_text(json.dumps({"busy": list(busy), "paused": paused, "ccpaused": ccpaused}))
    return setup


def test_capi_export_waits_for_quiet_clusters(tmp_path, capi):
    capi(busy=[True, True])
    slept = []
    tar = rn.capi_export(Path("kc"), tmp_path, quiet_minutes=30, sleep=slept.append)
    assert len(slept) == 2
    import tarfile
    with tarfile.open(tar) as tf:
        assert "capi/cluster-mgmt/Cluster_cluster-mgmt_mgmt.yaml" in tf.getnames()
    log = (tmp_path / "capi.log").read_text()
    assert "--namespace cluster-mgmt --to-directory" in log


def test_capi_export_gives_up_when_never_quiet(tmp_path, capi):
    capi(busy=[True] * 100)
    with pytest.raises(rn.RunnerError, match="weren't quiet"):
        rn.capi_export(Path("kc"), tmp_path, quiet_minutes=0, sleep=lambda s: None)


def test_capi_export_resumes_a_cluster_it_left_paused(tmp_path, capi, monkeypatch):
    capi()
    monkeypatch.setenv("FAKE_LEAVE_PAUSED", "1")
    with pytest.raises(rn.RunnerError, match="left Cluster cluster-mgmt/mgmt paused"):
        rn.capi_export(Path("kc"), tmp_path, sleep=lambda s: None)
    assert "patch clusters.cluster.x-k8s.io mgmt" in (tmp_path / "capi.log").read_text()


def test_capi_export_resumes_a_clusterclass_it_left_paused(tmp_path, capi, monkeypatch):
    capi()
    monkeypatch.setenv("FAKE_LEAVE_CLASS_PAUSED", "1")
    with pytest.raises(rn.RunnerError, match="ClusterClass dciab-clusterclasses/dciab-proxmox-r1"):
        rn.capi_export(Path("kc"), tmp_path, sleep=lambda s: None)
    log = (tmp_path / "capi.log").read_text()
    assert "annotate clusterclasses.cluster.x-k8s.io dciab-proxmox-r1 cluster.x-k8s.io/paused-" in log
    assert json.loads(Path(os.environ["FAKE_STATE"]).read_text())["ccpaused"] is False


def test_capi_export_leaves_what_was_paused_before_alone(tmp_path, capi):
    capi(paused=True, ccpaused=True)
    rn.capi_export(Path("kc"), tmp_path, sleep=lambda s: None)
    log = (tmp_path / "capi.log").read_text()
    assert "patch" not in log and "annotate" not in log


# --- the backup key ----------------------------------------------------------------------

@needs_age
def test_generated_key_is_sealed_to_the_recipients(tmp_path):
    spec = load_example("vsphere")["spec"]
    ids = []
    recips = []
    for _ in range(2):
        out = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        recips.append(out.split("# public key: ")[1].split()[0])
        ids.append(out)
    spec["bootstrap"]["recipients"] = recips
    pub = jobs.generate_key(spec, tmp_path)
    assert jobs.public_key(spec, tmp_path) == pub
    sealed = tmp_path / "backup" / "backup-key.age"
    assert b"AGE-SECRET-KEY" not in sealed.read_bytes()
    idf = tmp_path / "id"
    idf.write_text(ids[1])
    opened = subprocess.run(["age", "-d", "-i", str(idf), str(sealed)], capture_output=True, text=True, check=True).stdout
    assert "AGE-SECRET-KEY-" in opened and pub in opened
    assert jobs.recipients(spec, tmp_path) == recips + [pub]
    assert oct((tmp_path / "backup").stat().st_mode & 0o777) == "0o700"


def test_the_configs_key_wins(tmp_path):
    spec = load_example("vsphere")["spec"]
    with pytest.raises(jobs.BackupError, match="no backup key"):
        jobs.recipients(spec, tmp_path)
    spec["backup"]["key"] = {"publicKey": "age1" + "z" * 58}
    assert jobs.recipients(spec, tmp_path)[-1] == "age1" + "z" * 58


# --- Secret and manifests ----------------------------------------------------------------

def test_secret_for_s3_only(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "backup-site.env").write_text("AWS_ACCESS_KEY_ID=a\nAWS_SECRET_ACCESS_KEY=b\nOTHER=x\n")
    (tmp_path / "secrets" / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    spec = load_example("vsphere")["spec"]
    spec["backup"]["targets"]["site"]["caBundle"] = {"file": "./secrets/ca.pem"}
    m = jobs.secret_manifest(spec, tmp_path)
    data = {k: base64.b64decode(v).decode() for k, v in m["data"].items()}
    assert data == {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "AWS_CERT": "-----BEGIN CERTIFICATE-----"}
    assert m["metadata"]["namespace"] == jobs.NS
    assert jobs.secret_manifest(load_example("proxmox")["spec"], tmp_path) is None


KEYS = ["age1" + c * 58 for c in "qpz"]


def by_kind(docs):
    out = {}
    for d in docs:
        out.setdefault(d["kind"], []).append(d)
    return out


def cron(docs, n):
    return next(d for d in docs if d["kind"] == "CronJob" and d["metadata"]["name"] == n)


def test_mgmt_manifests_s3():
    spec = load_example("vsphere")["spec"]
    spec["backup"]["schedules"] = {"etcd": {"mgmt": {"cron": "15 * * * *"}}}
    docs = jobs.manifests(spec, "mgmt", KEYS, PINNED)
    k = by_kind(docs)
    assert k["ConfigMap"][0]["data"]["recipients.txt"].split() == KEYS
    etcd = cron(docs, "etcd-snapshot")
    assert etcd["spec"]["schedule"] == "15 * * * *" and etcd["spec"]["concurrencyPolicy"] == "Forbid"
    pod = etcd["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["hostNetwork"] and pod["nodeSelector"] == {"node-role.kubernetes.io/control-plane": ""}
    [snap] = pod["initContainers"]
    assert snap["image"].startswith("registry.k8s.io/etcd:") and "@sha256:" in snap["image"]
    assert "snapshot" in snap["command"] and "save" in snap["command"]
    # Only the client certificate and the CA certificate: never etcd's CA key or server keys.
    pki = [v["hostPath"] for v in pod["volumes"] if "hostPath" in v]
    assert sorted(h["path"].rsplit("/", 1)[1] for h in pki) == ["ca.crt", "healthcheck-client.crt",
                                                                 "healthcheck-client.key"]
    assert all(h["type"] == "File" for h in pki)
    job = etcd["spec"]["jobTemplate"]["spec"]
    assert job["ttlSecondsAfterFinished"] and etcd["spec"]["failedJobsHistoryLimit"] == 1
    up = pod["containers"][0]
    assert up["image"] == PINNED["images"]["backup"]
    assert up["envFrom"] == [{"secretRef": {"name": jobs.SECRET}}]
    env = {e["name"]: e["value"] for e in up["env"]}
    assert json.loads(env["DCIAB_KEEP"]) == {"hourly": 24, "daily": 14}
    assert env["DCIAB_TARGET_URL"] == spec["backup"]["targets"]["site"]["url"]
    capi = cron(docs, "capi-export")
    assert capi["spec"]["schedule"] == "30 2 * * *"
    cpod = capi["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert cpod["serviceAccountName"] == jobs.EXPORT_SA
    assert k["ClusterRoleBinding"][0]["subjects"][0]["name"] == jobs.EXPORT_SA


def test_mgmt_manifests_need_no_storage_class():
    """MGMT may run no block storage: the jobs use the node's disk and the target, never a PVC."""
    docs = jobs.manifests(load_example("proxmox")["spec"], "mgmt", KEYS, PINNED)
    text = yaml.safe_dump_all(docs)
    assert "persistentVolumeClaim" not in text and "storageClassName" not in text
    pod = cron(docs, "etcd-snapshot")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in pod["volumes"]}
    assert "emptyDir" in vols["work"]
    assert vols["target"]["nfs"] == {"server": "nas01.dc3.example.net", "path": "/exports/dciab-backup"}
    assert pod["containers"][0]["envFrom"] == []


def test_manifests_refuse_an_unpinned_image_and_other_clusters():
    spec = load_example("vsphere")["spec"]
    with pytest.raises(jobs.BackupError, match="images.backup"):
        jobs.manifests(spec, "mgmt", KEYS, {**LOCK, "images": {**LOCK["images"], "backup": ""}})
    with pytest.raises(jobs.BackupError, match="MGMT only"):
        jobs.manifests(spec, "infra", KEYS, PINNED)
    with pytest.raises(jobs.BackupError, match="three keys"):
        jobs.manifests(spec, "mgmt", KEYS[:2], PINNED)


def test_etcd_image_is_the_clusters_own():
    assert jobs.etcd_image(LOCK).startswith("registry.k8s.io/etcd:3.")


def test_export_now_keeps_a_local_copy_and_records_it(tmp_path):
    spec = load_example("proxmox")["spec"]
    spec["backup"]["key"] = {"publicKey": KEYS[2]}
    fake = tmp_path / "fake-runner"
    fake.write_text("#!/bin/sh\nmkdir -p \"$DCIAB_KEEP_LOCAL\" && env > \"$DCIAB_KEEP_LOCAL/env\"\n")
    fake.chmod(0o755)

    class Mounted:
        def __init__(self, spec):
            pass

        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *a):
            return False
    orig = jobs.mounted
    jobs.mounted = Mounted
    try:
        jobs.export_now(spec, tmp_path, tmp_path / "state", Path("/kc"), "before-move", runner_cmd=[str(fake)])
    finally:
        jobs.mounted = orig
    env = dict(line.split("=", 1) for line in (tmp_path / "state" / "capi-backup" / "env").read_text().splitlines()
               if "=" in line)
    assert env["DCIAB_KUBECONFIG"] == "/kc" and env["DCIAB_TARGET_ROOT"] == str(tmp_path)
    assert "before-move" in (tmp_path / "state" / "capi-backup" / "LAST").read_text()
    fake.write_text("#!/bin/sh\nexit 1\n")
    jobs.mounted = Mounted
    try:
        with pytest.raises(jobs.BackupError, match="before-move failed"):
            jobs.export_now(spec, tmp_path, tmp_path / "state", Path("/kc"), "before-move", runner_cmd=[str(fake)])
    finally:
        jobs.mounted = orig


def test_cli_site_kind_and_manifests(tmp_path):
    cfg = tmp_path / "dciab.yaml"
    doc = load_example("proxmox")
    doc["spec"]["backup"]["key"] = {"publicKey": KEYS[2]}
    cfg.write_text(yaml.safe_dump(doc))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "lib")}
    r = subprocess.run([sys.executable, "-m", "dciab_backup", "site-kind", str(cfg)], capture_output=True, text=True,
                       env=env)
    assert r.stdout.strip() == "nfs"
    r = subprocess.run([sys.executable, "-m", "dciab_backup", "manifests", str(cfg), "mgmt", "--state", str(tmp_path)],
                       capture_output=True, text=True, env=env)
    if "@sha256:" in (LOCK["images"].get("backup") or ""):
        assert r.returncode == 0, r.stderr
    else:
        assert r.returncode == 1 and "images.backup" in r.stderr


def test_backup_image_recipe_is_pinned():
    text = (ROOT / "images" / "backup" / "build.sh").read_text()
    assert "sha256sum" in text and "bundle.lock" in text
    assert LOCK["backupImage"]["base"].count("@sha256:") == 1
    r = subprocess.run([str(ROOT / "images" / "backup" / "build.sh"), "--recipe-id"], capture_output=True, text=True)
    assert r.returncode == 0 and len(r.stdout.strip()) == 12


def test_mgmt_backups_step_applies_server_side():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    step = text.split("step backups", 1)[1]
    # The pin is checked (manifests rendered) before the credentials Secret is applied.
    assert step.index("dciab_backup manifests") < step.index("dciab_backup secret")
    assert 'dciab_backup secret "$config" | on_mgmt kubectl apply --server-side' in step
    assert '--server-side --field-manager=dciab --force-conflicts -f - <<<"$backup_jobs"' in step
    # Credentials never land on disk.
    assert "> \"$state" not in step


def test_after_move_export_is_retried_until_recorded():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    adopt = text.split("step adopt", 1)[1].split("finish_if adopt", 1)[0]
    assert "grep -qs ' after-move$' \"$state/capi-backup/LAST\"" in adopt
    backup = text.split("capi_backup() {", 1)[1].split("\n}", 1)[0]
    assert backup.count("|| return") == 2


class FakeTarget:
    store = {}
    fail = 0

    def __init__(self, url, root=None, env=None, workdir=None):
        pass

    def list(self, d):
        return sorted(k.rsplit("/", 1)[1] for k in self.store if k.startswith(d + "/"))

    def put(self, src, rel):
        if FakeTarget.fail:
            FakeTarget.fail -= 1
            raise rn.RunnerError("connection refused")
        self.store[rel] = Path(src).read_bytes()


@pytest.fixture
def fake_target(monkeypatch, tmp_path):
    class NoMount:
        def __init__(self, spec):
            pass

        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    FakeTarget.store, FakeTarget.fail = {}, 0
    monkeypatch.setattr(rn, "Target", FakeTarget)
    monkeypatch.setattr(jobs, "mounted", NoMount)
    monkeypatch.setattr(jobs, "target_env", lambda spec, d: {})
    return FakeTarget


def test_sealed_key_upload_is_idempotent_and_retried(tmp_path, fake_target):
    spec = load_example("vsphere")["spec"]
    d = tmp_path / "backup"
    d.mkdir()
    (d / "backup-key.age").write_bytes(b"sealed")
    (d / "backup-key.pub").write_text(KEYS[2] + "\n")
    fake_target.fail = 2
    assert jobs.seal_key_to_target(spec, tmp_path, tmp_path, sleep=lambda s: None)
    assert fake_target.store == {f"dciab/recovery/backup-key-{KEYS[2]}.age": b"sealed"}
    # Already there: nothing uploaded again.
    fake_target.fail = 5
    assert jobs.seal_key_to_target(spec, tmp_path, tmp_path, sleep=lambda s: None)
    # Never reaches the target: the step fails.
    fake_target.store.clear()
    with pytest.raises(jobs.BackupError, match="isn't on the site target"):
        jobs.seal_key_to_target(spec, tmp_path, tmp_path, sleep=lambda s: None)
    # A key the config gives has no sealed copy of DCiaB's to upload.
    spec["backup"]["key"] = {"publicKey": KEYS[0]}
    assert jobs.seal_key_to_target(spec, tmp_path, tmp_path, sleep=lambda s: None) is False
