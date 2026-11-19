#!/usr/bin/env python3
"""Check Kubernetes manifests against the CRDs of the providers DCiaB pins.

    python3 hack/check_manifests.py --crds .cache/crds file.yaml [more.yaml | -]

For every object it checks the schema of its CRD, more strictly than the API server:
unknown fields are errors here, where the server would drop them silently.

For a ClusterClass it also checks that:
  - every templateRef names an object in the same input,
  - every patch selector picks a template the class references,
  - every JSON patch path exists in the target template's schema,
  - literal patch values fit the schema at that path,
  - variables used in patches are declared, and variable types fit their targets,
  - objects produced by template patches use only known fields.

For a Cluster that uses a ClusterClass in the same input, it checks the topology
variables: required ones are set, unknown ones are rejected, and values fit.

Exit status 0 when clean, 1 when anything is wrong.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from jsonschema import Draft7Validator

Key = Tuple[str, str]  # (apiVersion, kind)

CORE_GROUPS = {"v1", "apps/v1", "batch/v1", "rbac.authorization.k8s.io/v1", "networking.k8s.io/v1"}
TEMPLATE_ACTION = re.compile(r"{{(.*?)}}", re.S)
VAR_REF = re.compile(r"(?<![\w)\].])\.([A-Za-z_][A-Za-z0-9_]*)")


# --- CRD schemas ------------------------------------------------------------------------

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


def strict(schema: Any) -> Any:
    """OpenAPI v3 (Kubernetes flavour) -> JSON Schema draft 7, rejecting unknown fields."""
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k.startswith("x-kubernetes-") or k in ("nullable", "format", "x-metadata"):
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
            for e in Draft7Validator(strict(raw_schema)).iter_errors(value)]


# --- checks ------------------------------------------------------------------------------

class Checker:
    def __init__(self, schemas: Dict[Key, dict], docs: List[Tuple[str, dict]]):
        self.schemas = schemas
        self.docs = docs
        self.problems: List[str] = []
        self.by_name: Dict[Tuple[str, str, str], dict] = {}
        for _, d in docs:
            self.by_name[(d["apiVersion"], d["kind"], d["metadata"]["name"])] = d

    def problem(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")

    def run(self) -> List[str]:
        for src, doc in self.docs:
            where = f"{src}: {doc['kind']}/{doc['metadata'].get('name', '?')}"
            self.check_schema(where, doc)
            if doc["kind"] == "ClusterClass":
                self.check_clusterclass(where, doc)
            if doc["kind"] == "Cluster" and "topology" in doc.get("spec", {}):
                self.check_topology(where, doc)
        return self.problems

    def check_schema(self, where: str, doc: dict) -> None:
        key = (doc["apiVersion"], doc["kind"])
        if key not in self.schemas:
            if doc["apiVersion"] not in CORE_GROUPS:
                self.problem(where, f"no CRD found for {doc['apiVersion']} {doc['kind']}")
            return
        for e in Draft7Validator(strict(self.schemas[key])).iter_errors(doc):
            path = "/".join(str(p) for p in e.absolute_path)
            self.problem(where, f"{path}: {e.message}")

    # ClusterClass ---------------------------------------------------------------------

    def _refs(self, spec: dict) -> Dict[str, List[Tuple[dict, dict]]]:
        """Template refs grouped by how a patch selector can match them."""
        groups: Dict[str, List[Tuple[dict, dict]]] = {"controlPlane": [], "infrastructureCluster": []}
        cp = spec["controlPlane"]
        groups["controlPlane"].append((cp["templateRef"], {}))
        if "machineInfrastructure" in cp:
            groups["controlPlane"].append((cp["machineInfrastructure"]["templateRef"], {}))
        groups["infrastructureCluster"].append((spec["infrastructure"]["templateRef"], {}))
        for md in spec.get("workers", {}).get("machineDeployments", []):
            for part in ("bootstrap", "infrastructure"):
                groups.setdefault(f"md:{md['class']}", []).append((md[part]["templateRef"], {}))
        return groups

    def check_clusterclass(self, where: str, cc: dict) -> None:
        spec = cc["spec"]
        groups = self._refs(spec)
        for refs in groups.values():
            for ref, _ in refs:
                if (ref["apiVersion"], ref["kind"], ref["name"]) not in self.by_name:
                    self.problem(where, f"templateRef {ref['kind']}/{ref['name']} ({ref['apiVersion']}) "
                                        "is not in the input")
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
                    for op in d["jsonPatches"]:
                        self._check_op(pwhere, ref, raw, op, variables)

    def _select(self, sel: dict, groups) -> List[dict]:
        mr = sel["matchResources"]
        candidates: List[dict] = []
        if mr.get("controlPlane"):
            candidates += [r for r, _ in groups["controlPlane"]]
        if mr.get("infrastructureCluster"):
            candidates += [r for r, _ in groups["infrastructureCluster"]]
        for name in mr.get("machineDeploymentClass", {}).get("names", []):
            candidates += [r for r, _ in groups.get(f"md:{name}", [])]
        return [r for r in candidates if r["apiVersion"] == sel["apiVersion"] and r["kind"] == sel["kind"]]

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
            name = vf["variable"].split(".")[0]
            if name != "builtin" and name not in variables:
                self.problem(where, f"{target}: variable '{name}' is not declared")
            elif name in variables and "." not in vf["variable"]:
                vtype = variables[name]["schema"]["openAPIV3Schema"].get("type")
                ttype = sub.get("type")
                if vtype and ttype and vtype != ttype:
                    self.problem(where, f"{target}: variable '{name}' is {vtype} but the field is {ttype}")
        if "template" in vf:
            self._check_template_vars(f"{where}: {target}", vf["template"], variables)
            self._check_template_shape(f"{where}: {target}", vf["template"], sub)

    def _check_template_vars(self, where: str, template: str, variables: dict) -> None:
        for action in TEMPLATE_ACTION.findall(template):
            for name in VAR_REF.findall(action):
                if name != "builtin" and name not in variables:
                    self.problem(where, f"template uses '.{name}', which is not a declared variable")

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

    # Cluster topology -------------------------------------------------------------------

    def check_topology(self, where: str, cluster: dict) -> None:
        topo = cluster["spec"]["topology"]
        name = topo["classRef"]["name"]
        cc = next((d for _, d in self.docs if d["kind"] == "ClusterClass" and d["metadata"]["name"] == name), None)
        if cc is None:
            return  # class not part of this input
        variables = {v["name"]: v for v in cc["spec"].get("variables", [])}
        given = {v["name"]: v["value"] for v in topo.get("variables", [])}
        for vname, v in variables.items():
            if v.get("required") and vname not in given:
                self.problem(where, f"required variable '{vname}' of {name} is not set")
        sets = [("topology.variables", given)]
        sets.append(("controlPlane overrides",
                     {o["name"]: o["value"] for o in topo.get("controlPlane", {}).get("variables", {}).get("overrides", [])}))
        for md in topo.get("workers", {}).get("machineDeployments", []):
            sets.append((f"machineDeployment {md['name']} overrides",
                         {o["name"]: o["value"] for o in md.get("variables", {}).get("overrides", [])}))
            if md["class"] not in {m["class"] for m in cc["spec"].get("workers", {}).get("machineDeployments", [])}:
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


def read_docs(paths: List[str]) -> List[Tuple[str, dict]]:
    docs = []
    for p in paths:
        text = sys.stdin.read() if p == "-" else Path(p).read_text()
        for doc in yaml.safe_load_all(text):
            if doc:
                docs.append((p, doc))
    return docs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crds", action="append", required=True, type=Path, help="folder of CRD/component YAML files")
    ap.add_argument("files", nargs="+", help="manifest files, or - for stdin")
    args = ap.parse_args(argv)

    schemas = load_crds(args.crds)
    if not schemas:
        print("check_manifests: no CRDs found; run hack/fetch_crds.py first", file=sys.stderr)
        return 2
    docs = read_docs(args.files)
    problems = Checker(schemas, copy.deepcopy(docs)).run()
    for p in problems:
        print(p)
    kinds = sorted({d["kind"] for _, d in docs})
    print(f"checked {len(docs)} objects ({', '.join(kinds)}): "
          f"{len(problems)} problem{'s' if len(problems) != 1 else ''}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
