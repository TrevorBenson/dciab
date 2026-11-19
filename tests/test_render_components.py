import importlib.util
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "hack" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("render_components")
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
CRDS = _load("fetch_crds").cache_dir(LOCK)


def test_both_default_forms_and_explicit_values():
    text, missing = rc.substitute("a=${A:=1} b=${B=\"\"} c=${C} d=${D:=x}", {"C": "3", "D": "set"})
    assert text == 'a=1 b="" c=3 d=set'
    assert missing == []


def test_missing_values_are_reported():
    _, missing = rc.substitute("${NEEDED} ${ALSO}", {})
    assert missing == ["NEEDED", "ALSO"]


def test_environment_overrides_lock_values():
    values = rc.values_from(LOCK, {"VSPHERE_USERNAME": "from-env", "UNRELATED": "x"})
    assert values["VSPHERE_USERNAME"] == "from-env"
    assert "UNRELATED" not in values


def test_cluster_topology_is_enabled_everywhere():
    values = rc.values_from(LOCK, {})
    assert values["CLUSTER_TOPOLOGY"] == "true" and values["ClusterTopology"] == "true"


@pytest.mark.skipif(not CRDS.is_dir() and not os.environ.get("CI"), reason="run hack/fetch_crds.py first")
def test_every_pinned_component_renders_completely():
    values = rc.values_from(LOCK, {})
    files = [p for pattern in ("cluster-api-*/*.yaml", "infrastructure-*/*.yaml", "ipam-*/*.yaml")
             for p in CRDS.glob(pattern)]
    assert files
    for f in files:
        text, missing = rc.substitute(f.read_text(), values)
        assert missing == [], f"{f.name}: {missing}"
        assert not rc.VARIABLE.search(text), f.name
        assert re.findall(r"ClusterTopology=(\w+)", text) in ([], ["true"]), f.name
