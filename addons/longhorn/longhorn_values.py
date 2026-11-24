#!/usr/bin/env python3
"""Longhorn's per-site values, backup credentials Secret and image list, from the site config.

    longhorn_values.py values <config> [<cluster>]               # Helm values from the cluster's Longhorn settings
    longhorn_values.py secret <config> <namespace> [<cluster>]   # backup credentials Secret YAML (S3 only)
    longhorn_values.py images <chart values> <values>  # every image the release runs, one per line

The Longhorn settings are the cluster's tier block (lib/dciab_storage): for INFRA,
spec.storage.block with overrides.infra, or the old spec.storage.longhorn; for MGMT, only
overrides.mgmt. <cluster> defaults to mgmt. The backup target is the cluster's from
spec.backup (lib/dciab_backup): for MGMT, always the site target. A config without spec.backup still uses the Longhorn settings' backupTarget.

The Secret goes to stdout so install.sh can pipe it into kubectl apply --server-side -f -:
credentials never land on disk, in a process's arguments, or in a last-applied annotation. `secret` prints nothing for an NFS target.

backupTarget is DCiaB's form: s3://<bucket>@<endpoint>[/<path>] or nfs://<server>:/<export>.
Longhorn wants s3://<bucket>@<region>/<path> and the endpoint in the Secret's AWS_ENDPOINTS,
so an S3 target is translated (the region is read from an AWS endpoint, s3.<region>.amazonaws.com,
and is otherwise us-east-1, which S3-compatible stores ignore). backupCredentials ({file: ...} or {env: ...}) holds KEY=VALUE
lines with at least AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. The target's caBundle, if
any, becomes the Secret's AWS_CERT, which Longhorn trusts for the endpoint.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path
from typing import Dict, List

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from dciab_backup import BackupError, longhorn_target  # noqa: E402
from dciab_cluster.credentials import resolve  # noqa: E402
from dciab_storage import StorageError, cluster_block  # noqa: E402

SECRET_NAME = "longhorn-backup-credentials"
# Longhorn's S3 URL needs a region; S3-compatible endpoints (SeaweedFS, MinIO) ignore it.
S3_REGION = "us-east-1"
AWS_ENDPOINT = re.compile(r"^s3[.-](?P<region>[a-z0-9-]+)\.amazonaws\.com(:\d+)?$")
CONTROL_PLANE_TAINT = "node-role.kubernetes.io/control-plane"
S3_TARGET = re.compile(r"^s3://(?P<bucket>[^@/]+)@(?P<endpoint>[^/]+)(?P<path>/.*)?$")
REQUIRED_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def longhorn(site: dict, cluster: str = "mgmt") -> dict:
    block = cluster_block(site["spec"], cluster)
    if block is None:
        raise ValueError(f"cluster {cluster} has no block storage from DCiaB (MGMT and workload clusters get it "
                         "only through spec.storage.block.overrides.mgmt or overrides.workload)")
    if block["provider"] != "longhorn":
        raise ValueError(f"cluster {cluster} uses {block['provider']} for block storage, not longhorn")
    return block["settings"]


def backup_target(target: str) -> str:
    """DCiaB's backupTarget in the form Longhorn reads."""
    m = S3_TARGET.match(target)
    if target.startswith("s3://"):
        if not m:
            raise ValueError(f"backupTarget {target!r}: use s3://<bucket>@<endpoint>[/<path>]")
        aws = AWS_ENDPOINT.match(m["endpoint"])
        region = aws["region"] if aws else S3_REGION
        return f"s3://{m['bucket']}@{region}{m['path'] or '/'}"
    if target.startswith("nfs://"):
        return target
    raise ValueError(f"backupTarget {target!r}: use s3:// or nfs://")


def has_workers(site: dict, cluster: str) -> bool:
    pools = site["spec"]["clusters"].get(cluster, {}).get("workerPools")
    if pools is None:
        return True   # bare-metal or unknown layout: leave scheduling to the cluster
    return any(p.get("replicas", 0) > 0 for p in pools)


def site_values(site: dict, cluster: str = "mgmt") -> dict:
    lh = longhorn(site, cluster)
    count = lh["replicaCount"]
    target = longhorn_target(site["spec"], cluster)["url"]
    store = {"backupTarget": backup_target(target)}
    if target.startswith("s3://"):
        store["backupTargetCredentialSecret"] = SECRET_NAME
    values = {
        "persistence": {"defaultClassReplicaCount": count},
        "defaultSettings": {"defaultReplicaCount": count},
        "defaultBackupStore": store,
    }
    if not has_workers(site, cluster):
        # No worker pools: the control plane nodes run workloads, so Longhorn runs there too.
        tol = [{"key": CONTROL_PLANE_TAINT, "operator": "Exists", "effect": "NoSchedule"}]
        values["defaultSettings"]["taintToleration"] = f"{CONTROL_PLANE_TAINT}:NoSchedule"
        for part in ("longhornManager", "longhornDriver", "longhornUI"):
            values[part] = {"tolerations": tol}
    return values


def parse_env(text: str) -> Dict[str, str]:
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
            raise ValueError(f"backupCredentials line {n}: expected KEY=VALUE")
        out[key] = value.strip().strip("'\"")
    return out


def secret(site: dict, namespace: str, config_dir: Path, cluster: str = "mgmt"):
    """The backup credentials Secret, or None for an NFS target."""
    longhorn(site, cluster)
    target = longhorn_target(site["spec"], cluster)
    url = target["url"]
    m = S3_TARGET.match(url)
    if not url.startswith("s3://"):
        return None
    if not m:
        raise ValueError(f"backup target {url!r}: use s3://<bucket>@<endpoint>[/<path>]")
    if "credentials" not in target:
        raise ValueError("an s3:// backup target needs credentials")
    data = parse_env(resolve(target["credentials"], config_dir))
    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"the backup credentials have no {', '.join(missing)}")
    data.setdefault("AWS_ENDPOINTS", f"https://{m['endpoint']}")
    if "caBundle" in target:
        data["AWS_CERT"] = resolve(target["caBundle"], config_dir)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": SECRET_NAME, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "dciab"}},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in sorted(data.items())},
    }


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def images(values: dict) -> List[str]:
    """Every repository:tag under the chart's image: tree. The manager starts most of these
    (engine, instance manager, CSI sidecars, ...) itself, so they never appear as image: in
    the rendered manifests."""
    out: List[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("repository"), str) and node.get("tag"):
                out.append(f"{node['repository']}:{node['tag']}")
            for v in node.values():
                walk(v)

    walk((values or {}).get("image") or {})
    registry = ((values or {}).get("global") or {}).get("imageRegistry") or ""
    if registry:
        out = [f"{registry.rstrip('/')}/{ref}" for ref in out]
    return sorted(set(out))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) in (2, 3) and argv[0] == "values":
            cluster = argv[2] if len(argv) == 3 else "mgmt"
            yaml.safe_dump(site_values(yaml.safe_load(open(argv[1])), cluster), sys.stdout, sort_keys=False)
        elif len(argv) in (3, 4) and argv[0] == "secret":
            path = Path(argv[1]).resolve()
            cluster = argv[3] if len(argv) == 4 else "mgmt"
            manifest = secret(yaml.safe_load(path.read_text()), argv[2], path.parent, cluster)
            if manifest:
                yaml.safe_dump(manifest, sys.stdout, sort_keys=False)
        elif len(argv) >= 2 and argv[0] == "images":
            values: dict = {}
            for f in argv[1:]:
                values = merge(values, yaml.safe_load(open(f)) or {})
            print("\n".join(images(values)))
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except (KeyError, ValueError, OSError, StorageError, BackupError) as exc:
        print(f"longhorn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
