"""The platform credentials Secret a VM cluster's ClusterClass references.

    python3 -m dciab_cluster.credentials <config> <cluster> <namespace>   # Secret YAML on stdout

The classes read <cluster>-proxmox-credentials (url, token, secret) or
<cluster>-vsphere-credentials (username, password). Secret values come from the config's
references ({file: ...} relative to the config's folder, or {env: ...}); pipe the output
straight into kubectl apply -f - so they never land on disk or in a process's arguments.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path as FsPath
from typing import Dict


def resolve(ref, config_dir: FsPath) -> str:
    if isinstance(ref, str):
        return ref
    if "env" in ref:
        value = os.environ.get(ref["env"], "")
        if not value:
            raise KeyError(f"environment variable {ref['env']} is not set")
        return value
    raw = ref["file"]
    path = FsPath(raw) if os.path.isabs(raw) else config_dir / raw
    return path.read_text(encoding="utf-8").strip()


def secret(site: dict, cluster: str, namespace: str, config_dir: FsPath) -> Dict:
    platform = site["spec"]["platform"]
    kind = platform["type"]
    if kind == "proxmox":
        p = platform["proxmox"]
        data = {"url": p["url"], "token": p["credentials"]["tokenID"],
                "secret": resolve(p["credentials"]["tokenSecret"], config_dir)}
    elif kind == "vsphere":
        v = platform["vsphere"]
        data = {"username": v["credentials"]["username"],
                "password": resolve(v["credentials"]["password"], config_dir)}
    else:
        raise ValueError(f"platform {kind} has no credentials Secret")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": f"{cluster}-{kind}-credentials", "namespace": namespace,
                     # clusterctl move takes objects with this label along with the Cluster.
                     "labels": {"app.kubernetes.io/managed-by": "dciab", "clusterctl.cluster.x-k8s.io/move": ""}},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def main(argv=None) -> int:
    import yaml

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    config, cluster, namespace = args
    path = FsPath(config)
    site = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        manifest = secret(site, cluster, namespace, path.resolve().parent)
    except (KeyError, OSError, ValueError) as exc:
        print(f"credentials: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
