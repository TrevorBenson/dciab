#!/usr/bin/env python3
"""Every container image a DCiaB site needs, from what bundle.lock pins.

    python3 build/inventory.py --manifests DIR [--manifests DIR ...] [--extra FILE ...]
                               [--kubeadm-images FILE] [--format text|yaml]

Sources, each recorded next to the image:
- bundle.lock: images (kube-vip) and day0 (CoreDNS, Caddy, chrony base), pinned by digest
- the Cluster API, provider and cert-manager manifests hack/fetch_crds.py downloads
  (rendered with bundle.lock's provider variables, as DCiaB installs them)
- --extra: rendered manifests of other add-ons, such as Cilium
  (addons/cilium/install.sh <values> --template)
- --image: single images, such as the kind node image the bootstrap cluster boots
- --kubeadm-images: the output of `kubeadm config images list --kubernetes-version <v>`
  for the bundle's Kubernetes version: the control plane images nodes pull

The archive build pulls each image by digest into the archive and the site registry
serves them. Images without a digest are listed with "digest: resolve": the build
resolves the tag once and records the digest it pulled.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hack"))

from render_components import substitute, values_from  # noqa: E402

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def images_in(doc) -> Iterable[str]:
    """Every container image a Kubernetes object (or list) references."""
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == "image" and isinstance(value, str) and value:
                yield value
            else:
                yield from images_in(value)
    elif isinstance(doc, list):
        for item in doc:
            yield from images_in(item)


def from_manifest_text(text: str) -> Set[str]:
    out: Set[str] = set()
    for doc in yaml.safe_load_all(text):
        out.update(images_in(doc))
    return out


def normalize(ref: str) -> str:
    """Spell out the registry an unqualified name means: nginx -> docker.io/library/nginx."""
    if "/" not in ref:
        return "docker.io/library/" + ref
    first = ref.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return ref
    return "docker.io/" + ref


def inventory(lock: dict, manifest_dirs: List[Path], extras: List[Path], kubeadm: Optional[Path],
              singles: Optional[List[str]] = None) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {}

    def add(ref: str, source: str) -> None:
        found.setdefault(normalize(ref), set()).add(source)

    for name, ref in (lock.get("images") or {}).items():
        add(ref, f"bundle.lock images.{name}")
    for name, ref in ((lock.get("day0") or {}).get("images") or {}).items():
        add(ref, f"bundle.lock day0.images.{name}")

    values = values_from(lock, {})
    for d in manifest_dirs:
        for f in sorted(d.rglob("*.yaml")):
            rendered, _ = substitute(f.read_text(), values)
            for ref in from_manifest_text(rendered):
                add(ref, f"{f.parent.name}/{f.name}")
    for f in extras:
        for ref in from_manifest_text(f.read_text()):
            add(ref, f.name)
    for item in singles or []:
        ref, _, source = item.partition("=")
        add(ref, source or "--image")
    if kubeadm:
        for line in kubeadm.read_text().split():
            add(line.strip(), f"kubeadm {lock['kubernetes']['version']}")
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifests", action="append", type=Path, default=[], help="a hack/fetch_crds.py output folder")
    p.add_argument("--extra", action="append", type=Path, default=[], help="a rendered add-on manifest")
    p.add_argument("--kubeadm-images", type=Path, help="output of kubeadm config images list")
    p.add_argument("--image", action="append", default=[], metavar="REF[=SOURCE]",
                   help="one more image, e.g. the kind node image the bootstrap cluster boots")
    p.add_argument("--format", choices=["text", "yaml"], default="text")
    args = p.parse_args(argv)

    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    found = inventory(lock, args.manifests, args.extra, args.kubeadm_images, args.image)
    if args.format == "yaml":
        out = [{"image": ref, "digest": "pinned" if DIGEST.search(ref) else "resolve",
                "sources": sorted(src)} for ref, src in sorted(found.items())]
        yaml.safe_dump({"images": out}, sys.stdout, sort_keys=False)
    else:
        for ref, src in sorted(found.items()):
            mark = " " if DIGEST.search(ref) else "*"
            print(f"{mark} {ref}    ({', '.join(sorted(src))})")
        unpinned = sum(1 for ref in found if not DIGEST.search(ref))
        print(f"\n{len(found)} images; {unpinned} marked * have no digest yet (the build resolves them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
