#!/usr/bin/env python3
"""Fetch the Redfish test tooling pinned in bundle.lock redfishTesting: DMTF's Redfish Mockup
Server and DMTF's public mockups, each at its pinned commit.

    python3 hack/redfish/fetch.py [DIR]

DIR defaults to $DCIAB_REDFISH_MOCKUPS, else ~/.cache/dciab/redfish. It gets mockup-server/
(redfishMockupServer.py) and mockups/<name>/ for each name in redfishTesting.mockups.names, and
prints DIR. A rerun with the right commits in place changes nothing. Test tooling only: nothing
here goes into the archive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def checkout(repo: str, version: str, commit: str, dest: Path, paths: list[str] | None = None) -> None:
    """dest at commit (tag version, checked against commit); only paths when given (sparse)."""
    if (dest / ".git").is_dir():
        try:
            if git("rev-parse", "HEAD", cwd=dest) == commit:
                return
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "-q", "--depth", "1", "--branch", version]
    if paths:
        args += ["--filter=blob:none", "--sparse"]
    git(*args, repo, str(dest))
    if paths:
        git("sparse-checkout", "set", "--no-cone", *paths, cwd=dest)
    got = git("rev-parse", "HEAD", cwd=dest)
    if got != commit:
        shutil.rmtree(dest)
        raise SystemExit(f"fetch.py: {repo} {version} is {got}, but bundle.lock pins {commit}; refusing it")


def main(argv: list[str]) -> int:
    pins = yaml.safe_load((ROOT / "bundle.lock").read_text())["redfishTesting"]
    out = Path(argv[0] if argv else os.environ.get("DCIAB_REDFISH_MOCKUPS") or Path.home() / ".cache/dciab/redfish")
    server, mockups = pins["mockupServer"], pins["mockups"]
    checkout(server["repo"], server["version"], server["commit"], out / "mockup-server")
    checkout(mockups["repo"], mockups["version"], mockups["commit"], out / "publications",
             ["/LICENSE.md", *[f"/mockups/{n}/" for n in mockups["names"]]])
    (out / "mockups").mkdir(exist_ok=True)
    for name in mockups["names"]:
        src = out / "publications" / "mockups" / name
        if not (src / "index.json").is_file():
            raise SystemExit(f"fetch.py: {name} isn't in {mockups['repo']} {mockups['version']}")
        link = out / "mockups" / name
        if not link.is_symlink():
            if link.exists():
                shutil.rmtree(link)
            link.symlink_to(src)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
