"""Check the document against schema/dciab.schema.json and turn schema errors into findings."""

from __future__ import annotations

import json
from pathlib import Path as FsPath
from typing import Any, List

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

from .findings import ERROR, Finding, Path, format_path

_NOT_FIXES = {
    "platform": "keep only the block for platform.type and remove the other one",
    "loadBalancer": "remove bgp: it is only used when mode is bgp",
    "pki": "remove certificate, key and chain: they are only used when mode is import-intermediate",
    "workload": "remove overrides: the workload override is complete on its own and takes no overrides",
}


# The usual block storage provider for MGMT and INFRA, by platform. A hint only: the provider
# is required and has no default (ADR 0008, decisions 1 and 4).
USUAL_BLOCK_PROVIDER = {"vsphere": ("vSphere", "vsphere-csi"), "proxmox": ("Proxmox", "proxmox-csi"),
                        "metal3": ("Metal3", "longhorn")}


def load_schema(root: FsPath) -> dict:
    with open(root / "schema" / "dciab.schema.json", encoding="utf-8") as fh:
        return json.load(fh)


def check(doc: Any, schema: dict) -> List[Finding]:
    validator = Draft202012Validator(schema)
    findings: List[Finding] = []
    seen = set()
    for err in validator.iter_errors(doc):
        for f in _to_findings(err):
            key = (f.path, f.found, f.fix)
            if key not in seen:
                seen.add(key)
                findings.append(f)
    return [_block_provider_hint(doc, f) for f in findings]


def _block_provider_hint(doc: Any, f: Finding) -> Finding:
    """A missing spec.storage.block.provider: name the platform's usual choice."""
    if f.path != ("spec", "storage", "block", "provider") or f.found != "missing":
        return f
    spec = doc.get("spec") if isinstance(doc, dict) else None
    platform = spec.get("platform") if isinstance(spec, dict) else None
    ptype = platform.get("type") if isinstance(platform, dict) else None
    usual = USUAL_BLOCK_PROVIDER.get(ptype) if isinstance(ptype, str) else None
    fix = ("add provider: required for MGMT and INFRA, with no default (ADR 0008). "
           + (f"On {usual[0]}, the usual choice is {usual[1]}, with its settings block" if usual else
              "Usually vsphere-csi on vSphere, proxmox-csi on Proxmox, longhorn on bare metal (Metal3)"))
    return Finding(f.severity, f.path, f.found, fix, f.line)


def _to_findings(err: ValidationError) -> List[Finding]:
    path: Path = tuple(err.absolute_path)
    schema = err.schema if isinstance(err.schema, dict) else {}
    desc = schema.get("description")

    if schema.get("x-secret"):
        return [Finding(ERROR, path, "a value that is not a secret reference (value not shown)",
                        "use {file: <path>} or {env: <VARIABLE>}; secrets are never written inline")]

    v = err.validator
    if v in ("oneOf", "anyOf") and err.context:
        if desc:
            return [Finding(ERROR, path, _brief(err.instance), desc)]
        return _to_findings(best_match(err.context))

    if v == "required":
        missing = [k for k in err.validator_value if isinstance(err.instance, dict) and k not in err.instance]
        props = schema.get("properties", {})
        out = []
        for k in missing:
            hint = props.get(k, {}).get("description") if isinstance(props.get(k), dict) else None
            fix = f"add {k}" + (f": {hint}" if hint else "")
            out.append(Finding(ERROR, path + (k,), "missing", fix))
        return out

    if v == "additionalProperties":
        allowed = set(schema.get("properties", {}))
        extras = [k for k in err.instance if k not in allowed] if isinstance(err.instance, dict) else []
        fix = desc or "remove it, or check its spelling"
        return [Finding(ERROR, path + (k,), "a field this config does not use", fix) for k in extras]

    if v == "type":
        expected = err.validator_value
        found = _brief(err.instance)
        if expected == "string" and isinstance(err.instance, (bool, int, float)):
            kind = "a boolean" if isinstance(err.instance, bool) else "a number"
            return [Finding(ERROR, path, f"{found} (YAML read this as {kind})", "put the value in quotes")]
        return [Finding(ERROR, path, found, f"must be {_article(expected)}" + (f": {desc}" if desc else ""))]

    if v in ("const", "enum"):
        choices = [err.validator_value] if v == "const" else err.validator_value
        return [Finding(ERROR, path, _brief(err.instance), "use " + " or ".join(json.dumps(c) for c in choices))]

    if v == "not":
        last = next((p for p in reversed(path) if isinstance(p, str)), "")
        return [Finding(ERROR, path, "a combination of fields that cannot be used together",
                        _NOT_FIXES.get(last, desc or err.message))]

    if v in ("minItems", "maxItems"):
        n = len(err.instance) if isinstance(err.instance, list) else "?"
        return [Finding(ERROR, path, f"{n} item(s)", desc or err.message)]

    if v == "uniqueItems":
        return [Finding(ERROR, path, "a repeated entry", "remove the duplicate" + (f"; {desc}" if desc else ""))]

    if v == "minProperties":
        return [Finding(ERROR, path, "an empty map", desc or "add at least one entry")]

    # pattern, minimum, maximum, minLength and anything else
    return [Finding(ERROR, path, _brief(err.instance), desc or err.message)]


def _brief(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return "a map" if isinstance(value, dict) else "a list"
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 80 else text[:77] + "..."


def _article(kind: Any) -> str:
    names = {"string": "a string", "integer": "a whole number", "object": "a map",
             "array": "a list", "boolean": "true or false", "number": "a number"}
    if isinstance(kind, list):
        return " or ".join(names.get(k, k) for k in kind)
    return names.get(kind, str(kind))


__all__ = ["check", "load_schema", "format_path"]
