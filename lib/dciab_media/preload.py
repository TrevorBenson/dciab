"""The Day 0 images the bootstrap appliance carries, loaded into root's container storage.

The appliance image holds the Day 0 images (CoreDNS, Caddy, DCiaB's chrony, Zot and the
proxmox-auto-install-assistant) at their bundle.lock digests, as an OCI layout under
PRELOAD_DIR with an images.yaml, the same format as the archive's payload (build/archive.py
images). They are files, not an additional image store: an image store under /usr breaks bootc
updates, and root's storage (graphroot) moves to the work disk when `dciab media` runs.

`python3 -m dciab_media.preload` (dciab-day0-images.service at boot, and `dciab media`'s preload
step after it moves the storage) loads each one that isn't in root's storage yet, under the name
bundle.lock pins, so `dciab services` finds it with `podman image exists` and loads nothing from
the payload. `--check` only compares the layout with images.yaml.

day0_refs() is the list build.sh copies in: every Day 0 image bundle.lock pins by digest.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PRELOAD_DIR = Path("/usr/share/dciab/day0-images")
REF_NAME = "org.opencontainers.image.ref.name"
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class PreloadError(Exception):
    """A preloaded image is missing, or doesn't match its pinned digest."""


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True)


def day0_refs(lock: dict) -> Dict[str, str]:
    """{key: reference} for the Day 0 images bundle.lock pins by digest. chronyBase is a build
    input of the chrony image, not something the appliance runs."""
    day0 = ((lock.get("day0") or {}).get("images") or {})
    images = lock.get("images") or {}
    wanted = {"coredns": day0.get("coredns"), "caddy": day0.get("caddy"), "chrony": day0.get("chrony"),
              "zot": images.get("zot"), "pveAssistant": images.get("pveAssistant")}
    return {k: str(v) for k, v in wanted.items() if v and DIGEST_RE.search(str(v))}


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


def layout_digests(directory: Path) -> Dict[str, str]:
    """{layout name: digest} from the OCI layout's index.json."""
    try:
        index = json.loads((directory / "images" / "index.json").read_text())
    except (OSError, ValueError) as exc:
        raise PreloadError(f"{directory / 'images' / 'index.json'}: {exc}") from None
    return {m.get("annotations", {}).get(REF_NAME, ""): m.get("digest", "") for m in index.get("manifests", [])}


def check(directory: Path) -> List[str]:
    """Problems with the layout: an image images.yaml lists that isn't there at its digest."""
    have = layout_digests(directory)
    problems = []
    for e in entries(directory):
        want = split_ref(e["image"])[2]
        got = have.get(e["name"])
        if got != want:
            problems.append(f"{e['image']}: the layout holds {got or 'nothing'} under {e['name']}")
        elif not (directory / "images" / "blobs" / "sha256" / want.split(":", 1)[1]).is_file():
            problems.append(f"{e['image']}: blob {want} is missing")
    return problems


def _exists(ref: str, runner: Runner) -> bool:
    try:
        runner(["podman", "image", "exists", ref])
        return True
    except subprocess.CalledProcessError:
        return False


def load(directory: Path = PRELOAD_DIR, runner: Optional[Runner] = None) -> Tuple[List[str], List[str]]:
    """Load each listed image root's storage lacks. Returns (loaded, already there)."""
    runner = runner or run
    loaded, present = [], []
    for e in entries(directory):
        ref = e["image"]
        if _exists(ref, runner):
            present.append(ref)
            continue
        out = runner(["podman", "pull", "-q", f"oci:{directory / 'images'}:{e['name']}"]).stdout
        image_id = out.strip().splitlines()[-1] if out.strip() else ""
        if not image_id:
            raise PreloadError(f"{ref}: podman pull from the layout printed no image ID")
        repo, tag, _ = split_ref(ref)
        runner(["podman", "tag", image_id, f"{repo}:{tag}" if tag else repo])
        if not _exists(ref, runner):
            raise PreloadError(f"{ref}: the appliance's copy ({e['name']}) doesn't match the pinned digest")
        loaded.append(ref)
    return loaded, present


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_media.preload", description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=PRELOAD_DIR, help=f"the images (default {PRELOAD_DIR})")
    ap.add_argument("--check", action="store_true", help="only compare the layout with images.yaml")
    args = ap.parse_args(argv)
    say = lambda msg: print(f"dciab-day0-images: {msg}", file=sys.stderr)  # noqa: E731
    if not (args.dir / "images.yaml").is_file():
        say(f"{args.dir}: no preloaded images; nothing to do")
        return 0
    try:
        if args.check:
            problems = check(args.dir)
            for p in problems:
                say(p)
            if not problems:
                say(f"ok: {len(entries(args.dir))} images at their pinned digests")
            return 1 if problems else 0
        loaded, present = load(args.dir)
    except PreloadError as exc:
        say(str(exc))
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        say(f"{exc} {detail.strip()}".rstrip())
        return 1
    for ref in loaded:
        say(f"loaded {ref}")
    say(f"{len(loaded)} loaded, {len(present)} already in root's container storage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
