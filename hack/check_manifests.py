#!/usr/bin/env python3
"""Check Kubernetes manifests against the CRDs of the providers DCiaB pins.

    python3 hack/check_manifests.py --crds DIR [--expect Cluster=1] FILE[:NAMESPACE] ...

FILE may be '-' for stdin. NAMESPACE applies to objects in that file that set none
(Helm output omits it); without one they are in 'default'.

For every object it checks the schema of its CRD more strictly than the API server
(unknown fields are errors here; the server drops them silently), including the
formats the server enforces (uri, ipv4, ipv6, cidr), and evaluates the CRD's simple
CEL rules (has(), &&, ||, !, comparisons, size()). Rules using other CEL
features are skipped and counted.

For a ClusterClass it also checks, in the class's namespace, that:
  - every templateRef names an object in the input,
  - every patch selector picks a template the class references,
  - every JSON patch path exists in the target template's schema,
  - the patches apply in order to the templates as RFC 6902 requires
    (replace/remove need the target, add needs its parent),
  - literal patch values fit the schema at their path,
  - variables used in patches are declared, including nested fields, and fit their targets,
  - objects produced by template patches use only known fields.

For a Cluster with a topology it checks that its ClusterClass is in the input and that
required variables are set, unknown ones are absent, and values fit.

Exit status 0 when clean, 1 when anything is wrong, 2 when it could not run.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ipaddress
import yaml
from jsonschema import Draft7Validator, FormatChecker

Key = Tuple[str, str]               # (apiVersion, kind)
ObjKey = Tuple[str, str, str, str]  # (apiVersion, kind, namespace, name)

CORE_GROUPS = {"v1", "apps/v1", "batch/v1", "rbac.authorization.k8s.io/v1", "networking.k8s.io/v1"}
TEMPLATE_ACTION = re.compile(r"{{(.*?)}}", re.S)
VAR_REF = re.compile(r"(?<![\w)\].])\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")


# --- CRD schemas -------------------------------------------------------------------------

def load_crds(dirs: List[Path]) -> Dict[Key, dict]:
    schemas: Dict[Key, dict] = {}
    for d in dirs:
        for f in sorted(d.rglob("*.yaml")):
            for doc in yaml.safe_load_all(f.read_text()):
                if not doc or doc.get("kind") != "CustomResourceDefinition":
                    continue
                group = doc["spec"]["group"]
                kind = doc["spec"]["names"]["kind"]
                for v in doc["spec"]["versions"]:
                    if v.get("served") and "schema" in v:
                        schemas[(f"{group}/{v['name']}", kind)] = v["schema"]["openAPIV3Schema"]
    return schemas


# Formats the Kubernetes API server enforces on CRD fields (kube-openapi), checked the same way.
FORMATS = FormatChecker(formats=())


@FORMATS.checks("uri")
def _uri(value: object) -> bool:
    """Go's url.ParseRequestURI: an absolute URI with a scheme, or an absolute path."""
    if not isinstance(value, str):
        return True
    if value.startswith("/"):
        return True
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$", value))


@FORMATS.checks("ipv4")
def _ipv4(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


@FORMATS.checks("ipv6")
def _ipv6(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        ipaddress.IPv6Address(value)
        return True
    except ValueError:
        return False


@FORMATS.checks("cidr")
def _cidr(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        ipaddress.ip_network(value, strict=False)
        return "/" in value
    except ValueError:
        return False


def validator(schema: dict) -> Draft7Validator:
    return Draft7Validator(strict(schema), format_checker=FORMATS)


def strict(schema: Any) -> Any:
    """OpenAPI v3 (Kubernetes flavour) -> JSON Schema draft 7, rejecting unknown fields."""
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k.startswith("x-kubernetes-") or k in ("nullable", "x-metadata"):
            continue
        if k in ("properties", "patternProperties"):
            out[k] = {pk: strict(pv) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties", "not"):
            out[k] = strict(v) if isinstance(v, dict) else v
        elif k in ("allOf", "anyOf", "oneOf"):
            out[k] = [strict(x) for x in v]
        else:
            out[k] = v
    if schema.get("x-kubernetes-int-or-string"):
        out.pop("type", None)
        out["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if schema.get("nullable") and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    if not schema.get("x-kubernetes-preserve-unknown-fields") and "properties" in out \
            and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def schema_at(raw: dict, pointer: str) -> Optional[dict]:
    """The raw sub-schema at a JSON pointer, '{}' where anything goes, None if the path is invalid."""
    s: Any = raw
    for seg in [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]:
        if s is None:
            return None
        if s.get("x-kubernetes-preserve-unknown-fields") and seg not in s.get("properties", {}):
            return {}
        if seg in s.get("properties", {}):
            s = s["properties"][seg]
        elif s.get("type") == "array" and (seg == "-" or seg.isdigit()):
            s = s.get("items", {})
        elif isinstance(s.get("additionalProperties"), dict):
            s = s["additionalProperties"]
        else:
            return None
    return s


def fits(value: Any, raw_schema: dict) -> List[str]:
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(value)'}: {e.message}"
            for e in validator(raw_schema).iter_errors(value)]


# --- a small CEL subset ----------------------------------------------------------------------

class _Missing(Exception):
    pass


_CEL_TOKEN = re.compile(r"""\s*(?:(?P<has>has\()|(?P<size>\.size\(\))|(?P<path>self(?:\.[A-Za-z_]\w*)*)
    |(?P<op>&&|\|\||==|!=|>=|<=|>|<|!)|(?P<num>-?\d+)|(?P<str>'[^']*')|(?P<kw>true|false)
    |(?P<paren>[()])|(?P<bad>\S+))""", re.X)


def cel_to_python(rule: str) -> Optional[str]:
    """Translate rules made of has(), field access, size(), !, &&, ||, comparisons and literals.

    Returns None for anything else (oldSelf, macros such as all()/exists(), matches(), lists...).
    """
    out: List[str] = []
    pos = 0
    depth_has: List[int] = []
    depth = 0
    while pos < len(rule):
        m = _CEL_TOKEN.match(rule, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        if m.group("bad"):
            return None
        if m.group("has"):
            out.append("_has(")
            depth += 1
            depth_has.append(depth)
        elif m.group("path"):
            fields = m.group("path").split(".")[1:]
            if depth_has and depth_has[-1] == depth:
                out.append(repr(fields))
            else:
                out.append(f"_get(self, {fields!r})")
        elif m.group("size"):
            out[-1] = f"len({out[-1]})"
        elif m.group("op"):
            out.append({"&&": " and ", "||": " or ", "!": " not "}.get(m.group("op"), f" {m.group('op')} "))
        elif m.group("num") or m.group("str"):
            out.append(m.group("num") or m.group("str"))
        elif m.group("kw"):
            out.append("True" if m.group("kw") == "true" else "False")
        elif m.group("paren"):
            if m.group("paren") == "(":
                depth += 1
                out.append("(")
            else:
                if depth_has and depth_has[-1] == depth:
                    depth_has.pop()
                depth -= 1
                out.append(")")
    return "".join(out)


def _get(obj: Any, fields: List[str]) -> Any:
    for f in fields:
        if not isinstance(obj, dict) or f not in obj:
            raise _Missing(f)
        obj = obj[f]
    return obj


def _has_fn(self_obj: Any):
    def _has(fields: List[str]) -> bool:
        try:
            _get(self_obj, fields)
            return True
        except _Missing:
            return False
    return _has


def cel_violations(value: Any, raw: dict, path: str = "") -> Tuple[List[str], int]:
    """Evaluate supported CEL rules over an object. Returns (violations, rules skipped)."""
    found: List[str] = []
    skipped = 0
    if not isinstance(raw, dict):
        return found, skipped
    for rule in raw.get("x-kubernetes-validations", []) or []:
        expr = cel_to_python(rule.get("rule", ""))
        if expr is None:
            skipped += 1
            continue
        try:
            ok = eval(expr, {"__builtins__": {}}, {"_get": _get, "_has": _has_fn(value), "self": value, "len": len})  # noqa: S307 - whitelisted tokens only
        except (_Missing, TypeError):
            continue  # CEL would not reach this rule for a missing optional field
        if not ok:
            found.append(f"{path or '/'}: {rule.get('message') or rule['rule']}")
    if isinstance(value, dict):
        for k, v in value.items():
            if k in raw.get("properties", {}):
                f, s = cel_violations(v, raw["properties"][k], f"{path}/{k}")
                found += f
                skipped += s
    elif isinstance(value, list) and isinstance(raw.get("items"), dict):
        for i, v in enumerate(value):
            f, s = cel_violations(v, raw["items"], f"{path}/{i}")
            found += f
            skipped += s
    return found, skipped


# --- checks ---------------------------------------------------------------------------------

def md_name_matches(pattern: str, name: str) -> bool:
    """CAPI's machineDeploymentClass name matching: exact, '*', prefix* and *suffix."""
    if pattern == "*" or pattern == name:
        return True
    if pattern.endswith("*") and name.startswith(pattern[:-1]):
        return True
    return pattern.startswith("*") and name.endswith(pattern[1:])


class Checker:
    def __init__(self, schemas: Dict[Key, dict], docs: List[Tuple[str, str, dict]]):
        """docs: (source, namespace, object)."""
        self.schemas = schemas
        self.docs = docs
        self.problems: List[str] = []
        self.cel_skipped = 0
        self.by_key: Dict[ObjKey, dict] = {}
        for _, ns, d in docs:
            self.by_key[(d.get("apiVersion", ""), d.get("kind", ""), ns, d.get("metadata", {}).get("name", ""))] = d

    def problem(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")

    def run(self) -> List[str]:
        for src, ns, doc in self.docs:
            where = f"{src}: {doc.get('kind', '?')}/{doc.get('metadata', {}).get('name', '?')}"
            if not self.check_schema(where, doc):
                continue  # later checks assume a valid object
            try:
                if doc["kind"] == "ClusterClass":
                    self.check_clusterclass(where, ns, doc)
                if doc["kind"] == "Cluster" and "topology" in doc.get("spec", {}):
                    self.check_topology(where, ns, doc)
            except (KeyError, TypeError, IndexError) as exc:
                self.problem(where, f"could not check further: {type(exc).__name__} {exc}")
        return self.problems

    def check_schema(self, where: str, doc: dict) -> bool:
        if not all(k in doc for k in ("apiVersion", "kind", "metadata")):
            self.problem(where, "missing apiVersion, kind or metadata")
            return False
        key = (doc["apiVersion"], doc["kind"])
        if key not in self.schemas:
            if doc["apiVersion"] not in CORE_GROUPS:
                self.problem(where, f"no CRD found for {doc['apiVersion']} {doc['kind']}")
            return doc["apiVersion"] in CORE_GROUPS
        before = len(self.problems)
        for e in validator(self.schemas[key]).iter_errors(doc):
            path = "/".join(str(p) for p in e.absolute_path)
            self.problem(where, f"{path}: {e.message}")
        violations, skipped = cel_violations(doc, self.schemas[key])
        self.cel_skipped += skipped
        for v in violations:
            self.problem(where, f"CEL rule: {v}")
        return len(self.problems) == before

    # ClusterClass -------------------------------------------------------------------------

    @staticmethod
    def _refs(spec: dict) -> Dict[str, List[dict]]:
        """Template refs grouped by how a patch selector can match them."""
        cp = spec["controlPlane"]
        groups: Dict[str, List[dict]] = {"controlPlane": [cp["templateRef"]],
                                         "infrastructureCluster": [spec["infrastructure"]["templateRef"]]}
        if "machineInfrastructure" in cp:
            groups["controlPlane"].append(cp["machineInfrastructure"]["templateRef"])
        for md in spec.get("workers", {}).get("machineDeployments", []):
            groups.setdefault(f"md:{md['class']}", []).extend(
                [md["bootstrap"]["templateRef"], md["infrastructure"]["templateRef"]])
        return groups

    def check_clusterclass(self, where: str, ns: str, cc: dict) -> None:
        spec = cc["spec"]
        groups = self._refs(spec)
        working: Dict[ObjKey, dict] = {}
        for refs in groups.values():
            for ref in refs:
                key = (ref["apiVersion"], ref["kind"], ns, ref["name"])
                if key not in self.by_key:
                    self.problem(where, f"templateRef {ref['kind']}/{ref['name']} ({ref['apiVersion']}) "
                                        f"is not in the input in namespace {ns}")
                else:
                    working[key] = copy.deepcopy(self.by_key[key])

        variables = {v["name"]: v for v in spec.get("variables", [])}
        for name, v in variables.items():
            try:
                Draft7Validator.check_schema(strict(v["schema"]["openAPIV3Schema"]))
            except Exception as exc:  # noqa: BLE001 - report any schema error
                self.problem(where, f"variable {name}: invalid schema: {exc}")

        for patch in spec.get("patches", []):
            pwhere = f"{where}: patch {patch['name']}"
            if "enabledIf" in patch:
                self._check_template_vars(pwhere + " enabledIf", patch["enabledIf"], variables)
            for d in patch.get("definitions", []):
                sel = d["selector"]
                targets = self._select(sel, groups)
                if not targets:
                    self.problem(pwhere, f"selector {sel['kind']} {sel['matchResources']} matches no "
                                         "template the class references")
                    continue
                for ref in targets:
                    raw = self.schemas.get((ref["apiVersion"], ref["kind"]))
                    if raw is None:
                        continue
                    obj = working.get((ref["apiVersion"], ref["kind"], ns, ref["name"]))
                    for op in d["jsonPatches"]:
                        self._check_op(pwhere, ref, raw, op, variables)
                        if obj is not None:
                            self._apply(pwhere, ref, obj, op, variables)

    @staticmethod
    def _select(sel: dict, groups: Dict[str, List[dict]]) -> List[dict]:
        mr = sel["matchResources"]
        candidates: List[dict] = []
        if mr.get("controlPlane"):
            candidates += groups["controlPlane"]
        if mr.get("infrastructureCluster"):
            candidates += groups["infrastructureCluster"]
        for pattern in mr.get("machineDeploymentClass", {}).get("names", []):
            for key, refs in groups.items():
                if key.startswith("md:") and md_name_matches(pattern, key[3:]):
                    candidates += refs
        seen, out = set(), []
        for r in candidates:
            k = (r["apiVersion"], r["kind"], r["name"])
            if r["apiVersion"] == sel["apiVersion"] and r["kind"] == sel["kind"] and k not in seen:
                seen.add(k)
                out.append(r)
        return out

    @staticmethod
    def _variable_schema(variables: dict, dotted: str) -> Tuple[Optional[dict], str]:
        """Schema of a variable reference such as 'vsphere.server'; (None, reason) if undeclared."""
        name, *rest = dotted.split(".")
        if name == "builtin":
            return {}, ""
        if name not in variables:
            return None, f"variable '{name}' is not declared"
        s = variables[name]["schema"]["openAPIV3Schema"]
        for i, field in enumerate(rest):
            props = s.get("properties")
            if props and field in props:
                s = props[field]
            elif isinstance(s.get("additionalProperties"), dict):
                s = s["additionalProperties"]
            else:
                return None, f"variable '{name}' has no field '{'.'.join(rest[:i + 1])}'"
        return s, ""

    def _check_op(self, where: str, ref: dict, raw: dict, op: dict, variables: dict) -> None:
        target = f"{ref['kind']}/{ref['name']} {op['op']} {op['path']}"
        sub = schema_at(raw, op["path"])
        if sub is None:
            self.problem(where, f"{target}: path does not exist in the {ref['kind']} schema")
            return
        if "value" in op:
            for err in fits(op["value"], sub):
                self.problem(where, f"{target}: value does not fit: {err}")
        vf = op.get("valueFrom", {})
        if "variable" in vf:
            vschema, reason = self._variable_schema(variables, vf["variable"])
            if vschema is None:
                self.problem(where, f"{target}: {reason}")
            elif vschema.get("type") and sub.get("type") and vschema["type"] != sub["type"]:
                self.problem(where, f"{target}: '{vf['variable']}' is {vschema['type']} "
                                    f"but the field is {sub['type']}")
        if "template" in vf:
            self._check_template_vars(f"{where}: {target}", vf["template"], variables)
            self._check_template_shape(f"{where}: {target}", vf["template"], sub)

    def _apply(self, where: str, ref: dict, obj: dict, op: dict, variables: dict) -> None:
        """Apply one JSON patch op to the working template, as RFC 6902 requires."""
        target = f"{ref['kind']}/{ref['name']} {op['op']} {op['path']}"
        parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
        parent: Any = obj
        for i, seg in enumerate(parts[:-1]):
            if isinstance(parent, dict) and seg in parent:
                parent = parent[seg]
            elif isinstance(parent, list) and seg.isdigit() and int(seg) < len(parent):
                parent = parent[int(seg)]
            else:
                missing = "/" + "/".join(parts[:i + 1])
                self.problem(where, f"{target}: {missing} does not exist in the template at this point "
                                    "(after the base template and earlier patches)")
                return
        last = parts[-1]
        value = self._placeholder(op, variables)
        if isinstance(parent, list):
            if last == "-":
                if op["op"] != "add":
                    self.problem(where, f"{target}: '-' only works with add")
                else:
                    parent.append(value)
                return
            if not last.isdigit():
                self.problem(where, f"{target}: '{last}' is not a list index")
                return
            idx = int(last)
            if op["op"] == "add" and idx <= len(parent):
                parent.insert(idx, value)
            elif op["op"] == "replace" and idx < len(parent):
                parent[idx] = value
            elif op["op"] == "remove" and idx < len(parent):
                del parent[idx]
            else:
                self.problem(where, f"{target}: index {idx} is out of range")
            return
        if not isinstance(parent, dict):
            self.problem(where, f"{target}: the parent is not an object or a list")
            return
        if op["op"] in ("replace", "remove") and last not in parent:
            self.problem(where, f"{target}: the field does not exist in the template at this point, "
                                "so JSON Patch cannot replace or remove it (use add)")
            return
        if op["op"] == "remove":
            del parent[last]
        else:
            parent[last] = value

    def _placeholder(self, op: dict, variables: dict) -> Any:
        if "value" in op:
            return copy.deepcopy(op["value"])
        vf = op.get("valueFrom", {})
        if "template" in vf:
            text = TEMPLATE_ACTION.sub("PLACEHOLDER", vf["template"])
            text = re.sub(r"^\s*PLACEHOLDER\s*$", "", text, flags=re.M)
            try:
                parsed = yaml.safe_load(text)
            except yaml.YAMLError:
                return "PLACEHOLDER"
            return parsed if parsed is not None else "PLACEHOLDER"
        schema, _ = self._variable_schema(variables, vf.get("variable", ""))
        return {"object": {}, "array": [], "integer": 1, "boolean": True}.get((schema or {}).get("type"), "PLACEHOLDER")

    def _check_template_vars(self, where: str, template: str, variables: dict) -> None:
        for action in TEMPLATE_ACTION.findall(template):
            for dotted in VAR_REF.findall(action):
                schema, reason = self._variable_schema(variables, dotted)
                if schema is None:
                    self.problem(where, f"template uses '.{dotted}': {reason}")

    def _check_template_shape(self, where: str, template: str, sub: dict) -> None:
        """Parse the template with each action replaced by a placeholder and check its keys."""
        text = TEMPLATE_ACTION.sub("PLACEHOLDER", template)
        text = re.sub(r"^\s*PLACEHOLDER\s*$", "", text, flags=re.M)  # control-only lines
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            self.problem(where, f"template is not valid YAML once rendered: {exc}")
            return
        for path in unknown_keys(value, sub, ""):
            self.problem(where, f"template produces unknown field {path}")

    # Cluster topology ---------------------------------------------------------------------

    def check_topology(self, where: str, ns: str, cluster: dict) -> None:
        topo = cluster["spec"]["topology"]
        name = topo["classRef"]["name"]
        class_ns = topo["classRef"].get("namespace") or ns
        cc = self.by_key.get(("cluster.x-k8s.io/v1beta2", "ClusterClass", class_ns, name))
        if cc is None:
            self.problem(where, f"its ClusterClass {class_ns}/{name} is not in the input "
                                "(pass the classes too, with their namespace)")
            return
        variables = {v["name"]: v for v in cc["spec"].get("variables", [])}
        given = {v["name"]: v["value"] for v in topo.get("variables", [])}
        for vname, v in variables.items():
            if v.get("required") and vname not in given:
                self.problem(where, f"required variable '{vname}' of {name} is not set")
        sets = [("topology.variables", given),
                ("controlPlane overrides",
                 {o["name"]: o["value"] for o in topo.get("controlPlane", {}).get("variables", {}).get("overrides", [])})]
        classes = {m["class"] for m in cc["spec"].get("workers", {}).get("machineDeployments", [])}
        for md in topo.get("workers", {}).get("machineDeployments", []):
            sets.append((f"machineDeployment {md['name']} overrides",
                         {o["name"]: o["value"] for o in md.get("variables", {}).get("overrides", [])}))
            if md["class"] not in classes:
                self.problem(where, f"machineDeployment {md['name']} uses class '{md['class']}', which {name} lacks")
        for label, values in sets:
            for vname, value in values.items():
                if vname not in variables:
                    self.problem(where, f"{label}: '{vname}' is not a variable of {name}")
                    continue
                for err in fits(value, variables[vname]["schema"]["openAPIV3Schema"]):
                    self.problem(where, f"{label}: variable '{vname}': {err}")


def unknown_keys(value: Any, raw: dict, path: str):
    if raw is None or raw == {} or raw.get("x-kubernetes-preserve-unknown-fields"):
        return
    if isinstance(value, dict):
        props = raw.get("properties")
        extra = raw.get("additionalProperties")
        for k, v in value.items():
            if props is not None and k in props:
                yield from unknown_keys(v, props[k], f"{path}/{k}")
            elif isinstance(extra, dict):
                yield from unknown_keys(v, extra, f"{path}/{k}")
            elif props is not None:
                yield f"{path}/{k}"
    elif isinstance(value, list) and isinstance(raw.get("items"), dict):
        for i, v in enumerate(value):
            yield from unknown_keys(v, raw["items"], f"{path}/{i}")


def read_docs(args: List[str]) -> List[Tuple[str, str, dict]]:
    docs = []
    for arg in args:
        path, _, default_ns = arg.partition(":")
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
        for doc in yaml.safe_load_all(text):
            if doc:
                ns = (doc.get("metadata") or {}).get("namespace") or default_ns or "default"
                docs.append((path, ns, doc))
    return docs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crds", action="append", required=True, type=Path, help="folder of CRD/component YAML files")
    ap.add_argument("--expect", action="append", default=[], metavar="KIND=N",
                    help="require exactly N objects of KIND in the input (repeatable)")
    ap.add_argument("files", nargs="+", metavar="FILE[:NAMESPACE]", help="manifest files, or - for stdin")
    args = ap.parse_args(argv)

    schemas = load_crds(args.crds)
    if not schemas:
        print("check_manifests: no CRDs found; run hack/fetch_crds.py first", file=sys.stderr)
        return 2
    docs = read_docs(args.files)
    checker = Checker(schemas, copy.deepcopy(docs))
    problems = checker.run()
    counts = Counter(d.get("kind") for _, _, d in docs)
    for spec in args.expect:
        kind, _, n = spec.partition("=")
        if counts.get(kind, 0) != int(n):
            problems.append(f"expected {n} {kind} object(s) in the input, found {counts.get(kind, 0)}")
    for p in problems:
        print(p)
    print(f"checked {len(docs)} objects ({', '.join(sorted(counts))}), "
          f"{checker.cel_skipped} CEL rules not evaluated: "
          f"{len(problems)} problem{'s' if len(problems) != 1 else ''}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
