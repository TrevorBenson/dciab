"""hack/redfish: the pins in bundle.lock redfishTesting and fetch.py's checks (no network)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
FETCH = ROOT / "hack" / "redfish" / "fetch.py"
OVERLAYS = ROOT / "tests" / "fixtures" / "redfish" / "overlays"


def test_pins_are_exact():
    t = LOCK["redfishTesting"]
    for key in ("mockupServer", "mockups"):
        assert t[key]["repo"].startswith("https://github.com/DMTF/")
        assert re.fullmatch(r"[0-9a-f]{40}", t[key]["commit"])
        assert t[key]["version"]
    assert t["mockups"]["names"]
    assert all(re.fullmatch(r"\d+(\.\d+)+", v) for v in t["mockupServer"]["pip"].values())


def test_mockup_server_requirements_are_hashed_and_match_the_pins():
    m = LOCK["redfishTesting"]["mockupServer"]
    text = (ROOT / m["requirements"]).read_text()
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.M))
    norm = {k.lower().replace("_", "-").replace(".", "-"): v for k, v in pins.items()}
    for name, version in m["pip"].items():
        assert norm.get(name.lower()) == version, f"{name} isn't pinned to {version} in {m['requirements']}"
    entries = re.split(r"\n(?=[A-Za-z0-9])", "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).strip())
    assert entries and all("--hash=sha256:" in e for e in entries)


def test_never_part_of_the_archive():
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import package
    finally:
        sys.path.pop(0)
    assert not any(s.startswith("hack/redfish") or s.startswith("tests") for s in package.SOURCES)


def test_overlays_are_json_and_use_known_file_names():
    files = list(OVERLAYS.rglob("*"))
    assert files
    for f in files:
        if f.is_file():
            assert f.name in ("index.json", "index.patch.json"), f
            json.loads(f.read_text())


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fake_repos(tmp_path):
    """Two local repositories standing in for DMTF's, each with a tag, and a bundle.lock copy that
    pins them."""
    repos = {}
    for name, files in (("server", {"redfishMockupServer.py": "#"}),
                        ("pubs", {"LICENSE.md": "BSD", "mockups/public-x/index.json": "{}", "mockups/public-y/index.json": "{}"})):
        d = tmp_path / name
        d.mkdir()
        git(d, "init", "-q", "-b", "main")
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        git(d, "add", ".")
        git(d, "commit", "-q", "-m", "x")
        git(d, "tag", "1.0")
        repos[name] = (f"file://{d}", git(d, "rev-parse", "HEAD"))
    root = tmp_path / "root"
    (root / "hack" / "redfish").mkdir(parents=True)
    (root / "hack" / "redfish" / "fetch.py").write_text(FETCH.read_text())

    def write_lock(server_commit=None, names=("public-x",)):
        lock = {"redfishTesting": {
            "mockupServer": {"repo": repos["server"][0], "version": "1.0", "commit": server_commit or repos["server"][1]},
            "mockups": {"repo": repos["pubs"][0], "version": "1.0", "commit": repos["pubs"][1], "names": list(names)}}}
        (root / "bundle.lock").write_text(yaml.safe_dump(lock))
    return root, write_lock


def run_fetch(root, out, cwd=None):
    return subprocess.run([sys.executable, str(root / "hack" / "redfish" / "fetch.py"), str(out)],
                          capture_output=True, text=True, cwd=cwd)


def test_fetch_checks_out_the_pinned_commits_and_reruns_cleanly(fake_repos, tmp_path):
    root, write_lock = fake_repos
    write_lock()
    out = tmp_path / "out"
    r = run_fetch(root, out)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(out)
    assert (out / "mockup-server" / "redfishMockupServer.py").is_file()
    assert (out / "mockups" / "public-x" / "index.json").is_file()
    assert run_fetch(root, out).returncode == 0


def test_a_rerun_with_more_mockup_names_checks_them_out(fake_repos, tmp_path):
    root, write_lock = fake_repos
    write_lock()
    out = tmp_path / "out"
    assert run_fetch(root, out).returncode == 0
    write_lock(names=("public-x", "public-y"))
    r = run_fetch(root, out)
    assert r.returncode == 0, r.stderr
    assert (out / "mockups" / "public-y" / "index.json").is_file()


def test_fetch_with_a_relative_directory_makes_links_that_resolve(fake_repos, tmp_path):
    root, write_lock = fake_repos
    write_lock()
    r = run_fetch(root, "rf", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "rf")
    assert (tmp_path / "rf" / "mockups" / "public-x" / "index.json").is_file()


def test_fetch_refuses_a_tag_that_isnt_the_pinned_commit(fake_repos, tmp_path):
    root, write_lock = fake_repos
    write_lock(server_commit="0" * 40)
    r = run_fetch(root, tmp_path / "out")
    assert r.returncode != 0
    assert "refusing it" in r.stderr
    assert not (tmp_path / "out" / "mockup-server").exists()
