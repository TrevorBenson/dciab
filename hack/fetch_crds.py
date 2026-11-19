#!/usr/bin/env python3
"""Download the component manifests pinned in bundle.lock, and verify their checksums.

That is CAPI core, bootstrap and control plane, every provider, cert-manager, and the
Bare Metal Operator CRDs. The files land in .cache/crds/<lock>/<name>-<version>/,
where <lock> is a hash of the pins and checksums, so a branch with other pins never
reads another branch's files.

Every file must match the sha256 recorded under `checksums` in bundle.lock; a file
with a different digest, or none recorded, is refused. After changing a version, run
with --print-checksums, review the output, and paste it into bundle.lock.

hack/check_manifests.py reads the files for their CRDs; the e2e test installs them.

    python3 hack/fetch_crds.py                    # downloads, verifies, prints the folder
    python3 hack/fetch_crds.py --print-checksums  # prints entries for bundle.lock
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, Iterator, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "crds"
GITHUB = "https://github.com/"
RAW = "https://raw.githubusercontent.com/"


class ChecksumError(Exception):
    pass


def cache_dir(lock: dict) -> Path:
    pins = {k: lock.get(k) for k in ("clusterAPI", "metal3", "certManager", "checksums")}
    return CACHE / hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()[:12]


def checksum_key(url: str) -> str:
    """repo/version/path, the same for release assets and raw files."""
    if url.startswith(RAW):
        return url[len(RAW):]
    return url[len(GITHUB):].replace("/releases/download/", "/", 1)


def release_files(lock: dict) -> Iterator[Tuple[str, str, str]]:
    """(folder, file name, URL) for every release asset to fetch."""
    capi = lock["clusterAPI"]
    entries = [("cluster-api", capi["core"])] + list(capi["providers"].items())
    if "certManager" in lock:
        entries.append(("cert-manager", lock["certManager"]))
    for name, entry in entries:
        for component in entry["components"]:
            yield f"{name}-{entry['version']}", component, \
                f"{GITHUB}{entry['repo']}/releases/download/{entry['version']}/{component}"


def bmo_files(lock: dict, fetch) -> Iterator[Tuple[str, str, str]]:
    bmo = lock.get("metal3", {}).get("baremetalOperator")
    if not bmo or "crds" not in bmo:
        return
    base = f"{RAW}{bmo['repo']}/{bmo['version']}"
    kustomization = yaml.safe_load(fetch(f"{base}/{bmo['crds']}"))
    folder = bmo["crds"].rsplit("/", 1)[0]
    for resource in kustomization["resources"]:
        yield f"baremetal-operator-{bmo['version']}", Path(resource).name, f"{base}/{folder}/{resource}"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def verified(url: str, data: bytes, checksums: Dict[str, str]) -> bytes:
    key = checksum_key(url)
    actual = hashlib.sha256(data).hexdigest()
    pinned = checksums.get(key)
    if pinned is None:
        raise ChecksumError(f"no checksum pinned for {key} (sha256 {actual}). Review the file, then "
                            "add it under checksums in bundle.lock (--print-checksums lists them all).")
    if pinned != actual:
        raise ChecksumError(f"{key}: sha256 is {actual}, but bundle.lock pins {pinned}. The upstream file "
                            "changed: find out why before re-pinning.")
    return data


def write_atomic(dest: Path, data: bytes) -> None:
    """A unique temporary file per process, then a rename: never a truncated or shared file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def print_checksums(lock: dict) -> int:
    print("checksums:")
    files = list(release_files(lock))
    seen = {}

    def fetch(url: str) -> bytes:
        data = download(url)
        seen[checksum_key(url)] = hashlib.sha256(data).hexdigest()
        return data

    files += list(bmo_files(lock, fetch))
    for _, _, url in files:
        if checksum_key(url) not in seen:
            fetch(url)
    for key in sorted(seen):
        print(f"  {key}: {seen[key]}")
    return 0


def main(argv) -> int:
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    if "--print-checksums" in argv:
        return print_checksums(lock)
    checksums = lock.get("checksums", {})
    root = cache_dir(lock)
    marker = root / ".complete"
    if marker.is_file():
        print(root)
        return 0
    try:
        files = list(release_files(lock)) + list(bmo_files(lock, lambda u: verified(u, download(u), checksums)))
        for folder, name, url in files:
            dest = root / folder / name
            if dest.is_file():
                verified(url, dest.read_bytes(), checksums)
                continue
            print(f"fetching {url}", file=sys.stderr)
            write_atomic(dest, verified(url, download(url), checksums))
    except ChecksumError as exc:
        print(f"fetch_crds: {exc}", file=sys.stderr)
        return 1
    marker.write_text("")
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
