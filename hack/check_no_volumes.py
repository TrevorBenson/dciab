#!/usr/bin/env python3
"""Fail if any manifest asks for a volume or brings a StorageClass (ADR 0008, decision 4a).

    python3 hack/check_no_volumes.py [--ignore KIND/NAME]... [--ignore-namespace NS]... FILE|DIR ...

MGMT keeps no volumes: nothing DCiaB installs there may hold a PersistentVolumeClaim, a
StatefulSet's volumeClaimTemplates, a pod volume backed by a claim (persistentVolumeClaim,
or an ephemeral volume's volumeClaimTemplate), or a StorageClass. Each argument is a YAML
stream, or a folder whose *.yaml and *.yml files are read. CustomResourceDefinitions are
skipped: their schemas name these fields without asking for anything. emptyDir, hostPath,
nfs, secret and configMap volumes are fine. --ignore and --ignore-namespace leave out what
DCiaB didn't install, such as a test cluster's own StorageClass, when the input is read from
a live cluster (kubectl get ... -o yaml). Prints each offender; exits 1 if there is any.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import yaml

CLAIM_KEYS = ("volumeClaimTemplates", "volumeClaimTemplate", "persistentVolumeClaim")
KINDS = ("PersistentVolumeClaim", "PersistentVolume", "StorageClass")


def _walk(node, path: str) -> Iterator[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            if k in CLAIM_KEYS and v:
                yield here
            yield from _walk(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def offenders(docs, ignore=(), ignore_namespaces=()) -> List[str]:
    """What asks for a volume, one line per finding, from parsed manifests. ignore: KIND/NAME
    entries to leave out; ignore_namespaces: namespaces whose objects are left out."""
    out = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind", "")
        if kind == "List":
            out.extend(offenders(doc.get("items") or [], ignore, ignore_namespaces))
            continue
        if kind == "CustomResourceDefinition":
            continue
        meta = doc.get("metadata") or {}
        name = f"{kind}/{meta.get('name', '?')}"
        if name in ignore or meta.get("namespace") in ignore_namespaces:
            continue
        if kind in KINDS:
            out.append(f"{name}: a {kind}")
            continue
        out.extend(f"{name}: {p}" for p in _walk(doc, ""))
    return out


def files(args: List[str]) -> List[Path]:
    out: List[Path] = []
    for a in args:
        p = Path(a)
        out.extend(sorted(q for q in p.rglob("*") if q.suffix in (".yaml", ".yml")) if p.is_dir() else [p])
    return out


def check(args: List[str], ignore=(), ignore_namespaces=()) -> Tuple[int, List[str]]:
    found, n = [], 0
    for f in files(args):
        docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        n += len(docs)
        found.extend(f"{f}: {o}" for o in offenders(docs, ignore, ignore_namespaces))
    return n, found


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ignore, namespaces, paths = [], [], []
    while argv:
        a = argv.pop(0)
        if a in ("--ignore", "--ignore-namespace") and argv:
            (ignore if a == "--ignore" else namespaces).append(argv.pop(0))
        elif a.startswith("-"):
            paths = []
            break
        else:
            paths.append(a)
    if not paths:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    n, found = check(paths, ignore, namespaces)
    for line in found:
        print(line)
    if found:
        print(f"check_no_volumes: {len(found)} volume request(s) in what DCiaB installs on MGMT "
              "(ADR 0008, decision 4a)", file=sys.stderr)
        return 1
    print(f"check_no_volumes: {n} manifests, none asks for a volume")
    return 0


if __name__ == "__main__":
    sys.exit(main())
