#!/usr/bin/env python3
"""Fill in the ${VARIABLES} of CAPI provider component manifests, as clusterctl does.

    python3 hack/render_components.py FILE [FILE ...] > rendered.yaml

Values come from clusterAPI.variables in bundle.lock. An environment variable of the
same name overrides one, which is how the e2e test and a site install pass credentials.
${NAME:=default} and ${NAME=default} fall back to their default; a ${NAME} with no
value and no default stops the render with the list of everything missing.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?=([^}]*))?\}")


def substitute(text: str, values: Mapping[str, str]) -> Tuple[str, List[str]]:
    missing: List[str] = []

    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in values:
            return str(values[name])
        if default is not None:
            return default
        missing.append(name)
        return m.group(0)

    return VARIABLE.sub(repl, text), missing


def values_from(lock: dict, env: Mapping[str, str]) -> Dict[str, str]:
    values = {k: "" if v is None else str(v) for k, v in lock["clusterAPI"].get("variables", {}).items()}
    for name in list(values):
        if name in env:
            values[name] = env[name]
    return values


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    values = values_from(lock, os.environ)
    missing_all: List[str] = []
    out: List[str] = []
    for path in argv:
        text, missing = substitute(Path(path).read_text(), values)
        missing_all += [f"{name} (in {path})" for name in missing]
        out.append(text.rstrip("\n"))
    if missing_all:
        print("render_components: no value for: " + ", ".join(sorted(set(missing_all))), file=sys.stderr)
        print("Add it to clusterAPI.variables in bundle.lock, or set it in the environment.", file=sys.stderr)
        return 1
    print("\n---\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
