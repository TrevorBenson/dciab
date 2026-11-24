"""Release versioning (docs/VERSIONING.md): VERSION is the SemVer form of an rN release name,
and only GA tags (vN.0.0) freeze ClusterClass revisions."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hack" / "check_frozen_classes.sh"
SEMVER = re.compile(r"^(\d+)\.0\.0(?:-(alpha|beta)\.(\d+))?$")


def release_name(version: str) -> str:
    """rN name for a VERSION value, as docs/VERSIONING.md maps them."""
    m = SEMVER.fullmatch(version)
    assert m, f"{version!r} is not N.0.0, N.0.0-alpha.M or N.0.0-beta.M"
    major, stage, n = m.groups()
    return f"r{major}{stage or ''}{n or ''}"


@pytest.mark.parametrize("version,name", [
    ("1.0.0-alpha.1", "r1alpha1"), ("1.0.0-alpha.12", "r1alpha12"),
    ("1.0.0-beta.2", "r1beta2"), ("1.0.0", "r1"), ("2.0.0-alpha.1", "r2alpha1"),
])
def test_release_name_mapping(version, name):
    assert release_name(version) == name


@pytest.mark.parametrize("version", ["0.1.0", "1.1.0", "1.0.1", "1.0.0-rc.1", "1.0.0-alpha"])
def test_versions_outside_the_scheme(version):
    assert not SEMVER.fullmatch(version)


def test_version_file_is_a_release():
    release_name((ROOT / "VERSION").read_text().strip())


def test_schema_accepts_every_release_form():
    schema = json.loads((ROOT / "schema" / "dciab.schema.json").read_text())
    pattern = re.compile(schema["properties"]["spec"]["properties"]["bundleVersion"]["pattern"])
    for v in ("1.0.0-alpha.1", "1.0.0-beta.3", "1.0.0", "2.0.0-alpha.10"):
        assert pattern.search(v), v
    for v in ("1.0.0-rc.1", "r1alpha1", "v1.0.0"):
        assert not pattern.search(v), v


def test_examples_match_version():
    version = (ROOT / "VERSION").read_text().strip()
    for f in sorted((ROOT / "examples").glob("*.yaml")):
        doc = yaml.safe_load(f.read_text())
        if isinstance(doc, dict) and "bundleVersion" in (doc.get("spec") or {}):
            assert doc["spec"]["bundleVersion"] == version, f.name


# --- check_frozen_classes.sh: alpha and beta tags freeze nothing, GA does ---

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    (r / "charts/capi-clusterclasses/classes/r1").mkdir(parents=True)
    git(r.parent, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "charts/capi-clusterclasses/classes/r1/proxmox.yaml").write_text("a: 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "r1 draft")
    return r


def edit_class_and_check(repo):
    (repo / "charts/capi-clusterclasses/classes/r1/proxmox.yaml").write_text("a: 2\n")
    git(repo, "commit", "-qam", "edit r1")
    return subprocess.run(["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("tag", ["v1.0.0-alpha.1", "v1.0.0-beta.2"])
def test_prerelease_tags_freeze_nothing(repo, tag):
    git(repo, "tag", tag)
    out = edit_class_and_check(repo)
    assert out.returncode == 0, out.stdout + out.stderr


def test_ga_tag_freezes_the_revision(repo):
    git(repo, "tag", "v1.0.0")
    out = edit_class_and_check(repo)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "released in v1.0.0" in out.stdout
