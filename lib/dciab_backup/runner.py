#!/usr/bin/env python3
"""Encrypt, upload and prune backups: the code DCiaB's backup jobs run (ADR 0009).

Standard library only: the same file runs in the backup image (images/backup, as
/usr/libexec/dciab/backup-runner) and on the bootstrap host (dciab mgmt's CAPI exports
around the pivot). It needs age, and rclone for an S3 target.

    backup-runner etcd --src FILE           check an etcd snapshot (etcdutl), encrypt and upload it;
                                            FILE is deleted whatever happens
    backup-runner capi-export               clusterctl move --to-directory of every namespace that
                                            holds a Cluster, then encrypt and upload it as one tar
    backup-runner store --kind KIND --file FILE [--keep-local DIR]
                                            encrypt and upload any file

Everything else comes from the environment:

    DCIAB_CLUSTER       the cluster the backup is of (mgmt)
    DCIAB_TARGET_URL    s3://<bucket>@<endpoint>[/<path>] or nfs://<server>:/<export>
    DCIAB_TARGET_ROOT   NFS: where the export is mounted (the pod's nfs volume, or a mount on the host)
    DCIAB_RECIPIENTS    a file of age public keys, one per line: the recipients and the backup key
    DCIAB_KEEP          JSON retention, e.g. {"hourly": 24, "daily": 14} (default: keep everything)
    DCIAB_KUBECONFIG    capi-export: the kubeconfig (default: one written from the pod's service account)
    DCIAB_QUIET_MINUTES capi-export: how long to wait for the clusters to be quiet (default 30)
    DCIAB_KEEP_LOCAL    capi-export: also keep the encrypted export in this folder
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINTS, AWS_CERT
                        S3: the site target's credentials, as Longhorn's Secret holds them

Files go to dciab/<cluster>/<kind>/<kind>-<cluster>-<UTC time>.<ext>.age on the target. Only the
encrypted file leaves the machine; a plaintext snapshot or export is deleted once encrypted.

Retention (DCIAB_KEEP): "hourly: N" keeps the newest N, "daily: N" the newest of each of the
N newest days that have a copy, "days: N" every copy from the N days before the newest one.
A copy any rule keeps is kept. Counting from the newest copy, not from now, means an outage
never prunes the last good copies. Bucket lifecycle rules are never relied on.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

STAMP = "%Y%m%dT%H%M%SZ"
NAME = re.compile(r"^(?P<kind>[a-z]+)-(?P<cluster>[a-z0-9-]+?)-(?P<stamp>\d{8}T\d{6}Z)(?P<ext>\.[a-z.]+)\.age$")
S3_URL = re.compile(r"^s3://(?P<bucket>[^@/]+)@(?P<endpoint>[^/]+)(?P<path>/.*)?$")
NFS_URL = re.compile(r"^nfs://(?P<server>[^:/]+):(?P<export>/\S*)$")
AWS_ENDPOINT = re.compile(r"^s3[.-](?P<region>[a-z0-9-]+)\.amazonaws\.com(:\d+)?$")
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PAUSED_ANNOTATION = "cluster.x-k8s.io/paused"


class RunnerError(Exception):
    pass


def log(msg: str) -> None:
    print(f"backup: {msg}", flush=True)


def run(cmd: List[str], env: Optional[Dict[str, str]] = None, capture: bool = False) -> str:
    r = subprocess.run(cmd, env=env, text=True, capture_output=capture)
    if r.returncode != 0:
        detail = (r.stderr or "").strip() if capture else ""
        raise RunnerError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({r.returncode}){': ' + detail if detail else ''}")
    return r.stdout if capture else ""


def stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(STAMP)


def parse_env(text: str) -> Dict[str, str]:
    """KEY=VALUE lines (comments, blank lines and 'export ' allowed), as the config's S3
    credentials files hold them."""
    out: Dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise RunnerError(f"credentials line {n}: expected KEY=VALUE")
        out[key] = value.strip().strip("'\"")
    return out


# -- retention ----------------------------------------------------------------------------

def to_keep(names: Iterable[str], keep: Dict[str, int]) -> Set[str]:
    """The names a retention keeps. Names that aren't DCiaB backups are always kept."""
    dated = []
    out: Set[str] = set()
    for n in names:
        m = NAME.match(n)
        if not m:
            out.add(n)
            continue
        dated.append((datetime.strptime(m["stamp"], STAMP).replace(tzinfo=timezone.utc), n))
    if not keep:
        return out | {n for _, n in dated}
    dated.sort(reverse=True)
    out.update(n for _, n in dated[:keep.get("hourly", 0)])
    if keep.get("daily"):
        days: List[str] = []
        for when, n in dated:
            day = when.strftime("%Y%m%d")
            if day not in days:
                if len(days) == keep["daily"]:
                    break
                days.append(day)
                out.add(n)
    if keep.get("days") and dated:
        cutoff = dated[0][0] - timedelta(days=keep["days"])
        out.update(n for when, n in dated if when > cutoff)
    return out


# -- targets ------------------------------------------------------------------------------

class Target:
    """Where backups go: a local folder (an NFS export, mounted) or an S3 bucket, through rclone."""

    def __init__(self, url: str, root: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                 workdir: Optional[Path] = None):
        self.url = url
        env = dict(os.environ if env is None else env)
        if url.startswith("nfs://"):
            if not NFS_URL.match(url):
                raise RunnerError(f"{url}: use nfs://<server>:/<export>")
            if not root or not Path(root).is_dir():
                raise RunnerError(f"{url}: the export isn't mounted (DCIAB_TARGET_ROOT={root or 'unset'})")
            self.local: Optional[Path] = Path(root)
            self.env = env
            self.flags: List[str] = []
            return
        m = S3_URL.match(url)
        if not m:
            raise RunnerError(f"{url}: use s3://<bucket>@<endpoint>[/<path>] or nfs://<server>:/<export>")
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not env.get(k):
                raise RunnerError(f"{url}: no {k} (the site target's credentials)")
        self.local = None
        aws = AWS_ENDPOINT.match(m["endpoint"])
        endpoint = env.get("AWS_ENDPOINTS") or f"https://{m['endpoint']}"
        self.prefix = f"dciab:{m['bucket']}{(m['path'] or '').rstrip('/')}"
        self.env = {**env,
                    "RCLONE_CONFIG_DCIAB_TYPE": "s3",
                    "RCLONE_CONFIG_DCIAB_PROVIDER": "AWS" if aws else "Other",
                    "RCLONE_CONFIG_DCIAB_ACCESS_KEY_ID": env["AWS_ACCESS_KEY_ID"],
                    "RCLONE_CONFIG_DCIAB_SECRET_ACCESS_KEY": env["AWS_SECRET_ACCESS_KEY"],
                    "RCLONE_CONFIG_DCIAB_ENDPOINT": endpoint,
                    "RCLONE_CONFIG_DCIAB_REGION": aws["region"] if aws else "us-east-1",
                    # The site's bucket exists; DCiaB's credentials may not be allowed to create one.
                    "RCLONE_CONFIG_DCIAB_NO_CHECK_BUCKET": "true"}
        self.flags = []
        if env.get("AWS_CERT"):
            ca = (workdir or Path(tempfile.mkdtemp())) / "target-ca.pem"
            ca.write_text(env["AWS_CERT"] + "\n")
            self.flags = ["--ca-cert", str(ca)]

    def _remote(self, rel: str) -> str:
        return f"{self.prefix}/{rel}"

    def put(self, src: Path, rel: str) -> None:
        if self.local is not None:
            dest = self.local / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name("." + dest.name + ".part")
            shutil.copyfile(src, tmp)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            return
        run(["rclone", "copyto", *self.flags, str(src), self._remote(rel)], env=self.env)

    def list(self, rel_dir: str) -> List[str]:
        if self.local is not None:
            d = self.local / rel_dir
            return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []
        out = run(["rclone", "lsf", "--files-only", *self.flags, self._remote(rel_dir)], env=self.env, capture=True)
        return sorted(line for line in out.splitlines() if line)

    def delete(self, rel: str) -> None:
        if self.local is not None:
            (self.local / rel).unlink(missing_ok=True)
            return
        run(["rclone", "deletefile", *self.flags, self._remote(rel)], env=self.env)


def target_from_env(env: Optional[Dict[str, str]] = None, workdir: Optional[Path] = None) -> Target:
    env = dict(os.environ if env is None else env)
    url = env.get("DCIAB_TARGET_URL") or ""
    if not url:
        raise RunnerError("DCIAB_TARGET_URL is not set")
    return Target(url, env.get("DCIAB_TARGET_ROOT"), env, workdir)


# -- storing ------------------------------------------------------------------------------

def encrypt(src: Path, dest: Path, recipients: Path) -> None:
    keys = [line.strip() for line in recipients.read_text().splitlines() if line.strip().startswith("age1")]
    if len(keys) < 2:
        raise RunnerError(f"{recipients}: fewer than two age recipients; refusing to encrypt to fewer")
    run(["age", "--encrypt", "-R", str(recipients), "-o", str(dest), str(src)])


def store(kind: str, cluster: str, src: Path, ext: str, target: Target, recipients: Path,
          keep: Dict[str, int], keep_local: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """Encrypt src, upload it, then prune the target by keep. Returns the uploaded path."""
    name = f"{kind}-{cluster}-{stamp(now)}{ext}.age"
    rel_dir = f"dciab/{cluster}/{kind}"
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / name
        encrypt(src, enc, recipients)
        if keep_local is not None:
            keep_local.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(enc, keep_local / name)
        target.put(enc, f"{rel_dir}/{name}")
    log(f"uploaded {rel_dir}/{name} to {target.url}")
    names = target.list(rel_dir)
    if name not in names:
        raise RunnerError(f"{rel_dir}/{name} isn't on the target after the upload")
    kept = to_keep(names, keep)
    for old in sorted(set(names) - kept):
        target.delete(f"{rel_dir}/{old}")
        log(f"pruned {rel_dir}/{old}")
    return f"{rel_dir}/{name}"


# -- CAPI exports -------------------------------------------------------------------------

def service_account_kubeconfig(path: Path) -> Path:
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host or not (SA / "token").is_file():
        raise RunnerError("not in a pod: set DCIAB_KUBECONFIG")
    server = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
    path.write_text(json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "sa",
        "clusters": [{"name": "self", "cluster": {"server": server, "certificate-authority": str(SA / "ca.crt")}}],
        "users": [{"name": "sa", "user": {"tokenFile": str(SA / "token")}}],
        "contexts": [{"name": "sa", "context": {"cluster": "self", "user": "sa"}}],
    }))
    path.chmod(0o600)
    return path


def kubectl_json(kubeconfig: Path, *args: str) -> dict:
    return json.loads(run(["kubectl", "--kubeconfig", str(kubeconfig), *args, "-o", "json"], capture=True))


def busy(kubeconfig: Path) -> List[str]:
    """Why the clusters aren't quiet: a Machine that isn't Running, or one being deleted. clusterctl
    move expects stable clusters (ADR 0009, decision 3)."""
    out = []
    for m in kubectl_json(kubeconfig, "get", "machines.cluster.x-k8s.io", "-A").get("items", []):
        meta, phase = m["metadata"], (m.get("status") or {}).get("phase")
        if meta.get("deletionTimestamp") or phase != "Running":
            out.append(f"Machine {meta['namespace']}/{meta['name']} is {'deleting' if meta.get('deletionTimestamp') else phase or 'new'}")
    return out


def paused(kubeconfig: Path) -> Set[str]:
    out = set()
    for c in kubectl_json(kubeconfig, "get", "clusters.cluster.x-k8s.io", "-A").get("items", []):
        meta = c["metadata"]
        if (c.get("spec") or {}).get("paused") or PAUSED_ANNOTATION in (meta.get("annotations") or {}):
            out.add(f"{meta['namespace']}/{meta['name']}")
    return out


def paused_classes(kubeconfig: Path) -> Set[str]:
    out = set()
    for c in kubectl_json(kubeconfig, "get", "clusterclasses.cluster.x-k8s.io", "-A").get("items", []):
        meta = c["metadata"]
        if PAUSED_ANNOTATION in (meta.get("annotations") or {}):
            out.add(f"{meta['namespace']}/{meta['name']}")
    return out


def cluster_namespaces(kubeconfig: Path) -> List[str]:
    items = kubectl_json(kubeconfig, "get", "clusters.cluster.x-k8s.io", "-A").get("items", [])
    return sorted({c["metadata"]["namespace"] for c in items})


def capi_export(kubeconfig: Path, workdir: Path, quiet_minutes: int = 30, sleep=time.sleep) -> Path:
    """Export every namespace that holds a Cluster to one tar in workdir. Waits for the
    clusters to be quiet. clusterctl pauses each Cluster and its ClusterClass while it reads,
    and resumes them only on success: anything the export left paused is resumed here, and
    the export then fails so that someone looks."""
    deadline = time.monotonic() + quiet_minutes * 60
    while True:
        why = busy(kubeconfig)
        if not why:
            break
        if time.monotonic() >= deadline:
            raise RunnerError(f"the clusters weren't quiet for {quiet_minutes} minutes, so nothing was exported: "
                              + "; ".join(why[:5]))
        log("waiting for the clusters to be quiet: " + "; ".join(why[:3]))
        sleep(60)
    namespaces = cluster_namespaces(kubeconfig)
    if not namespaces:
        raise RunnerError("no Cluster to export")
    before, classes_before = paused(kubeconfig), paused_classes(kubeconfig)
    export = workdir / "capi"
    shutil.rmtree(export, ignore_errors=True)
    try:
        for ns in namespaces:
            (export / ns).mkdir(parents=True)
            run(["clusterctl", "move", "--kubeconfig", str(kubeconfig), "--namespace", ns,
                 "--to-directory", str(export / ns)])
            log(f"exported namespace {ns}")
    finally:
        left_classes = paused_classes(kubeconfig) - classes_before
        for ref in sorted(left_classes):
            ns, name = ref.split("/", 1)
            log(f"ClusterClass {ref} was left paused by the export; resuming it")
            run(["kubectl", "--kubeconfig", str(kubeconfig), "-n", ns, "annotate", "clusterclasses.cluster.x-k8s.io",
                 name, f"{PAUSED_ANNOTATION}-"])
        left = paused(kubeconfig) - before
        for ref in sorted(left):
            ns, name = ref.split("/", 1)
            log(f"Cluster {ref} was left paused by the export; resuming it")
            run(["kubectl", "--kubeconfig", str(kubeconfig), "-n", ns, "patch", "clusters.cluster.x-k8s.io", name,
                 "--type", "merge", "-p", '{"spec":{"paused":false}}'])
            run(["kubectl", "--kubeconfig", str(kubeconfig), "-n", ns, "annotate", "clusters.cluster.x-k8s.io",
                 name, f"{PAUSED_ANNOTATION}-"])
    if left or left_classes:
        what = [f"Cluster {r}" for r in sorted(left)] + [f"ClusterClass {r}" for r in sorted(left_classes)]
        raise RunnerError(f"the export left {', '.join(what)} paused (resumed now); check them, and the export")
    tar = workdir / "capi.tar"
    with tarfile.open(tar, "w") as tf:
        tf.add(export, arcname="capi")
    shutil.rmtree(export)
    return tar


# -- main ---------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="backup-runner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("etcd")
    e.add_argument("--src", required=True)
    sub.add_parser("capi-export")
    s = sub.add_parser("store")
    s.add_argument("--kind", required=True, choices=["etcd", "capi"])
    s.add_argument("--file", required=True)
    s.add_argument("--ext", default="")
    s.add_argument("--keep-local")
    args = ap.parse_args(argv)
    plaintext = Path(args.src) if args.cmd == "etcd" else None
    try:
        cluster = os.environ.get("DCIAB_CLUSTER") or ""
        if not re.fullmatch(r"[a-z0-9-]+", cluster):
            raise RunnerError("DCIAB_CLUSTER is not set")
        recipients = Path(os.environ.get("DCIAB_RECIPIENTS") or "/etc/dciab/backup/recipients.txt")
        keep = json.loads(os.environ.get("DCIAB_KEEP") or "{}")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            target = target_from_env(workdir=work)
            if plaintext is not None:
                if not plaintext.is_file():
                    raise RunnerError(f"no snapshot at {plaintext}")
                # The snapshot must be whole before it's kept: etcdutl reads its hash and revision.
                print(run(["etcdutl", "snapshot", "status", str(plaintext), "--write-out=table"], capture=True), end="")
                store("etcd", cluster, plaintext, ".db", target, recipients, keep)
            elif args.cmd == "capi-export":
                kc = os.environ.get("DCIAB_KUBECONFIG")
                kubeconfig = Path(kc) if kc else service_account_kubeconfig(work / "kubeconfig")
                tar = capi_export(kubeconfig, work, int(os.environ.get("DCIAB_QUIET_MINUTES") or 30))
                local = os.environ.get("DCIAB_KEEP_LOCAL")
                store("capi", cluster, tar, ".tar", target, recipients, keep, Path(local) if local else None)
            else:
                store(args.kind, cluster, Path(args.file), args.ext, target, recipients, keep,
                      Path(args.keep_local) if args.keep_local else None)
    except (RunnerError, OSError, ValueError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    finally:
        if plaintext is not None:
            plaintext.unlink(missing_ok=True)   # the plaintext snapshot never outlives the run
    return 0


if __name__ == "__main__":
    sys.exit(main())
