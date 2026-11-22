"""dciab validate: check a site config before anything is touched.

Exit codes: 0 valid (warnings allowed), 1 not valid, 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path as FsPath
from typing import List, Optional

from .findings import ERROR, Finding, attach_lines, render_json, render_text

LIVE_NOTE = ("live checks (hypervisor credentials and templates, BMC Redfish, NTP, upstream DNS, "
             "unused-address probes) were not run; this run checked the file only (add --live on the site network)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dciab validate", description="Validate a DCiaB site config.")
    parser.add_argument("config", help="path to the site config, for example ./dciab.yaml")
    parser.add_argument("--root", required=True, help="the unpacked archive (set by the dciab entrypoint)")
    parser.add_argument("--output", choices=["text", "json"], default="text", help="report format")
    parser.add_argument("--live", action="store_true",
                        help="also check the site's services over the network (hypervisor API, BMCs, NTP, DNS, "
                             "unused addresses); off by default so review works offline")
    parser.add_argument("--timeout", type=float, default=5.0, metavar="SECONDS",
                        help="timeout for each live request (default 5)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be more than 0")

    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as exc:
        print(f"dciab validate: missing Python module '{exc.name}'. Install python3-jsonschema and "
              "python3-pyyaml (the archive's rpms/ folder has them).", file=sys.stderr)
        return 2

    from . import checks, schema_check
    from .loader import LoadError, load

    root = FsPath(args.root).resolve()
    config_path = FsPath(args.config)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"dciab validate: cannot read {config_path}: {exc.strerror}", file=sys.stderr)
        return 2

    notes: List[str] = []
    try:
        doc, lines = load(text)
    except LoadError as exc:
        findings = [Finding(ERROR, exc.path, exc.message, "fix the YAML at the line shown", exc.line)]
        print(_render(args.output, str(config_path), findings, notes))
        return 1

    findings = schema_check.check(doc, schema_check.load_schema(root))
    if findings:
        notes.append("the semantic checks (addresses, references, secrets) run once the schema errors are fixed")
    else:
        more, check_notes = checks.run(doc, config_path, root)
        findings.extend(more)
        notes.extend(check_notes)
    if not args.live:
        notes.append(LIVE_NOTE)
    elif any(f.severity == ERROR for f in findings):
        notes.append("live checks run once the errors above are fixed")
    else:
        from . import live

        more, live_notes = live.run(doc, config_path, args.timeout)
        findings.extend(more)
        notes.extend(live_notes)

    findings = attach_lines(findings, lines)
    print(_render(args.output, str(config_path), findings, notes))
    return 1 if any(f.severity == ERROR for f in findings) else 0


def _render(fmt: str, name: str, findings, notes) -> str:
    return render_json(name, findings, notes) if fmt == "json" else render_text(name, findings, notes)
