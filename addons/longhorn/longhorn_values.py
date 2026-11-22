#!/usr/bin/env python3
"""Longhorn's per-site values, backup credentials Secret and image list, from the site config.

    longhorn_values.py values <config>                 # Helm values from spec.storage.longhorn
    longhorn_values.py secret <config> <namespace>     # backup credentials Secret YAML (S3 only)
    longhorn_values.py images <chart values> <values>  # every image the release runs, one per line

The Secret goes to stdout so install.sh can pipe it into kubectl apply -f -: credentials never
land on disk or in a process's arguments. `secret` prints nothing for an NFS target.

backupTarget is DCiaB's form: s3://<bucket>@<endpoint>[/<path>] or nfs://<server>:/<export>.
Longhorn wants s3://<bucket>@<region>/<path> and the endpoint in the Secret's AWS_ENDPOINTS,
so an S3 target is translated. backupCredentials ({file: ...} or {env: ...}) holds KEY=VALUE
lines with at least AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path
from typing import Dict, List

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from dciab_cluster.credentials import resolve  # noqa: E402

SECRET_NAME = "longhorn-backup-credentials"
# Longhorn's S3 URL needs a region; S3-compatible endpoints (SeaweedFS, MinIO) ignore it.
S3_REGION = "us-east-1"
S3_TARGET = re.compile(r"^s3://(?P<bucket>[^@/]+)@(?P<endpoint>[^/]+)(?P<path>/.*)?$")
REQUIRED_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def longhorn(site: dict) -> dict:
    return site["spec"]["storage"]["longhorn"]


def backup_target(target: str) -> str:
    """DCiaB's backupTarget in the form Longhorn reads."""
    m = S3_TARGET.match(target)
    if target.startswith("s3://"):
        if not m:
            raise ValueError(f"backupTarget {target!r}: use s3://<bucket>@<endpoint>[/<path>]")
        return f"s3://{m['bucket']}@{S3_REGION}{m['path'] or '/'}"
    if target.startswith("nfs://"):
        return target
    raise ValueError(f"backupTarget {target!r}: use s3:// or nfs://")


def site_values(site: dict) -> dict:
    lh = longhorn(site)
    count = lh["replicaCount"]
    store = {"backupTarget": backup_target(lh["backupTarget"])}
    if lh["backupTarget"].startswith("s3://"):
        store["backupTargetCredentialSecret"] = SECRET_NAME
    return {
        "persistence": {"defaultClassReplicaCount": count},
        "defaultSettings": {"defaultReplicaCount": count},
        "defaultBackupStore": store,
    }


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


def secret(site: dict, namespace: str, config_dir: Path):
    """The backup credentials Secret, or None for an NFS target."""
    lh = longhorn(site)
    m = S3_TARGET.match(lh["backupTarget"])
    if not lh["backupTarget"].startswith("s3://"):
        return None
    if not m:
        raise ValueError(f"backupTarget {lh['backupTarget']!r}: use s3://<bucket>@<endpoint>[/<path>]")
    if "backupCredentials" not in lh:
        raise ValueError("an s3:// backupTarget needs backupCredentials")
    data = parse_env(resolve(lh["backupCredentials"], config_dir))
    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"backupCredentials has no {', '.join(missing)}")
    data.setdefault("AWS_ENDPOINTS", f"https://{m['endpoint']}")
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
        if len(argv) == 2 and argv[0] == "values":
            yaml.safe_dump(site_values(yaml.safe_load(open(argv[1]))), sys.stdout, sort_keys=False)
        elif len(argv) == 3 and argv[0] == "secret":
            path = Path(argv[1]).resolve()
            manifest = secret(yaml.safe_load(path.read_text()), argv[2], path.parent)
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
    except (KeyError, ValueError, OSError) as exc:
        print(f"longhorn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
