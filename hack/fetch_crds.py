#!/usr/bin/env python3
"""Download the component manifests pinned in bundle.lock.

That is CAPI core, bootstrap and control plane, every provider, cert-manager, and the
Bare Metal Operator CRDs. The files land in .cache/crds/<lock>/<name>-<version>/,
where <lock> is a hash of the pinned versions, so a branch with other pins never reads
another branch's files.

hack/check_manifests.py reads them for their CRDs; the e2e test installs them.

    python3 hack/fetch_crds.py            # downloads, then prints the folder
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Iterator, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "crds"


def cache_dir(lock: dict) -> Path:
    pins = {k: lock.get(k) for k in ("clusterAPI", "metal3", "certManager")}
    return CACHE / hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()[:12]


def release_files(lock: dict) -> Iterator[Tuple[str, str, str]]:
    """(folder, file name, URL) for every release asset to fetch."""
    capi = lock["clusterAPI"]
    entries = [("cluster-api", capi["core"])] + list(capi["providers"].items())
    if "certManager" in lock:
        entries.append(("cert-manager", lock["certManager"]))
    for name, entry in entries:
        for component in entry["components"]:
            url = f"https://github.com/{entry['repo']}/releases/download/{entry['version']}/{component}"
            yield f"{name}-{entry['version']}", component, url


def bmo_files(lock: dict) -> Iterator[Tuple[str, str, str]]:
    bmo = lock.get("metal3", {}).get("baremetalOperator")
    if not bmo or "crds" not in bmo:
        return
    base = f"https://raw.githubusercontent.com/{bmo['repo']}/{bmo['version']}"
    kustomization = yaml.safe_load(download(f"{base}/{bmo['crds']}"))
    folder = bmo["crds"].rsplit("/", 1)[0]
    for resource in kustomization["resources"]:
        yield f"baremetal-operator-{bmo['version']}", Path(resource).name, f"{base}/{folder}/{resource}"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def main() -> int:
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    root = cache_dir(lock)
    marker = root / ".complete"
    if marker.is_file():
        print(root)
        return 0
    for folder, name, url in list(release_files(lock)) + list(bmo_files(lock)):
        dest = root / folder / name
        if dest.is_file() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {url}", file=sys.stderr)
        # Write to a temporary name and rename, so an interrupted download never
        # leaves a truncated file that later runs would take as complete.
        partial = dest.with_suffix(dest.suffix + ".partial")
        partial.write_bytes(download(url))
        partial.replace(dest)
    marker.write_text("")
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
