"""The Day 0 images, loaded once into root's container storage from the archive's payload.

The archive's payload (build/archive.py images) holds every image bundle.lock pins, by digest,
as an OCI layout (payload/images) with images.yaml naming each one's layout entry. `dciab media`
copies the archive onto the work disk and its preload step loads the Day 0 images from that
copy: CoreDNS, Caddy, DCiaB's chrony, Zot and the proxmox-auto-install-assistant (day0_refs).
Each one root's storage doesn't hold yet is pulled from the layout, checked against its pinned
digest (or one of that index's platform manifests) before it gets the pinned name, and tagged,
so `dciab services` finds it with `podman image exists` and loads nothing itself. A copy that
doesn't match is removed, never left tagged. The appliance image carries no images.

`python3 -m dciab_media.preload --payload DIR` does the same by hand; `--check` only hashes the
Day 0 images' blobs in the layout.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REF_NAME = "org.opencontainers.image.ref.name"
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class PreloadError(Exception):
    """A preloaded image is missing, or doesn't match its pinned digest."""


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True)


DAY0_KEYS = ("coredns", "caddy", "chrony", "zot", "pveAssistant")


def day0_refs(lock: dict) -> Dict[str, str]:
    """{key: reference} for the five Day 0 images, each pinned by digest in bundle.lock.
    chronyBase is a build input of the chrony image, not something the site runs. Raises
    PreloadError naming any that is missing or not pinned by digest, so none is silently
    skipped."""
    day0 = ((lock.get("day0") or {}).get("images") or {})
    images = lock.get("images") or {}
    wanted = {"coredns": day0.get("coredns"), "caddy": day0.get("caddy"), "chrony": day0.get("chrony"),
              "zot": images.get("zot"), "pveAssistant": images.get("pveAssistant")}
    bad = [k for k in DAY0_KEYS if not wanted.get(k) or not DIGEST_RE.search(str(wanted[k]))]
    if bad:
        raise PreloadError("bundle.lock doesn't pin these Day 0 images by digest: " + ", ".join(bad)
                           + " (day0.images.coredns/caddy/chrony, images.zot/pveAssistant)")
    return {k: str(wanted[k]) for k in DAY0_KEYS}


def split_ref(ref: str) -> Tuple[str, str, str]:
    """repo, tag ('' if none), digest ('' if none)."""
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    repo, tag = ref, ""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
    return repo, tag, digest


def normalize(repo: str) -> str:
    """docker.io's short forms spelled out, as images.yaml and bundle.lock may differ."""
    if "/" not in repo:
        return "docker.io/library/" + repo
    first = repo.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return repo
    return "docker.io/" + repo


def entries(directory: Path) -> List[dict]:
    """images.yaml: each image's pinned reference and layout name."""
    import yaml

    path = directory / "images.yaml"
    try:
        listed = (yaml.safe_load(path.read_text()) or {}).get("images") or []
    except (OSError, yaml.YAMLError) as exc:
        raise PreloadError(f"{path}: {exc}") from None
    out = []
    for e in listed:
        if not isinstance(e, dict) or not e.get("image") or not e.get("name") or not DIGEST_RE.search(str(e["image"])):
            raise PreloadError(f"{path}: every entry needs an image pinned by digest and a layout name: {e!r}")
        out.append({"image": str(e["image"]), "name": str(e["name"])})
    return out


def day0_entries(directory: Path, refs: Dict[str, str]) -> List[dict]:
    """The payload's images.yaml entries for refs (bundle.lock references), each with the
    pinned reference to tag it with. Raises PreloadError naming any the payload lacks."""
    listed = entries(directory)
    out, missing = [], []
    for ref in refs.values():
        repo, _, digest = split_ref(ref)
        found = [e for e in listed if split_ref(e["image"])[2] == digest
                 and normalize(split_ref(e["image"])[0]) == normalize(repo)]
        if found:
            out.append({"image": ref, "name": found[0]["name"]})
        else:
            missing.append(ref)
    if missing:
        raise PreloadError(f"{directory / 'images.yaml'} lacks these Day 0 images: " + ", ".join(missing)
                           + " (an archive built without them: rebuild it with build/archive.py images)")
    return out


def layout_digests(directory: Path) -> Dict[str, str]:
    """{layout name: digest} from the OCI layout's index.json."""
    try:
        index = json.loads((directory / "images" / "index.json").read_text())
    except (OSError, ValueError) as exc:
        raise PreloadError(f"{directory / 'images' / 'index.json'}: {exc}") from None
    return {m.get("annotations", {}).get(REF_NAME, ""): m.get("digest", "") for m in index.get("manifests", [])}


def _blob(directory: Path, digest: str) -> Path:
    return directory / "images" / "blobs" / "sha256" / digest.split(":", 1)[1]


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _children(directory: Path, digest: str) -> List[dict]:
    """The descriptors a manifest or index blob refers to (manifests, config, layers)."""
    try:
        doc = json.loads(_blob(directory, digest).read_text())
    except (OSError, ValueError):
        return []
    out = list(doc.get("manifests") or [])
    if isinstance(doc.get("config"), dict):
        out.append(doc["config"])
    out.extend(doc.get("layers") or [])
    return [d for d in out if isinstance(d, dict) and str(d.get("digest", "")).startswith("sha256:")]


def platform_digests(directory: Path, digest: str) -> List[str]:
    """The pinned digest and, for an index, the digests of the manifests it lists."""
    return [digest] + [d["digest"] for d in _children(directory, digest)
                       if "manifest" in str(d.get("mediaType", "")) or "index" in str(d.get("mediaType", ""))]


def check(directory: Path, refs: Dict[str, str], full: bool = False) -> List[str]:
    """Problems with the Day 0 images in the layout: one that isn't there at its digest. The
    top blob (the pinned index or manifest) is hashed; with full, every blob it reaches (child
    manifests, configs, layers) is hashed too."""
    have = layout_digests(directory)
    problems = []
    for e in day0_entries(directory, refs):
        want = split_ref(e["image"])[2]
        got = have.get(e["name"])
        if got != want:
            problems.append(f"{e['image']}: the layout holds {got or 'nothing'} under {e['name']}")
            continue
        todo, seen = [want], set()
        while todo:
            d = todo.pop()
            if d in seen:
                continue
            seen.add(d)
            blob = _blob(directory, d)
            if not blob.is_file():
                problems.append(f"{e['image']}: blob {d} is missing")
                continue
            if (d == want or full) and _sha256(blob) != d:
                problems.append(f"{e['image']}: blob {d} doesn't match its digest")
                continue
            if d == want or full:
                todo.extend(c["digest"] for c in _children(directory, d))
    return problems


def _exists(ref: str, runner: Runner) -> bool:
    try:
        runner(["podman", "image", "exists", ref])
        return True
    except subprocess.CalledProcessError:
        return False


def _remove_if_ours(image_id: str, layout_name: str, runner: Runner) -> bool:
    """Remove an image this load pulled, unless it already had other names (the same ID can be
    an image root's storage held before, under its own correct name)."""
    try:
        tags = json.loads(runner(["podman", "image", "inspect", "--format", "{{json .RepoTags}}",
                                  image_id]).stdout or "[]") or []
    except (subprocess.CalledProcessError, ValueError):
        return False
    if any(not str(t).startswith(f"localhost/{layout_name}:") for t in tags):
        return False
    try:
        runner(["podman", "rmi", image_id])
        return True
    except subprocess.CalledProcessError:
        return False


def load(directory: Path, refs: Dict[str, str], runner: Optional[Runner] = None) -> Tuple[List[str], List[str]]:
    """Load each of refs root's storage lacks from the payload at directory. Returns (loaded,
    already there)."""
    runner = runner or run
    loaded, present = [], []
    for e in day0_entries(directory, refs):
        ref = e["image"]
        if _exists(ref, runner):
            present.append(ref)
            continue
        out = runner(["podman", "pull", "-q", f"oci:{directory / 'images'}:{e['name']}"]).stdout
        image_id = out.strip().splitlines()[-1] if out.strip() else ""
        if not image_id:
            raise PreloadError(f"{ref}: podman pull from the layout printed no image ID")
        repo, tag, want = split_ref(ref)
        # Check what was pulled before it gets the pinned name: a copy that doesn't match is
        # removed, never left tagged where podman would use it. The pinned blob is hashed first,
        # so the platform manifests it lists can be trusted as part of the pinned image.
        got = runner(["podman", "image", "inspect", "--format", "{{.Digest}}", image_id]).stdout.strip()
        blob = _blob(directory, want)
        pinned_ok = blob.is_file() and _sha256(blob) == want
        allowed = platform_digests(directory, want) if pinned_ok else [want]
        if got not in allowed:
            removed = _remove_if_ours(image_id, e["name"], runner)
            raise PreloadError(f"{ref}: the payload's copy ({e['name']}) is {got or 'unknown'}, not the "
                               "pinned digest or one of its platform manifests; "
                               + ("removed it" if removed else "it keeps only the names it already had"))
        runner(["podman", "tag", image_id, f"{repo}:{tag}" if tag else repo])
        if not _exists(ref, runner):
            raise PreloadError(f"{ref}: the payload's copy ({e['name']}) doesn't match the pinned digest")
        loaded.append(ref)
    return loaded, present


def lock_refs(directory: Path) -> Dict[str, str]:
    """day0_refs from the bundle.lock of the archive the payload belongs to (its parent)."""
    import yaml
    path = directory.parent / "bundle.lock"
    try:
        return day0_refs(yaml.safe_load(path.read_text()) or {})
    except (OSError, yaml.YAMLError) as exc:
        raise PreloadError(f"{path}: {exc}") from None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_media.preload", description=__doc__.splitlines()[0])
    ap.add_argument("--payload", type=Path, required=True,
                    help="the archive's payload (images.yaml and images/), e.g. <work>/dciab-<VERSION>/payload")
    ap.add_argument("--check", action="store_true", help="only hash the Day 0 images' blobs in the layout")
    ap.add_argument("--full", action="store_true", help="with --check: hash every blob, not just the top one")
    ap.add_argument("--lock", type=Path, help="hold this lock while loading (dciab media's)")
    args = ap.parse_args(argv)
    say = lambda msg: print(f"dciab preload: {msg}", file=sys.stderr)  # noqa: E731
    if not (args.payload / "images.yaml").is_file():
        say(f"{args.payload}: no images.yaml; nothing to do")
        return 0
    lock = None
    if args.lock and not args.check:
        import fcntl
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        # O_NOFOLLOW, as dciab media opens it: /run/lock is world-writable, so a planted symlink
        # mustn't make root create or lock another file.
        try:
            lock = os.fdopen(os.open(args.lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "w")
        except OSError as exc:
            say(f"can't open the lock {args.lock}: {exc.strerror or exc}")
            return 1
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        refs = lock_refs(args.payload)
        if args.check:
            problems = check(args.payload, refs, full=args.full)
            for p in problems:
                say(p)
            if not problems:
                say(f"ok: {len(refs)} Day 0 images at their pinned digests")
            return 1 if problems else 0
        loaded, present = load(args.payload, refs)
    except PreloadError as exc:
        say(str(exc))
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        say(f"{exc} {detail.strip()}".rstrip())
        return 1
    finally:
        if lock is not None:
            lock.close()
    for ref in loaded:
        say(f"loaded {ref}")
    say(f"{len(loaded)} loaded, {len(present)} already in root's container storage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
