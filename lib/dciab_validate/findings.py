"""Findings: one problem in the config, with where it is, what was found and how to fix it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

PathPart = Union[str, int]
Path = Tuple[PathPart, ...]

ERROR = "error"
WARNING = "warning"


def format_path(path: Path) -> str:
    """('spec', 'workloadClusters', 0, 'name') -> 'spec.workloadClusters[0].name'."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(document)"


@dataclass(frozen=True)
class Finding:
    severity: str
    path: Path
    found: str
    fix: str
    line: Optional[int] = field(default=None, compare=False)

    @property
    def path_str(self) -> str:
        return format_path(self.path)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "path": self.path_str,
            "line": self.line,
            "found": self.found,
            "fix": self.fix,
        }


def attach_lines(findings: List[Finding], lines: Dict[Path, int]) -> List[Finding]:
    """Give each finding the line of the longest prefix of its path that exists in the file."""
    out = []
    for f in findings:
        line = None
        for n in range(len(f.path), 0, -1):
            if f.path[:n] in lines:
                line = lines[f.path[:n]]
                break
        out.append(Finding(f.severity, f.path, f.found, f.fix, line))
    return out


def sort_key(f: Finding):
    return (0 if f.severity == ERROR else 1, f.line if f.line is not None else 0, f.path_str)


def render_text(config_name: str, findings: List[Finding], notes: List[str]) -> str:
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    out = [f"dciab validate: {config_name}", ""]
    for f in sorted(findings, key=sort_key):
        where = f.path_str + (f"  (line {f.line})" if f.line else "")
        out.append(f"{f.severity.upper():<8}{where}")
        out.append(f"        found: {f.found}")
        out.append(f"        fix:   {f.fix}")
        out.append("")
    for note in notes:
        out.append(f"note: {note}")
    if notes:
        out.append("")
    verdict = "The config is not valid." if errors else "The config is valid."
    out.append(f"{_count(len(errors), 'error')}, {_count(len(warnings), 'warning')}. {verdict}")
    return "\n".join(out)


def render_json(config_name: str, findings: List[Finding], notes: List[str]) -> str:
    errors = sum(1 for f in findings if f.severity == ERROR)
    doc = {
        "config": config_name,
        "valid": errors == 0,
        "errors": errors,
        "warnings": len(findings) - errors,
        "findings": [f.to_dict() for f in sorted(findings, key=sort_key)],
        "notes": notes,
    }
    return json.dumps(doc, indent=2)


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
