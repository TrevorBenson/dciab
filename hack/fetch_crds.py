#!/usr/bin/env python3
"""Download the CAPI and provider component manifests pinned in bundle.lock.

The files land in .cache/crds/<provider>-<version>/ and are only read for their
CustomResourceDefinitions, which hack/check_manifests.py validates against.

    python3 hack/fetch_crds.py            # prints the cache directory
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "crds"


def entries(lock: dict):
    capi = lock["clusterAPI"]
    yield "cluster-api", capi["core"]
    for name, entry in capi["providers"].items():
        yield name, entry


def main() -> int:
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    for name, entry in entries(lock):
        target = CACHE / f"{name}-{entry['version']}"
        target.mkdir(parents=True, exist_ok=True)
        for component in entry["components"]:
            dest = target / component
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            url = f"https://github.com/{entry['repo']}/releases/download/{entry['version']}/{component}"
            print(f"fetching {url}", file=sys.stderr)
            with urllib.request.urlopen(url, timeout=120) as resp:
                dest.write_bytes(resp.read())
    print(CACHE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
