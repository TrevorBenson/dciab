#!/usr/bin/env python3
"""Render docs/CONFIG.md, the site config reference, from schema/dciab.schema.json.

    python3 hack/gen_config_docs.py           # write docs/CONFIG.md
    python3 hack/gen_config_docs.py --check   # exit 1 when docs/CONFIG.md is out of date

The schema is the source: every key's type, whether it's required, its default, its allowed
values and its description come from it. Only the links from each block to the pages that
explain it (SECTION_DOCS) live here. The output depends on nothing but the schema, so it is
the same on every machine; CI runs --check.

Layout: one section per block of spec (spec.platform, spec.network, ...), with a row per
key. An object type the schema uses in two or more places (a VM cluster, a BMC, root device
hints, Longhorn's settings, ...) gets one section of its own under "Shared blocks", and the
rows that use it link there. Value types (ipv4, cidr, a secret reference, ...) are in "Types".
"""

from __future__ import annotations

import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "dciab.schema.json"
OUT = ROOT / "docs" / "CONFIG.md"

# The pages that explain each block, as links relative to docs/. Keyed by spec's property
# names; "" is the top level (apiVersion, kind, metadata, bundleVersion).
SECTION_DOCS: Dict[str, List[Tuple[str, str]]] = {
    "": [("VERSIONING.md", "Versioning"), ("MEDIA.md", "dciab media")],
    "platform": [("PROXMOX.md", "the Proxmox VE API tokens"), ("MGMT.md", "dciab mgmt"),
                 ("adr/0010-failure-domains.md", "ADR 0010: failure domains")],
    "kubernetes": [("VERSIONING.md", "Versioning")],
    "bootstrap": [("adr/0002-bootstrap-appliance-on-ventoy.md", "ADR 0002: the bootstrap appliance"),
                  ("MEDIA.md", "dciab media"), ("SEED.md", "dciab seed")],
    "network": [("adr/0001-day0-hypervisors-and-services.md", "ADR 0001: Day 0 services"),
                ("SEED.md", "dciab seed"), ("adr/0010-failure-domains.md", "ADR 0010: storage networks")],
    "time": [("adr/0010-failure-domains.md", "ADR 0010, decision 9")],
    "failureDomains": [("adr/0010-failure-domains.md", "ADR 0010: failure domains")],
    "clusters": [("MGMT.md", "dciab mgmt"), ("../charts/capi-cluster/README.md", "the capi-cluster chart"),
                 ("VERSIONING.md", "Versioning: class revisions")],
    "storage": [("adr/0008-provider-pattern.md", "ADR 0008: providers"),
                ("../addons/longhorn/README.md", "Longhorn"), ("../addons/csi/README.md", "the CSI drivers"),
                ("PROXMOX.md", "the Proxmox VE API tokens")],
    "backup": [("BACKUP.md", "Backups"), ("adr/0009-backup-and-dr.md", "ADR 0009: backup and DR")],
    "pki": [("SEED.md", "dciab seed: the platform CA"), ("adr/0005-secrets.md", "ADR 0005: secrets and PKI")],
    "identity": [("IDENTITY.md", "Identity"), ("adr/0004-identity-and-sso.md", "ADR 0004: identity and SSO"),
                 ("adr/0008-provider-pattern.md", "ADR 0008, decision 3")],
    "baremetal": [("../charts/capi-clusterclasses/README.md", "the ClusterClasses"),
                  ("adr/0010-failure-domains.md", "ADR 0010: failure domains")],
    "hypervisors": [("HYPERVISORS.md", "dciab hypervisors"),
                    ("adr/0001-day0-hypervisors-and-services.md", "ADR 0001: Day 0 hypervisors")],
    "workloadClusters": [("../charts/capi-cluster/README.md", "the capi-cluster chart"),
                         ("adr/0008-provider-pattern.md", "ADR 0008, decision 4: storage for workload clusters"),
                         ("VERSIONING.md", "Versioning: class revisions")],
}


class Doc:
    def __init__(self, schema: dict):
        self.schema = schema
        self.defs: Dict[str, dict] = schema.get("$defs", {})
        refs: Dict[str, int] = {}
        for name in _refs(schema):
            target = self.alias_target(name)
            refs[target] = refs.get(target, 0) + 1
        # Object types used in two or more places get a section of their own.
        self.shared = [n for n in self.defs if self.is_block(n) and refs.get(n, 0) >= 2]
        self.usage: "OrderedDict[str, List[str]]" = OrderedDict()
        self.within: Optional[str] = None  # the shared block being walked, if any

    # -- the schema's $refs ---------------------------------------------------------------

    def alias_target(self, name: str) -> str:
        """A def that is only allOf: [{$ref}] plus constraints names its target's keys."""
        d = self.defs[name]
        if "properties" not in d and len(d.get("allOf", [])) == 1 and "$ref" in d["allOf"][0]:
            return self.alias_target(_ref_name(d["allOf"][0]["$ref"]))
        return name

    def is_type(self, name: str) -> bool:
        d = self.defs[name]
        return d.get("x-secret", False) or ("properties" not in d and self.alias_target(name) == name)

    def is_block(self, name: str) -> bool:
        return not self.is_type(name) and "properties" in self.defs[name]

    def resolve(self, s: Any) -> Tuple[Optional[str], dict]:
        """(the def a schema names, or None; the schema with that def's keys under its own)."""
        if not isinstance(s, dict) or "$ref" not in s:
            return None, s if isinstance(s, dict) else {}
        name = _ref_name(s["$ref"])
        d = self.defs[name]
        if not self.is_type(name):
            target = self.alias_target(name)
            if target != name:
                merged = dict(self.defs[target])
                merged.update({k: v for k, v in d.items() if k not in ("allOf", "not")})
                merged.update({k: v for k, v in s.items() if k != "$ref"})
                return target, merged
        merged = dict(d)
        merged.update({k: v for k, v in s.items() if k != "$ref"})
        return name, merged

    # -- rows ---------------------------------------------------------------------------

    def rows(self, s: dict, prefix: str, out: List[List[str]]) -> None:
        """A row per key of an object schema, then the keys of the objects inside it."""
        for key, ps, req in self.props(s):
            path = prefix + key
            name, rs = self.resolve(ps)
            out.append([f"`{path}`", self.type_text(name, rs), req, _default(rs), self.allowed(name, rs),
                        _cell(rs.get("description", ""))])
            self.children(name, rs, path, out)

    def children(self, name: Optional[str], rs: dict, path: str, out: List[List[str]]) -> None:
        if name in self.shared:
            where = f"`{path}`" + (f" in [{self.within}](#{_slug(self.within)})" if self.within else "")
            self.usage.setdefault(name, []).append(where)
            return
        if name and self.is_type(name):
            return
        if "properties" in rs or "oneOf" in rs and any("properties" in b for b in rs["oneOf"]):
            self.rows(rs, path + ".", out)
        elif rs.get("type") == "array" and isinstance(rs.get("items"), dict):
            iname, items = self.resolve(rs["items"])
            self.children(iname, items, path + "[]", out)
        elif isinstance(rs.get("additionalProperties"), dict):
            vname, value = self.resolve(rs["additionalProperties"])
            self.children(vname, value, path + "." + _placeholder(rs), out)

    def props(self, s: dict) -> List[Tuple[str, dict, str]]:
        """(key, schema, required text) for each key of an object schema, with the keys and
        requirements of its if/then/else and oneOf branches."""
        base = s.get("properties", {})
        required = set(s.get("required", []))
        when: Dict[str, List[str]] = {}
        branch_props: "OrderedDict[str, List[Tuple[dict, str, bool]]]" = OrderedDict()

        branches: List[Tuple[Any, str]] = []
        for sub in [s] + [a for a in s.get("allOf", []) if isinstance(a, dict)]:
            if "if" in sub:
                yes, no = _condition(sub["if"])
                branches += [(sub.get("then"), yes), (sub.get("else"), no)]
        for b, cond in branches:
            if not isinstance(b, dict):
                continue
            for k in b.get("required", []):
                when.setdefault(k, []).append(cond)
            for k, v in b.get("properties", {}).items():
                if isinstance(v, dict) and k not in base:
                    branch_props.setdefault(k, []).append((v, cond, k in b.get("required", [])))
        one_of = [b for b in s.get("oneOf", []) if isinstance(b, dict)]
        choice = [k for b in one_of for k in b.get("required", [])]
        for b in one_of:
            for k, v in b.get("properties", {}).items():
                if isinstance(v, dict) and k not in base:
                    branch_props.setdefault(k, []).append((v, "", k in b.get("required", [])))

        out = []
        for k, v in base.items():
            if not isinstance(v, dict):
                continue
            if k in required:
                req = "required"
            elif k in choice:
                req = "one of " + ", ".join(f"`{c}`" for c in choice)
            elif k in when:
                req = "required when " + " or ".join(when[k])
            else:
                req = "optional"
            out.append((k, v, req))
        for k, variants in branch_props.items():
            if k in choice:
                out.append((k, variants[0][0], "one of " + ", ".join(f"`{c}`" for c in choice)))
                continue
            same = all(json.dumps(v, sort_keys=True) == json.dumps(variants[0][0], sort_keys=True)
                       for v, _, _ in variants)
            if same and len(variants) == len([b for b in branches if isinstance(b[0], dict)]):
                req = "required" if all(r for _, _, r in variants) else "optional"
                out.append((k, variants[0][0], req))
                continue
            for v, cond, r in variants:
                out.append((k, v, ("required" if r else "optional") + f", when {cond}"))
        return out

    # -- cells --------------------------------------------------------------------------

    def type_text(self, name: Optional[str], rs: dict) -> str:
        if name and self.is_type(name):
            return f"[{name}](#types)"
        if name in self.shared:
            return f"object: [{name}](#{_slug(name)})"
        if "const" in rs:
            return _json_type(rs["const"])
        if "enum" in rs:
            return " or ".join(sorted({_json_type(v) for v in rs["enum"]}))
        t = rs.get("type")
        if t == "array":
            iname, items = self.resolve(rs.get("items", {}))
            inner = self.type_text(iname, items) if (iname or items) else "any"
            return f"array of {inner}"
        if t == "object" and isinstance(rs.get("additionalProperties"), dict) and "properties" not in rs:
            vname, value = self.resolve(rs["additionalProperties"])
            return f"map of {self.type_text(vname, value)}"
        if t is None and ("properties" in rs or "oneOf" in rs):
            return "object"
        return t or "any"

    def allowed(self, name: Optional[str], rs: dict) -> str:
        if name and self.is_type(name):
            return ""  # the Types section has its format
        parts: List[str] = []
        if "const" in rs:
            parts.append(_code(rs["const"]))
        if "enum" in rs:
            parts.append(", ".join(_code(v) for v in rs["enum"]))
        items = rs.get("items") if isinstance(rs.get("items"), dict) else {}
        if "enum" in items:
            parts.append(", ".join(_code(v) for v in items["enum"]))
        names = rs.get("propertyNames", {})
        if isinstance(names, dict) and "enum" in names:
            parts.append("keys: " + ", ".join(_code(v) for v in names["enum"]))
        lo, hi = rs.get("minimum"), rs.get("maximum")
        if lo is not None and hi is not None:
            parts.append(f"{lo} to {hi}")
        elif lo is not None:
            parts.append(f"at least {lo}")
        elif hi is not None:
            parts.append(f"at most {hi}")
        lo, hi = rs.get("minItems"), rs.get("maxItems")
        if lo is not None and lo == hi:
            parts.append(f"exactly {lo} items")
        else:
            if lo:
                parts.append(f"at least {lo} item{'s' if lo > 1 else ''}")
            if hi is not None:
                parts.append(f"at most {hi} items")
        if rs.get("uniqueItems"):
            parts.append("no duplicates")
        if rs.get("minProperties"):
            parts.append(f"at least {rs['minProperties']} entr{'ies' if rs['minProperties'] > 1 else 'y'}")
        if rs.get("minLength", 0) > 1:
            parts.append(f"at least {rs['minLength']} characters")
        if "maxLength" in rs:
            parts.append(f"at most {rs['maxLength']} characters")
        if "pattern" in rs:
            parts.append(_code(rs["pattern"]))
        return "; ".join(parts)

    # -- the page -----------------------------------------------------------------------

    def render(self) -> str:
        spec = self.schema["properties"]["spec"]
        spec_props = OrderedDict((k, v) for k, v, _ in self.props(spec))
        spec_req = {k: r for k, _, r in self.props(spec)}
        blocks = [k for k, v in spec_props.items()
                  if self.resolve(v)[1].get("type") in ("object", "array") or "$ref" in v]
        scalars = [k for k in spec_props if k not in blocks]

        sections: List[Tuple[str, str, List[str]]] = []  # (heading, anchor, lines)

        # Top level: apiVersion, kind, metadata and spec's scalar keys.
        rows: List[List[str]] = []
        top = {k: v for k, v in self.schema["properties"].items() if k != "spec"}
        self.rows(dict(self.schema, properties=top), "", rows)
        sub = dict(spec, properties={k: spec_props[k] for k in scalars})
        sub.pop("allOf", None)
        self.rows(sub, "spec.", rows)
        sections.append(("Top level", "top-level",
                         [_prose(self.schema.get("description", "")), "",
                          _links(SECTION_DOCS[""]), ""] + _table(rows)))

        for key in blocks:
            name, rs = self.resolve(spec_props[key])
            rows = []
            self.children(name, rs, f"spec.{key}", rows)
            path = f"spec.{key}"
            lines = [f"**{spec_req[key].capitalize()}.** {_prose(rs.get('description', ''))}", ""]
            if name in self.shared:
                lines += [f"See [{name}](#{_slug(name)})."]
            if key in SECTION_DOCS:
                lines += [_links(SECTION_DOCS[key])]
            lines.append("")
            if rs.get("type") == "array":
                lines += [f"`{path}` is {self.type_text(name, rs)}. Each item has these keys:", ""]
            lines += _table(rows) if rows else []
            sections.append((path, _slug(path), lines))

        # Shared blocks: walk each (which may add more) until none is left.
        shared_sections: List[Tuple[str, str, List[str]]] = []
        done: List[str] = []
        while True:
            todo = [n for n in self.usage if n not in done]
            if not todo:
                break
            name = todo[0]
            done.append(name)
            d = self.defs[name]
            rows = []
            self.within = name
            self.rows(d, "", rows)
            self.within = None
            lines = []
            if d.get("description"):
                lines += [_prose(d["description"]), ""]
            lines += ["Used at: " + ", ".join(self.usage[name]) + ".", ""]
            lines += _table(rows)
            shared_sections.append((name, _slug(name), lines))

        types = [[f"`{n}`", self.type_text(None, self.defs[n]) if not self.defs[n].get("x-secret") else "object",
                  _type_format(self, n), _cell(self.defs[n].get("description", ""))]
                 for n in self.defs if self.is_type(n)]

        out = [
            "# Site configuration reference",
            "",
            "<!-- Generated by hack/gen_config_docs.py from schema/dciab.schema.json. Don't edit this file:",
            "     change the schema, then run python3 hack/gen_config_docs.py. CI fails when they differ. -->",
            "",
            "Every key of the site config (`dciab.yaml`), from [the schema](../schema/dciab.schema.json). "
            "`dciab validate` checks a config against the schema and against the checks the schema can't express. "
            "Complete examples are in [examples/](../examples/); the [docs index](README.md) has the other pages, "
            "and the [glossary](GLOSSARY.md) defines the terms.",
            "",
            "How to read the tables:",
            "",
            "- **Key** is the full path. `[]` is an item of a list; `<name>` is a key you choose in a map.",
            "- **Required**: *required*; *optional*; *required when* another key has a value; or *one of* "
            "several keys, exactly one of which is given.",
            "- **Default** is what DCiaB uses when an optional key is left out. A site-specific key has none.",
            "- **Allowed** lists the values, ranges and patterns the schema accepts.",
            "- A [secret reference](#types) is `{file: <path>}` or `{env: <VARIABLE>}`. Secrets are never "
            "written in the config.",
            "",
            "## Contents",
            "",
        ]
        for heading, anchor, _ in sections:
            out.append(f"- [{heading}](#{anchor})")
        out.append("- [Shared blocks](#shared-blocks)")
        for heading, anchor, _ in shared_sections:
            out.append(f"  - [{heading}](#{anchor})")
        out.append("- [Types](#types)")
        out.append("")
        for heading, _, lines in sections:
            out += [f"## {heading}", ""] + lines + [""]
        out += ["## Shared blocks", "",
                "Blocks the config uses in more than one place. The rows above that use one link here.", ""]
        for heading, _, lines in shared_sections:
            out += [f"### {heading}", ""] + lines + [""]
        out += ["## Types", "", "Value types the keys above use.", ""]
        out += _table(types, ["Type", "Is", "Format", "Description"])
        return "\n".join(out).rstrip("\n") + "\n"


# -- helpers --------------------------------------------------------------------------------

def _refs(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield _ref_name(node["$ref"])
        for v in node.values():
            yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _condition(cond: dict) -> Tuple[str, str]:
    """(when it holds, when it doesn't) for a simple if."""
    props = cond.get("properties", {})
    if len(props) == 1:
        (k, v), = props.items()
        if "const" in v:
            value = v["const"] if isinstance(v["const"], str) else json.dumps(v["const"])
            return f"`{k}: {value}`", f"`{k}` isn't `{value}`"
        if v.get("maxItems") == 0:
            return f"`{k}` is empty", f"`{k}` isn't empty"
    return "a condition holds (see the description)", "it doesn't"


def _json_type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, str):
        return "string"
    return type(v).__name__


def _code(v: Any) -> str:
    text = v if isinstance(v, str) else json.dumps(v)
    return "`" + text.replace("|", "\\|").replace("`", "'") + "`"


def _default(rs: dict) -> str:
    if "default" not in rs:
        return ""
    return _code(json.dumps(rs["default"], separators=(", ", ": ")) if not isinstance(rs["default"], str)
                 else rs["default"])


def _cell(text: str) -> str:
    """Text for a table cell: one line, with | escaped and <placeholders> kept visible."""
    return _html(" ".join(str(text).split())).replace("|", "\\|")


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prose(text: str) -> str:
    """A description as a sentence: no leading "optional:" or "required:" (the heading says
    which), a capital letter and a full stop."""
    text = re.sub(r"^(optional|required)\b[,;:]?\s*", "", " ".join(str(text).split()))
    text = _html(text)
    if text and not text.endswith("."):
        text += "."
    return text[:1].upper() + text[1:]


def _placeholder(rs: dict) -> str:
    return "<consumer>" if "propertyNames" in rs else "<name>"


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _links(links: List[Tuple[str, str]]) -> str:
    return "See also: " + ", ".join(f"[{title}]({href})" for href, title in links) + "."


def _table(rows: List[List[str]], head: Optional[List[str]] = None) -> List[str]:
    head = head or ["Key", "Type", "Required", "Default", "Allowed", "Description"]
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(" --- " for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _type_format(doc: Doc, name: str) -> str:
    d = doc.defs[name]
    if d.get("x-secret"):
        return "`{file: <path>}` or `{env: <VARIABLE>}`"
    return doc.allowed(None, d)


def render(schema: dict) -> str:
    return Doc(schema).render()


def main(argv: List[str]) -> int:
    check = "--check" in argv
    text = render(json.loads(SCHEMA.read_text(encoding="utf-8")))
    if check:
        have = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if have != text:
            print(f"{OUT.relative_to(ROOT)} is out of date with {SCHEMA.relative_to(ROOT)}: "
                  "run python3 hack/gen_config_docs.py and commit the result", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
