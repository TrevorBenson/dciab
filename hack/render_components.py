#!/usr/bin/env python3
"""Fill in the ${VARIABLES} of CAPI provider component manifests, as clusterctl does.

    python3 hack/render_components.py FILE [FILE ...] > rendered.yaml

Values come from clusterAPI.variables in bundle.lock. An environment variable of the
same name overrides one, which is how the e2e test and a site install pass credentials.
${NAME:=default} falls back to its default when NAME is unset or empty, ${NAME=default}
only when it is unset (envsubst rules). A ${NAME} with no value and no default stops
the render with the list of everything missing.

Values are inserted into YAML as they are, so they are limited to characters that can't
change the document's structure: no whitespace, quotes, '#', braces or brackets.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?=)([^}]*))?\}")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._@:/=+%-]*$")


def substitute(text: str, values: Mapping[str, str]) -> Tuple[str, List[str]]:
    missing: List[str] = []

    def repl(m: re.Match) -> str:
        name, form, default = m.group(1), m.group(2), m.group(3)
        value = values.get(name)
        if value is not None and not (form == ":=" and value == ""):
            return str(value)
        if form is not None:
            return default
        missing.append(name)
        return m.group(0)

    return VARIABLE.sub(repl, text), missing


class UnsafeValue(ValueError):
    pass


def values_from(lock: dict, env: Mapping[str, str]) -> Dict[str, str]:
    """Values for the variables declared in bundle.lock; the environment overrides them.

    Only declared variables are taken from the environment, so nothing a manifest uses
    can be set without a reviewed entry in bundle.lock.
    """
    values = {k: "" if v is None else str(v) for k, v in lock["clusterAPI"].get("variables", {}).items()}
    for name in list(values):
        if name in env:
            values[name] = env[name]
    unsafe = [name for name, v in values.items() if not SAFE_VALUE.match(v)]
    if unsafe:
        raise UnsafeValue(", ".join(sorted(unsafe)))
    return values


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    try:
        values = values_from(lock, os.environ)
    except UnsafeValue as exc:
        print(f"render_components: unsafe characters in: {exc}. Values may only use letters, digits "
              "and . _ @ : / = + % - (they are inserted into YAML as they are).", file=sys.stderr)
        return 1
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
