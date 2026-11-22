#!/usr/bin/env python3
"""Build the DCiaB archive's payload from bundle.lock: images, charts and binaries.

    python3 build/archive.py digests  --inventory images.yaml
    python3 build/archive.py images   --inventory images.yaml --out DIR [--only TEXT ...]
    python3 build/archive.py charts   --out DIR [--print-digests]
    python3 build/archive.py providers --out DIR --manifests FOLDER
    python3 build/archive.py binaries --out DIR
    python3 build/archive.py sums     --out DIR

- digests: for each image the inventory lists without a digest, resolves its tag at the
  registry and prints the bundle.lock imageDigests line to pin. Review, then paste.
- images: copies every image by digest, all platforms, into one OCI layout (DIR/images),
  named <repository>:<tag> so the site registry can serve it by tag and by digest, then
  checks each digest in the layout. Writes DIR/images.yaml. Refuses unpinned images.
- charts: pulls each chart in bundle.lock that has a chartRepositories entry into
  DIR/charts and checks its sha256 against chartDigests.
- providers: copies the Cluster API, provider and cert-manager manifests that
  hack/fetch_crds.py downloaded and checked (its output folder) into DIR/providers.
- binaries: downloads kind, kubectl, helm and clusterctl into DIR/bin and checks each
  against bundle.lock.
- sums: writes DIR/SHA256SUMS for every file in DIR. Signing will sign this file.

Every step is safe to rerun: what is already there and correct is kept. Needs network
access, skopeo (images, digests) and helm (charts); CI runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHA256 = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class ArchiveError(Exception):
    pass


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True)


def run_bytes(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    """Like run, but stdout stays bytes: a manifest's digest is over its exact bytes."""
    return subprocess.run(list(cmd), check=True, capture_output=True)


def load_lock() -> dict:
    return yaml.safe_load((ROOT / "bundle.lock").read_text())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- images

def split_ref(ref: str):
    """registry/repo[:tag][@sha256:...] -> (repo, tag or None, digest or None)."""
    name, _, digest = ref.partition("@")
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1:], digest or None
    return name, None, digest or None


def layout_name(ref: str) -> str:
    """The name an image gets in the OCI layout: <repo>:<tag>, or <repo>:sha256-<hex>."""
    repo, tag, digest = split_ref(ref)
    if not digest:
        raise ArchiveError(f"{ref}: not pinned by digest")
    return f"{repo}:{tag or digest.replace(':', '-')}"


def read_inventory(path: Path) -> List[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    images = data.get("images")
    if not isinstance(images, list):
        raise ArchiveError(f"{path}: no images list (make it with build/inventory.py --format yaml)")
    return images


def layout_digests(layout: Path) -> Dict[str, str]:
    """Name -> manifest digest, from the OCI layout's index.json."""
    index = layout / "index.json"
    if not index.is_file():
        return {}
    out = {}
    for m in json.loads(index.read_text()).get("manifests", []):
        name = (m.get("annotations") or {}).get("org.opencontainers.image.ref.name")
        if name:
            out[name] = m["digest"]
    return out


def resolve_digests(entries: List[dict], runner: Runner = run_bytes) -> List[str]:
    """bundle.lock imageDigests lines for the inventory's images that have no digest."""
    lines = []
    for e in entries:
        if e.get("digest") == "pinned":
            continue
        ref = e["image"]
        raw = runner(["skopeo", "inspect", "--raw", f"docker://{ref}"]).stdout
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        lines.append(f'  {ref}: "{digest}"')
    return lines


def copy_images(entries: List[dict], out: Path, only: Optional[List[str]] = None,
                runner: Runner = run) -> List[dict]:
    unpinned = [e["image"] for e in entries if e.get("digest") != "pinned"]
    if unpinned:
        raise ArchiveError("not pinned by digest (pin them in bundle.lock imageDigests; "
                           "build/archive.py digests prints the lines):\n  " + "\n  ".join(unpinned))
    chosen = [e for e in entries if not only or any(t in e["image"] for t in only)]
    if not chosen:
        raise ArchiveError(f"no image matches {only}")
    layout = out / "images"
    layout.mkdir(parents=True, exist_ok=True)
    have = layout_digests(layout)
    record = []
    for e in chosen:
        ref = e["image"]
        repo, tag, digest = split_ref(ref)
        name = layout_name(ref)
        if have.get(name) == digest:
            print(f"have  {name}")
        else:
            print(f"copy  {ref}")
            # All platforms, byte for byte: the site pulls the same index digest the
            # manifests name, so the layout must hold that exact index and every image in it.
            runner(["skopeo", "copy", "--all", "--preserve-digests", "--retry-times", "3",
                    f"docker://{ref}", f"oci:{layout}:{name}"])
            got = layout_digests(layout).get(name)
            if got != digest:
                raise ArchiveError(f"{ref}: the layout holds {got} under {name}, not {digest}")
        record.append({"image": ref, "name": name, "tag": tag, "digest": digest,
                       "sources": e.get("sources", [])})
    old = {}
    images_yaml = out / "images.yaml"
    if images_yaml.is_file():
        old = {i["image"]: i for i in (yaml.safe_load(images_yaml.read_text()) or {}).get("images", [])}
    old.update({i["image"]: i for i in record})
    images_yaml.write_text(yaml.safe_dump({"images": [old[k] for k in sorted(old)]}, sort_keys=False))
    return record


# ---------------------------------------------------------------- charts

def chart_list(lock: dict) -> List[tuple]:
    """(name, version, repository, sha256 or '') for every chart with a repository."""
    repos = lock.get("chartRepositories") or {}
    digests = lock.get("chartDigests") or {}
    return [(n, str(v), repos[n], digests.get(n) or "")
            for n, v in (lock.get("charts") or {}).items() if n in repos]


def pull_charts(lock: dict, out: Path, print_digests: bool = False, runner: Runner = run) -> List[str]:
    dest = out / "charts"
    dest.mkdir(parents=True, exist_ok=True)
    unpinned = []
    lines = []
    for name, version, repo, want in chart_list(lock):
        with tempfile.TemporaryDirectory() as tmp:
            runner(["helm", "pull", name, "--repo", repo, "--version", version, "--destination", tmp])
            pulled = sorted(Path(tmp).glob("*.tgz"))
            if len(pulled) != 1:
                raise ArchiveError(f"helm pull {name} {version}: expected one .tgz, got {len(pulled)}")
            got = sha256_file(pulled[0])
            lines.append(f'  {name}: "{got}"')
            if want and got != want:
                raise ArchiveError(f"chart {name} {version}: sha256 {got}, bundle.lock chartDigests says {want}")
            if not want:
                unpinned.append(name)
            shutil.copy2(pulled[0], dest / pulled[0].name)
            print(f"chart {pulled[0].name} {got}")
    if print_digests:
        print("chartDigests:\n" + "\n".join(lines))
    elif unpinned:
        raise ArchiveError(f"no bundle.lock chartDigests for {', '.join(unpinned)} "
                           "(run with --print-digests, review, pin)")
    return lines


# ---------------------------------------------------------------- providers

def copy_providers(manifests: Path, out: Path) -> int:
    """Copy hack/fetch_crds.py's checked output folder into DIR/providers."""
    if not (manifests / ".complete").is_file():
        raise ArchiveError(f"{manifests}: not a complete hack/fetch_crds.py output folder")
    files = sorted(f for f in manifests.rglob("*") if f.is_file() and f.name != ".complete")
    if not files:
        raise ArchiveError(f"{manifests}: no manifests there")
    dest = out / "providers"
    if dest.exists():
        shutil.rmtree(dest)
    for f in files:
        target = dest / f.relative_to(manifests)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    print(f"providers {len(files)} files")
    return len(files)


# ---------------------------------------------------------------- binaries

def binary_list(lock: dict) -> List[tuple]:
    """(file name in bin/, URL, sha256 of the download, member to extract or None)."""
    k8s = lock["kubernetes"]["version"]
    tools = lock["tools"]
    capi = lock["clusterAPI"]["core"]["version"]
    t = (lock.get("bootstrapAppliance") or {}).get("toolChecksums") or {}
    b = lock.get("binarySHA256") or {}
    return [
        ("kind", f"https://github.com/kubernetes-sigs/kind/releases/download/{tools['kind']}/kind-linux-amd64",
         t.get("kind", ""), None),
        ("kubectl", f"https://dl.k8s.io/release/{k8s}/bin/linux/amd64/kubectl", t.get("kubectl", ""), None),
        ("helm", f"https://get.helm.sh/helm-{tools['helm']}-linux-amd64.tar.gz", t.get("helm", ""),
         "linux-amd64/helm"),
        ("clusterctl", f"https://github.com/kubernetes-sigs/cluster-api/releases/download/{capi}/clusterctl-linux-amd64",
         b.get("clusterctl-linux-amd64", ""), None),
    ]


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "dciab-archive"})
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def fetch_binaries(lock: dict, out: Path, fetch: Callable[[str, Path], None] = download) -> None:
    dest = out / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    for name, url, want, member in binary_list(lock):
        if not SHA256.fullmatch(want or ""):
            raise ArchiveError(f"{name}: no sha256 pinned in bundle.lock for {url}")
        with tempfile.TemporaryDirectory() as tmp:
            got_file = Path(tmp) / "download"
            fetch(url, got_file)
            got = sha256_file(got_file)
            if got != want:
                raise ArchiveError(f"{name}: {url} has sha256 {got}, bundle.lock says {want}")
            if member:
                with tarfile.open(got_file) as tar:
                    src = tar.extractfile(member)
                    if src is None:
                        raise ArchiveError(f"{name}: {member} is not a file in {url}")
                    with (Path(tmp) / name).open("wb") as f:
                        shutil.copyfileobj(src, f)
                got_file = Path(tmp) / name
            target = dest / name
            shutil.move(str(got_file), target)
            target.chmod(0o755)
            print(f"bin   {name} {want}")


# ---------------------------------------------------------------- sums

def write_sums(out: Path) -> Path:
    sums = out / "SHA256SUMS"
    lines = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p != sums):
        lines.append(f"{sha256_file(path)}  {path.relative_to(out).as_posix()}")
    tmp = sums.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, sums)
    print(f"sums  {len(lines)} files")
    return sums


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digests", help="print imageDigests lines for unpinned images")
    d.add_argument("--inventory", type=Path, required=True)
    i = sub.add_parser("images", help="copy images by digest into DIR/images")
    i.add_argument("--inventory", type=Path, required=True)
    i.add_argument("--out", type=Path, required=True)
    i.add_argument("--only", action="append", help="copy only images whose reference contains this text")
    c = sub.add_parser("charts", help="pull charts into DIR/charts")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--print-digests", action="store_true", help="print chartDigests lines instead of failing")
    v = sub.add_parser("providers", help="copy hack/fetch_crds.py's manifests into DIR/providers")
    v.add_argument("--out", type=Path, required=True)
    v.add_argument("--manifests", type=Path, required=True, help="hack/fetch_crds.py's output folder")
    b = sub.add_parser("binaries", help="download pinned binaries into DIR/bin")
    b.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sums", help="write DIR/SHA256SUMS")
    s.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    try:
        if args.cmd == "digests":
            lines = resolve_digests(read_inventory(args.inventory))
            print("imageDigests:\n" + "\n".join(lines) if lines else "every image is pinned")
        elif args.cmd == "images":
            copy_images(read_inventory(args.inventory), args.out, args.only)
        elif args.cmd == "charts":
            pull_charts(load_lock(), args.out, args.print_digests)
        elif args.cmd == "providers":
            copy_providers(args.manifests, args.out)
        elif args.cmd == "binaries":
            fetch_binaries(load_lock(), args.out)
        elif args.cmd == "sums":
            write_sums(args.out)
    except (ArchiveError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"archive: {exc}{(': ' + detail.strip()) if detail else ''}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
