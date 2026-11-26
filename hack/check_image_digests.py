#!/usr/bin/env python3
"""Check that every image in bundle.lock (images, day0.images and the build bases in
BUILD_BASES) is pinned by digest, and that the digest is still what the registry serves
for that tag. The same check covers imageDigests, the digests recorded for images other
manifests name by tag.

    python3 hack/check_image_digests.py

A mismatch means the tag was moved upstream. Needs skopeo and registry access, so it
runs in CI. For an entry without a digest it prints the pinned reference to use.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Base images DCiaB builds its own images from (dotted paths into bundle.lock).
BUILD_BASES = ("pveAssistant.baseImage", "backupImage.base")


def lookup(lock: dict, dotted: str):
    node = lock
    for key in dotted.split("."):
        node = (node or {}).get(key)
    return node


def pinned_images(lock: dict) -> dict:
    """Every image reference the check covers, by name."""
    images = dict(lock["images"])
    images.update({f"day0.{k}": v for k, v in lock.get("day0", {}).get("images", {}).items()})
    images.update({path: lookup(lock, path) for path in BUILD_BASES})
    return images


def registry_digest(ref: str) -> str:
    raw = subprocess.run(["skopeo", "inspect", "--raw", f"docker://{ref}"],
                         check=True, capture_output=True).stdout
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def main() -> int:
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    failed = False
    images = pinned_images(lock)
    for name, ref in images.items():
        if not ref:
            # Published by DCiaB's own CI, then pinned (e.g. images.pveAssistant).
            print(f"{name}: not published yet; pin it once CI has published it")
            continue
        tagged, _, pinned = ref.partition("@")
        actual = registry_digest(tagged)
        if not pinned:
            print(f"{name}: {tagged} is not pinned; use {tagged}@{actual}")
            failed = True
        elif pinned != actual:
            print(f"{name}: {tagged} is pinned to {pinned}, but the registry now serves {actual}. "
                  "The tag moved upstream: find out why before re-pinning.")
            failed = True
        else:
            print(f"{name}: {ref} ok")
    for tagged, pinned in (lock.get("imageDigests") or {}).items():
        actual = registry_digest(tagged)
        if pinned != actual:
            print(f"imageDigests: {tagged} is pinned to {pinned}, but the registry now serves {actual}. "
                  "The tag moved upstream: find out why before re-pinning.")
            failed = True
        else:
            print(f"imageDigests: {tagged}@{pinned} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
