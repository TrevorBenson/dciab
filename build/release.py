#!/usr/bin/env python3
"""Release names and checks for cutting a DCiaB release (docs/VERSIONING.md, docs/RELEASING.md).

    python3 build/release.py plan [--version V] [--ref REF] [--existing-tag TAG ...] [--github-output FILE]
    python3 build/release.py next [--version V] [--stage alpha|beta|ga]
    python3 build/release.py assets DIST ARCHIVE

- plan: checks that VERSION is a release form (N.0.0-alpha.M, N.0.0-beta.M or N.0.0), that the
  run is on main, and that the tag doesn't exist yet; prints the tag, the release name
  (rNalphaM, rNbetaM or rN), its title and whether it is a pre-release. --github-output also
  writes them as step outputs.
- next: prints the VERSION the next cut uses (alpha.M+1 by default; --stage beta or ga moves on).
- assets: gathers what the GitHub release carries into DIST/release: the archive (split into
  ARCHIVE.part-NN pieces when it is over GitHub's 2 GiB asset limit), ARCHIVE.sha256 (of the
  whole archive), cosign.pub and MANIFEST; prints one line per asset.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
RELEASE = re.compile(r"(?P<n>[1-9][0-9]*)\.0\.0(?:-(?P<stage>alpha|beta)\.(?P<m>[1-9][0-9]*))?")


class ReleaseError(Exception):
    pass


def parse(version: str) -> Dict[str, object]:
    m = RELEASE.fullmatch(version.strip())
    if not m:
        raise ReleaseError(f"VERSION {version.strip()!r} is not a release form (N.0.0-alpha.M, N.0.0-beta.M "
                           "or N.0.0; minor and patch stay 0)")
    n, stage, cut = int(m["n"]), m["stage"], int(m["m"]) if m["m"] else None
    return {"n": n, "stage": stage or "ga", "cut": cut}


def name(version: str) -> str:
    v = parse(version)
    return f"r{v['n']}" if v["stage"] == "ga" else f"r{v['n']}{v['stage']}{v['cut']}"


def plan(version: str, ref: str, existing_tags: List[str]) -> Dict[str, str]:
    version = version.strip()
    v = parse(version)
    if ref != "refs/heads/main":
        raise ReleaseError(f"releases are cut from main, not {ref}")
    tag = f"v{version}"
    if tag in existing_tags:
        raise ReleaseError(f"tag {tag} exists already: move VERSION to the next cut ({next_version(version)})")
    rname = name(version)
    return {"version": version, "tag": tag, "name": rname, "title": f"{rname} ({tag})",
            "prerelease": "false" if v["stage"] == "ga" else "true",
            "archive": f"dciab-{version}.tar", "next": next_version(version)}


def next_version(version: str, stage: Optional[str] = None) -> str:
    v = parse(version)
    n, cur = v["n"], v["stage"]
    stage = stage or cur
    order = ["alpha", "beta", "ga"]
    if cur == "ga":
        # Fixes after GA go into the next release's alpha.
        return f"{n + 1}.0.0-alpha.1"
    if order.index(stage) < order.index(cur):
        raise ReleaseError(f"can't go back from {cur} to {stage}")
    if stage == cur:
        return f"{n}.0.0-{cur}.{v['cut'] + 1}"
    return f"{n}.0.0" if stage == "ga" else f"{n}.0.0-{stage}.1"


# GitHub refuses release assets of 2 GiB or more; stay well under it.
ASSET_LIMIT = 2 * 1024 ** 3 - 64 * 1024 ** 2
PART = 1024 ** 3 + 512 * 1024 ** 2   # 1.5 GiB pieces


def assets(dist: Path, archive: str, limit: int = ASSET_LIMIT, part: int = PART) -> List[Path]:
    """Move the archive and its companions into dist/release; split the archive when too big."""
    src = dist / archive
    out = dist / "release"
    for needed in (src, dist / "cosign.pub", dist / "MANIFEST"):
        if not needed.is_file():
            raise ReleaseError(f"{needed} is missing")
    if out.exists() and any(out.iterdir()):
        raise ReleaseError(f"{out} is not empty")
    out.mkdir(exist_ok=True)
    digest = hashlib.sha256()
    size = src.stat().st_size
    with src.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    (out / f"{archive}.sha256").write_text(f"{digest.hexdigest()}  {archive}\n")
    if size < limit:
        os.replace(src, out / archive)
    else:
        with src.open("rb") as f:
            n = 0
            while True:
                piece = out / f"{archive}.part-{n:02d}"
                written = 0
                with piece.open("wb") as w:
                    while written < part:
                        block = f.read(min(1024 * 1024, part - written))
                        if not block:
                            break
                        w.write(block)
                        written += len(block)
                if written == 0:
                    piece.unlink()
                    break
                n += 1
        src.unlink()
    for name in ("cosign.pub", "MANIFEST"):
        os.replace(dist / name, out / name)
    return sorted(out.iterdir())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--version", help="default: the VERSION file")
    pl.add_argument("--ref", required=True, help="the git ref the run is on (GITHUB_REF)")
    pl.add_argument("--existing-tag", action="append", default=[], help="a tag that exists already")
    pl.add_argument("--github-output", type=Path, help="also append key=value lines here")
    nx = sub.add_parser("next")
    nx.add_argument("--version", help="default: the VERSION file")
    nx.add_argument("--stage", choices=["alpha", "beta", "ga"])
    a = sub.add_parser("assets")
    a.add_argument("dist", type=Path)
    a.add_argument("archive")
    args = p.parse_args(argv)
    if args.cmd == "assets":
        try:
            for path in assets(args.dist, args.archive):
                print(f"{path.name} {path.stat().st_size}")
        except ReleaseError as exc:
            print(f"release: {exc}", file=sys.stderr)
            return 1
        return 0
    version = args.version or (ROOT / "VERSION").read_text()
    try:
        if args.cmd == "plan":
            result = plan(version, args.ref, args.existing_tag)
            lines = [f"{k}={v}" for k, v in result.items()]
            print("\n".join(lines))
            if args.github_output:
                with args.github_output.open("a") as f:
                    f.write("\n".join(lines) + "\n")
        else:
            print(next_version(version, args.stage))
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
