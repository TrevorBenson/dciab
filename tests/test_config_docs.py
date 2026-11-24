"""The schema documents every key, and docs/CONFIG.md is rendered from it (hack/gen_config_docs.py)."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

SCHEMA = json.loads((ROOT / "schema" / "dciab.schema.json").read_text(encoding="utf-8"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "hack" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_config_docs")


def properties(node, path=""):
    """(path, schema) of every property the schema declares. A property listed as true in an
    if/then branch only allows a key declared elsewhere; if and not hold conditions, not
    declarations."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("if", "not"):
                continue
            if k == "properties" and isinstance(v, dict):
                for name, sub in v.items():
                    if isinstance(sub, dict):
                        yield f"{path}/{name}", sub
                    yield from properties(sub, f"{path}/{name}")
            else:
                yield from properties(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from properties(v, f"{path}[{i}]")


def test_every_property_has_a_description():
    missing = [p for p, s in properties(SCHEMA) if not str(s.get("description", "")).strip()]
    assert not missing, "add a description to each of these schema properties:\n" + "\n".join(missing)


def test_the_walk_sees_the_whole_schema():
    names = {p.rsplit("/", 1)[-1] for p, _ in properties(SCHEMA)}
    assert {"apiVersion", "replicaCount", "lbPool", "ironicIP", "clientSecret", "file", "env", "deviceName"} <= names


def test_config_reference_is_up_to_date():
    have = (ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    assert have == gen.render(SCHEMA), "docs/CONFIG.md is out of date: run python3 hack/gen_config_docs.py"


def test_render_is_deterministic():
    assert gen.render(SCHEMA) == gen.render(json.loads(json.dumps(SCHEMA)))


def test_every_property_has_a_row():
    text = (ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    keys = set(re.findall(r"^\| `([^`]+)` \|", text, re.M))
    leaves = {re.sub(r"\[\]$", "", k.rsplit(".", 1)[-1]) for k in keys}
    # spec itself is the page, and its blocks are its sections.
    leaves |= {"spec"} | set(re.findall(r"^## spec\.(\w+)$", text, re.M))
    # secretRef's file and env are a type, described in the Types section.
    declared = {p.rsplit("/", 1)[-1] for p, _ in properties(SCHEMA) if "/secretRef/" not in p}
    assert declared - leaves == set()


def test_check_mode_exit_codes(tmp_path, monkeypatch):
    out = ROOT / "docs" / "CONFIG.md"
    assert gen.main(["--check"]) == 0
    stale = tmp_path / "docs" / "CONFIG.md"
    stale.parent.mkdir()
    stale.write_text("stale\n", encoding="utf-8")
    (tmp_path / "schema").mkdir()
    (tmp_path / "schema" / "dciab.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen, "OUT", stale)
    monkeypatch.setattr(gen, "SCHEMA", tmp_path / "schema" / "dciab.schema.json")
    assert gen.main(["--check"]) == 1
    assert stale.read_text(encoding="utf-8") == "stale\n"
    assert out.is_file()


def test_section_links_resolve():
    for key, links in gen.SECTION_DOCS.items():
        assert key == "" or key in SCHEMA["properties"]["spec"]["properties"], key
        for href, _ in links:
            assert (ROOT / "docs" / href).is_file(), href


def test_schema_defaults_match_the_code():
    """A default in the schema is documentation; the code applies its own. They must agree."""
    import dciab_backup as bk
    import dciab_identity as idn

    defs = SCHEMA["$defs"]
    ident = defs["identity"]["properties"]
    assert ident["provider"]["default"] == idn.DEFAULT_PROVIDER
    assert ident["consumers"]["default"] == list(idn.DEFAULT_CONSUMERS)
    assert ident["external"]["properties"]["name"]["default"] == idn.DEFAULT_EXTERNAL_NAME
    assert defs["jwtIssuer"]["properties"]["claims"]["default"] == idn.DEFAULT_CLAIMS

    backup = defs["backup"]["properties"]
    for tier, settings in bk.DEFAULT_TIERS.items():
        props = backup["tiers"]["properties"][tier]["properties"]
        assert {k: props[k]["default"] for k in settings} == settings
    sched = backup["schedules"]["properties"]
    for tier, want in bk.DEFAULT_SCHEDULES["etcd"].items():
        assert sched["etcd"]["properties"][tier]["default"] == want
    assert sched["capiExport"]["default"] == bk.DEFAULT_SCHEDULES["capiExport"]
    for job, want in bk.DEFAULT_SCHEDULES["longhorn"].items():
        assert sched["longhorn"]["properties"][job]["default"] == want
    off = defs["offsiteTarget"]["properties"]
    assert off["retentionDays"]["default"] == bk.OFFSITE_RETENTION_DAYS
    assert off["everyDays"]["default"] == bk.REMOVABLE_EVERY_DAYS


def test_unknown_key_fix_lists_the_block_keys():
    """A block's description documents it; an unknown key's fix names what the block takes."""
    from dciab_validate.schema_check import check

    doc = {"apiVersion": "dciab.io/v1alpha1", "kind": "Site", "metadata": {"name": "dc1", "owner": "x"}}
    f = next(f for f in check(doc, SCHEMA) if f.path == ("metadata", "owner"))
    assert f.fix == "remove it, or check its spelling; this block takes name"


DOCS_WITH_LINKS = ["README.md", "GLOSSARY.md", "CONFIG.md", "COMPONENTS.md"]


@pytest.mark.parametrize("page", DOCS_WITH_LINKS)
def test_doc_links_resolve(page):
    path = ROOT / "docs" / page
    text = re.sub(r"`[^`\n]*`", "", path.read_text(encoding="utf-8"))  # code spans hold patterns, not links
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        file = target.split("#", 1)[0]
        if not (path.parent / file).exists():
            broken.append(target)
    assert not broken, f"docs/{page} links to missing files: {broken}"


def test_docs_index_links_every_doc():
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    pages = [p.relative_to(ROOT / "docs").as_posix() for p in (ROOT / "docs").rglob("*.md")
             if p.name != "README.md"]
    missing = [p for p in pages if f"({p})" not in index]
    assert not missing, f"docs/README.md doesn't link: {missing}"


def test_readme_links_the_docs_index():
    assert "(docs/README.md)" in (ROOT / "README.md").read_text(encoding="utf-8")
