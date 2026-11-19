#!/usr/bin/env python3
"""Download the CAPI and provider component manifests pinned in bundle.lock.

The files land in .cache/crds/<lock>/<provider>-<version>/, where <lock> is a hash of
the pinned versions, so a branch with other pins never reads another branch's CRDs.
They are only read for their CustomResourceDefinitions.

    python3 hack/fetch_crds.py            # downloads, then prints the folder to check against
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "crds"


def cache_dir(lock: dict) -> Path:
    pins = json.dumps(lock["clusterAPI"], sort_keys=True).encode()
    return CACHE / hashlib.sha256(pins).hexdigest()[:12]


def entries(lock: dict):
    capi = lock["clusterAPI"]
    yield "cluster-api", capi["core"]
    for name, entry in capi["providers"].items():
        yield name, entry


def main() -> int:
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    root = cache_dir(lock)
    for name, entry in entries(lock):
        target = root / f"{name}-{entry['version']}"
        target.mkdir(parents=True, exist_ok=True)
        for component in entry["components"]:
            dest = target / component
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            url = f"https://github.com/{entry['repo']}/releases/download/{entry['version']}/{component}"
            print(f"fetching {url}", file=sys.stderr)
            # Write to a temporary name and rename, so an interrupted download never
            # leaves a truncated file that later runs would take as complete.
            partial = dest.with_suffix(dest.suffix + ".partial")
            with urllib.request.urlopen(url, timeout=120) as resp:
                partial.write_bytes(resp.read())
            partial.replace(dest)
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
