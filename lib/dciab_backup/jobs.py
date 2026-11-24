"""MGMT's backup jobs (ADR 0009, decisions 2, 3 and 10): the backup key, the site target's
Secret, the etcd snapshot and CAPI export CronJobs, and the CAPI exports dciab mgmt takes
around the pivot.

Every backup is age-encrypted to spec.bootstrap.recipients and to the backup key, before it
leaves the machine that made it. Clusters only ever hold the public keys.

The backup key: spec.backup.key.publicKey when the config gives one. Otherwise DCiaB
generates an age key pair the first time it needs one (dciab mgmt's move step) and never
writes its private half in plaintext: it is sealed to the recipients at once and kept as
<state>/backup/backup-key.age on the bootstrap host and as dciab/recovery/backup-key-<public key>.age
on the site target. Until OpenBao exists, those two sealed copies are the only ones, so a
restore needs a recipient to open either the backup itself or the sealed key. When OpenBao
comes, the private half also goes to its platform KV mount (ADR 0009, decision 10).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from dciab_cluster.credentials import resolve

from . import BackupError, schedules, site_target, target_kind
from .runner import NFS_URL, S3_URL, parse_env, stamp

NS = "dciab-backup"
SECRET = "dciab-backup-target"
RECIPIENTS_CM = "dciab-backup-recipients"
EXPORT_SA = "dciab-capi-export"
RUNNER = "/usr/libexec/dciab/backup-runner"
ETCD_PKI = "/etc/kubernetes/pki/etcd"
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"
MANAGED_BY = {"app.kubernetes.io/managed-by": "dciab", "app.kubernetes.io/part-of": "dciab-backup"}
AGE_KEY = re.compile(r"^age1[0-9a-z]{58}$")


# -- the backup key -------------------------------------------------------------------------

def key_dir(state: Path) -> Path:
    return state / "backup"


def public_key(spec: dict, state: Path) -> Optional[str]:
    """The backup key's public half: the config's, else the one generated earlier; None if neither."""
    given = ((spec.get("backup") or {}).get("key") or {}).get("publicKey")
    if given:
        return given
    pub = key_dir(state) / "backup-key.pub"
    if pub.is_file():
        text = pub.read_text().strip()
        if AGE_KEY.match(text):
            return text
    return None


def generate_key(spec: dict, state: Path, keygen: str = "age-keygen", age: str = "age") -> str:
    """A new backup key pair: the private half sealed to the recipients straight from
    age-keygen's output (it never touches a disk in plaintext), the public half beside it."""
    out = subprocess.run([keygen], capture_output=True, text=True, check=True).stdout
    m = re.search(r"^# public key: (age1[0-9a-z]{58})$", out, re.M)
    if not m:
        raise BackupError("age-keygen printed no public key")
    args = [age, "--encrypt"]
    for r in spec["bootstrap"]["recipients"]:
        args += ["-r", r]
    sealed = subprocess.run(args, input=out.encode(), capture_output=True, check=True).stdout
    d = key_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    (d / "backup-key.age").write_bytes(sealed)
    (d / "backup-key.pub").write_text(m.group(1) + "\n")
    return m.group(1)


def recipients(spec: dict, state: Path) -> List[str]:
    """Every key a backup is encrypted to: the bootstrap recipients and the backup key."""
    key = public_key(spec, state)
    if key is None:
        raise BackupError(f"no backup key: none in spec.backup.key and none in {key_dir(state)}; "
                          "dciab mgmt's move step generates one")
    return list(spec["bootstrap"]["recipients"]) + ([key] if key not in spec["bootstrap"]["recipients"] else [])


# -- the site target ---------------------------------------------------------------------

def target(spec: dict) -> dict:
    t = site_target(spec)
    if t is None:
        raise BackupError("no site target: add spec.backup.targets.site (ADR 0009)")
    return t


def target_env(spec: dict, config_dir: Path) -> Dict[str, str]:
    """The site target's S3 credentials as the runner reads them (AWS_*); empty for NFS."""
    t = target(spec)
    if target_kind(t["url"]) == "nfs":
        return {}
    m = S3_URL.match(t["url"])
    if not m or "credentials" not in t:
        raise BackupError(f"{t['url']}: an S3 site target needs s3://<bucket>@<endpoint>[/<path>] and credentials")
    env = parse_env(resolve(t["credentials"], config_dir))
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not env.get(k):
            raise BackupError(f"the site target's credentials have no {k}")
    keep = {k: v for k, v in env.items() if k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINTS")}
    if "caBundle" in t:
        keep["AWS_CERT"] = resolve(t["caBundle"], config_dir)
    return keep


def secret_manifest(spec: dict, config_dir: Path) -> Optional[dict]:
    """The Secret the jobs read the S3 credentials from; None for an NFS target. Applied
    server-side from a pipe, never through OpenBao (ADR 0009, amendment to ADR 0005)."""
    env = target_env(spec, config_dir)
    if not env:
        return None
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET, "namespace": NS, "labels": dict(MANAGED_BY)},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in sorted(env.items())}}


# -- images ------------------------------------------------------------------------------

def etcd_image(lock: dict) -> str:
    """The etcd image the clusters' control planes run (kubeadm's, pinned in imageDigests):
    the snapshot is taken with that cluster's own etcdctl."""
    refs = [f"{ref}@{digest}" for ref, digest in (lock.get("imageDigests") or {}).items()
            if ref.startswith("registry.k8s.io/etcd:")]
    if len(refs) != 1:
        raise BackupError(f"bundle.lock imageDigests should pin one registry.k8s.io/etcd image, not {len(refs)}")
    return refs[0]


def backup_image(lock: dict) -> str:
    ref = (lock.get("images") or {}).get("backup") or ""
    if "@sha256:" not in ref:
        raise BackupError("bundle.lock images.backup isn't pinned by digest yet: CI builds it from images/backup "
                          "and publishes it on main; pin what it publishes")
    return ref


# -- MGMT's CronJobs -----------------------------------------------------------------------

def _target_mounts(t: dict) -> tuple:
    """(env, volumes, volumeMounts, envFrom) that give a container the site target."""
    env = [{"name": "DCIAB_TARGET_URL", "value": t["url"]}]
    if target_kind(t["url"]) == "nfs":
        m = NFS_URL.match(t["url"])
        env.append({"name": "DCIAB_TARGET_ROOT", "value": "/target"})
        return (env, [{"name": "target", "nfs": {"server": m["server"], "path": m["export"]}}],
                [{"name": "target", "mountPath": "/target"}], [])
    return env, [], [], [{"secretRef": {"name": SECRET}}]


def _security(read_only: bool = True) -> dict:
    # Root, with no capabilities: etcd's client key and the snapshot etcdctl writes are
    # root's, mode 0600, and an NFS export sees the same user as Longhorn's.
    return {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": read_only,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}


def _cronjob(name: str, schedule: dict, pod: dict) -> dict:
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": name, "namespace": NS, "labels": dict(MANAGED_BY)},
        "spec": {
            "schedule": schedule["cron"], "timeZone": "Etc/UTC",
            "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 900,
            "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 1,
            "jobTemplate": {"metadata": {"labels": dict(MANAGED_BY)}, "spec": {
                # A failed run whose upload container never started keeps its plaintext snapshot in
                # the pod's emptyDir until the pod goes: one failed Job, removed after 6 hours.
                "backoffLimit": 1, "activeDeadlineSeconds": 3 * 3600, "ttlSecondsAfterFinished": 6 * 3600,
                "template": {"metadata": {"labels": {**MANAGED_BY, "dciab.io/backup": name}}, "spec": pod},
            }},
        },
    }


def manifests(spec: dict, cluster: str, keys: List[str], lock: dict) -> List[dict]:
    """Namespace, recipients, RBAC and the two CronJobs for MGMT. Needs no StorageClass: the
    snapshot is staged on the node's disk (emptyDir) and goes straight to the target."""
    if cluster != "mgmt":
        raise BackupError(f"{cluster}: DCiaB installs backup jobs on MGMT only so far; INFRA and workload "
                          "clusters come when DCiaB builds them")
    if len(keys) < 3:
        raise BackupError("encrypt to every recipient and the backup key: at least three keys")
    t = target(spec)
    s = schedules(spec)
    etcd, backup = etcd_image(lock), backup_image(lock)
    tenv, tvols, tmounts, envfrom = _target_mounts(t)
    tolerations = [{"key": CONTROL_PLANE, "operator": "Exists", "effect": "NoSchedule"}]
    common_vols = [{"name": "recipients", "configMap": {"name": RECIPIENTS_CM}},
                   {"name": "tmp", "emptyDir": {}}] + tvols
    common_mounts = [{"name": "recipients", "mountPath": "/etc/dciab/backup", "readOnly": True},
                     {"name": "tmp", "mountPath": "/tmp"}] + tmounts

    def env(keep: dict) -> List[dict]:
        return [{"name": "DCIAB_CLUSTER", "value": cluster},
                {"name": "DCIAB_RECIPIENTS", "value": "/etc/dciab/backup/recipients.txt"},
                {"name": "DCIAB_KEEP", "value": json.dumps(keep, sort_keys=True)}] + tenv

    # Only the three files etcdctl needs, never etcd's CA key or its server and peer keys.
    pki_files = ("ca.crt", "healthcheck-client.crt", "healthcheck-client.key")
    certs = [f"--cacert={ETCD_PKI}/ca.crt", f"--cert={ETCD_PKI}/healthcheck-client.crt",
             f"--key={ETCD_PKI}/healthcheck-client.key"]
    etcd_pod = {
        "restartPolicy": "Never",
        # etcd listens on the node's loopback; the job runs on a control plane node.
        "hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet",
        "nodeSelector": {CONTROL_PLANE: ""}, "tolerations": tolerations,
        "automountServiceAccountToken": False,
        "priorityClassName": "system-cluster-critical",
        "initContainers": [
            {"name": "snapshot", "image": etcd,
             "command": ["etcdctl", "--endpoints=https://127.0.0.1:2379", *certs, "snapshot", "save", "/work/etcd.db"],
             "volumeMounts": [{"name": f"etcd-{f.replace('.', '-')}", "mountPath": f"{ETCD_PKI}/{f}", "readOnly": True}
                              for f in pki_files] + [{"name": "work", "mountPath": "/work"}],
             "securityContext": _security()},
        ],
        "containers": [
            {"name": "upload", "image": backup,
             "command": ["python3", RUNNER, "etcd", "--src", "/work/etcd.db"],
             "env": env(s["etcd"]["mgmt"]["keep"]), "envFrom": envfrom,
             "volumeMounts": [{"name": "work", "mountPath": "/work"}] + common_mounts,
             "securityContext": _security()},
        ],
        "volumes": [{"name": f"etcd-{f.replace('.', '-')}", "hostPath": {"path": f"{ETCD_PKI}/{f}", "type": "File"}}
                    for f in pki_files] + [
                    # The node's own disk: MGMT may have no StorageClass at all.
                    {"name": "work", "emptyDir": {"sizeLimit": "10Gi"}}] + common_vols,
    }
    export_pod = {
        "restartPolicy": "Never",
        "serviceAccountName": EXPORT_SA,
        "tolerations": tolerations,
        "containers": [
            {"name": "export", "image": backup,
             "command": ["python3", RUNNER, "capi-export"],
             "env": env(s["capiExport"]["keep"]), "envFrom": envfrom,
             "volumeMounts": common_mounts, "securityContext": _security()},
        ],
        "volumes": common_vols,
    }
    return [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": NS, "labels": {**MANAGED_BY,
                                             # hostPath and hostNetwork, for etcd's client key and its loopback.
                                             "pod-security.kubernetes.io/enforce": "privileged"}}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": RECIPIENTS_CM, "namespace": NS, "labels": dict(MANAGED_BY)},
         "data": {"recipients.txt": "\n".join(keys) + "\n"}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": EXPORT_SA, "namespace": NS, "labels": dict(MANAGED_BY)}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": EXPORT_SA, "labels": dict(MANAGED_BY)},
         # clusterctl move --to-directory reads every Cluster API object and Secret of a
         # namespace, and pauses the Cluster and its ClusterClass while it reads.
         "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list"]},
                   {"apiGroups": ["cluster.x-k8s.io"], "resources": ["clusters", "clusterclasses"],
                    "verbs": ["patch", "update"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": EXPORT_SA, "labels": dict(MANAGED_BY)},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": EXPORT_SA},
         "subjects": [{"kind": "ServiceAccount", "name": EXPORT_SA, "namespace": NS}]},
        _cronjob("etcd-snapshot", s["etcd"]["mgmt"], etcd_pod),
        _cronjob("capi-export", s["capiExport"], export_pod),
    ]


# -- running the runner from the bootstrap host --------------------------------------------

@contextmanager
def mounted(spec: dict) -> Iterator[Optional[str]]:
    """An NFS site target, mounted on this host for the duration (root only); None for S3."""
    t = target(spec)
    if target_kind(t["url"]) != "nfs":
        yield None
        return
    m = NFS_URL.match(t["url"])
    mnt = tempfile.mkdtemp(prefix="dciab-backup-")
    try:
        r = subprocess.run(["mount", "-t", "nfs", f"{m['server']}:{m['export']}", mnt], capture_output=True, text=True)
        if r.returncode != 0:
            raise BackupError(f"can't mount the site target {t['url']} here ({r.stderr.strip()}); "
                              "run as root with the NFS client installed")
        try:
            yield mnt
        finally:
            subprocess.run(["umount", mnt], check=False)
    finally:
        shutil.rmtree(mnt, ignore_errors=True)


def runner_env(spec: dict, config_dir: Path, cluster: str, keys_file: Path, keep: dict,
               root: Optional[str]) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "DCIAB_TARGET", "RCLONE_"))}
    env.update(target_env(spec, config_dir))
    env.update({"DCIAB_CLUSTER": cluster, "DCIAB_TARGET_URL": target(spec)["url"],
                "DCIAB_RECIPIENTS": str(keys_file), "DCIAB_KEEP": json.dumps(keep, sort_keys=True)})
    if root:
        env["DCIAB_TARGET_ROOT"] = root
    return env


def export_now(spec: dict, config_dir: Path, state: Path, kubeconfig: Path, label: str,
               runner_cmd: Optional[List[str]] = None) -> None:
    """A CAPI export from the bootstrap host, kept encrypted in <state>/capi-backup and
    uploaded to the site target. Any failure raises: the pivot doesn't go on without it."""
    keys = recipients(spec, state)
    cmd = runner_cmd or ["python3", str(Path(__file__).with_name("runner.py"))]
    with tempfile.TemporaryDirectory() as tmp, mounted(spec) as root:
        kf = Path(tmp) / "recipients.txt"
        kf.write_text("\n".join(keys) + "\n")
        env = runner_env(spec, config_dir, "mgmt", kf, schedules(spec)["capiExport"]["keep"], root)
        env["DCIAB_KUBECONFIG"] = str(kubeconfig)
        env["DCIAB_QUIET_MINUTES"] = env.get("DCIAB_QUIET_MINUTES", "10")
        work = state / "capi-backup"
        work.mkdir(parents=True, exist_ok=True)
        work.chmod(0o700)
        env["DCIAB_KEEP_LOCAL"] = str(work)   # the encrypted copy, beside the state it came from
        r = subprocess.run([*cmd, "capi-export"], env=env)
        if r.returncode != 0:
            raise BackupError(f"the CAPI export {label} failed (above)")
        (work / "LAST").write_text(f"{stamp()} {label}\n")


def sealed_name(public: str) -> str:
    """The sealed private half's name on the target: it names its public key, so a later run
    can tell whether this key's sealed copy is there."""
    return f"backup-key-{public}.age"


def seal_key_to_target(spec: dict, config_dir: Path, state: Path, attempts: int = 3, sleep=None) -> bool:
    """Make sure the sealed private half of a key DCiaB generated is in dciab/recovery/ on the
    site target: uploads it when it isn't there yet, retrying, and raises if it can't. Safe to
    call on every run. False when there is nothing to upload (the config gives the key)."""
    import time

    sealed = key_dir(state) / "backup-key.age"
    pub = public_key(spec, state)
    given = ((spec.get("backup") or {}).get("key") or {}).get("publicKey")
    if not sealed.is_file() or pub is None or (given and given != (key_dir(state) / "backup-key.pub").read_text().strip()):
        return False
    from .runner import RunnerError, Target
    name = sealed_name(pub)
    last = ""
    for attempt in range(attempts):
        try:
            with mounted(spec) as root:
                env = runner_env(spec, config_dir, "recovery", Path("/dev/null"), {}, root)
                with tempfile.TemporaryDirectory() as tmp:
                    t = Target(env["DCIAB_TARGET_URL"], env.get("DCIAB_TARGET_ROOT"), env, Path(tmp))
                    if name not in t.list("dciab/recovery"):
                        t.put(sealed, f"dciab/recovery/{name}")
                    if name in t.list("dciab/recovery"):
                        return True
                    last = "it isn't listed after the upload"
        except (RunnerError, BackupError, OSError) as exc:
            last = str(exc)
        if attempt + 1 < attempts:
            (sleep or time.sleep)(10)
    raise BackupError(f"the backup key's sealed private half isn't on the site target ({last}); it is only in "
                      f"{sealed}. Fix the target and re-run")


__all__ = ["NS", "SECRET", "backup_image", "etcd_image", "export_now", "generate_key", "manifests",
           "public_key", "recipients", "secret_manifest", "seal_key_to_target", "target_env"]
