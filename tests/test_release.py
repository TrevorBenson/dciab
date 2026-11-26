"""build/release.py and .github/workflows/release.yml (docs/RELEASING.md)."""

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))

import release  # noqa: E402
from release import ReleaseError  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.mark.parametrize("version,rname,prerelease", [
    ("1.0.0-alpha.1", "r1alpha1", "true"),
    ("1.0.0-alpha.12", "r1alpha12", "true"),
    ("1.0.0-beta.2", "r1beta2", "true"),
    ("1.0.0", "r1", "false"),
    ("3.0.0-alpha.1", "r3alpha1", "true"),
])
def test_plan_maps_version_to_name_tag_and_title(version, rname, prerelease):
    p = release.plan(version + "\n", "refs/heads/main", ["v0.9.0"])
    assert p["name"] == rname
    assert p["tag"] == f"v{version}"
    assert p["title"] == f"{rname} (v{version})"
    assert p["prerelease"] == prerelease
    assert p["archive"] == f"dciab-{version}.tar"


@pytest.mark.parametrize("version", ["1.1.0", "1.0.1", "1.0.0-rc.1", "1.0.0-alpha", "1.0.0-alpha.0",
                                     "0.0.0-alpha.1", "v1.0.0", "1.0.0-alpha.1+build", ""])
def test_plan_refuses_non_release_versions(version):
    with pytest.raises(ReleaseError, match="not a release form"):
        release.plan(version, "refs/heads/main", [])


def test_plan_refuses_other_branches_and_existing_tags():
    with pytest.raises(ReleaseError, match="from main"):
        release.plan("1.0.0-alpha.1", "refs/heads/lane-i/archive-release", [])
    with pytest.raises(ReleaseError, match="from main"):
        release.plan("1.0.0-alpha.1", "refs/tags/v1.0.0-alpha.1", [])
    with pytest.raises(ReleaseError, match=r"exists already.*1\.0\.0-alpha\.2"):
        release.plan("1.0.0-alpha.1", "refs/heads/main", ["v1.0.0-alpha.1"])


@pytest.mark.parametrize("version,stage,expected", [
    ("1.0.0-alpha.1", None, "1.0.0-alpha.2"),
    ("1.0.0-alpha.9", "alpha", "1.0.0-alpha.10"),
    ("1.0.0-alpha.3", "beta", "1.0.0-beta.1"),
    ("1.0.0-alpha.3", "ga", "1.0.0"),
    ("1.0.0-beta.1", None, "1.0.0-beta.2"),
    ("1.0.0-beta.4", "ga", "1.0.0"),
    ("1.0.0", None, "2.0.0-alpha.1"),   # no patch releases: fixes go to the next release
])
def test_next_version(version, stage, expected):
    assert release.next_version(version, stage) == expected


def test_next_version_never_goes_back():
    with pytest.raises(ReleaseError, match="go back"):
        release.next_version("1.0.0-beta.1", "alpha")


def test_repository_version_is_a_release_form():
    release.parse((ROOT / "VERSION").read_text())


def test_cli_plan_writes_github_outputs(tmp_path, capsys):
    out = tmp_path / "out"
    assert release.main(["plan", "--version", "1.0.0-beta.1", "--ref", "refs/heads/main",
                         "--existing-tag", "v1.0.0-alpha.3", "--github-output", str(out)]) == 0
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["title"] == "r1beta1 (v1.0.0-beta.1)" and lines["next"] == "1.0.0-beta.2"
    assert release.main(["plan", "--version", "1.0.0-beta.1", "--ref", "refs/heads/x"]) == 1
    assert "from main" in capsys.readouterr().err


def dist(tmp_path, size):
    d = tmp_path / "dist"
    d.mkdir()
    data = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
    (d / "dciab-1.0.0-alpha.1.tar").write_bytes(data)
    (d / "cosign.pub").write_text("pub\n")
    (d / "MANIFEST").write_text("m\n")
    return d, data


def test_assets_small_archive_is_attached_whole(tmp_path):
    d, data = dist(tmp_path, 1000)
    names = [p.name for p in release.assets(d, "dciab-1.0.0-alpha.1.tar")]
    assert names == ["MANIFEST", "cosign.pub", "dciab-1.0.0-alpha.1.tar", "dciab-1.0.0-alpha.1.tar.sha256"]
    assert (d / "release" / "dciab-1.0.0-alpha.1.tar.sha256").read_text() == \
        f"{hashlib.sha256(data).hexdigest()}  dciab-1.0.0-alpha.1.tar\n"


def test_assets_large_archive_is_split_and_rejoins(tmp_path):
    d, data = dist(tmp_path, 2500)
    paths = release.assets(d, "dciab-1.0.0-alpha.1.tar", limit=1000, part=1000)
    parts = [p for p in paths if ".part-" in p.name]
    assert [p.name for p in parts] == [f"dciab-1.0.0-alpha.1.tar.part-0{n}" for n in range(3)]
    assert b"".join(p.read_bytes() for p in parts) == data
    assert not (d / "dciab-1.0.0-alpha.1.tar").exists()
    assert hashlib.sha256(data).hexdigest() in (d / "release" / "dciab-1.0.0-alpha.1.tar.sha256").read_text()


def test_assets_exact_multiple_has_no_empty_part(tmp_path):
    d, data = dist(tmp_path, 2000)
    parts = [p for p in release.assets(d, "dciab-1.0.0-alpha.1.tar", limit=1000, part=1000) if ".part-" in p.name]
    assert len(parts) == 2 and all(p.stat().st_size == 1000 for p in parts)


def test_assets_needs_key_and_manifest(tmp_path):
    d, _ = dist(tmp_path, 10)
    (d / "cosign.pub").unlink()
    with pytest.raises(ReleaseError, match="cosign.pub is missing"):
        release.assets(d, "dciab-1.0.0-alpha.1.tar")


def test_asset_limit_is_under_githubs():
    assert release.PART < release.ASSET_LIMIT < 2 * 1024 ** 3


# ------------------------------------------------------------------ the workflow

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(wf):
    return wf.get("on", wf.get(True))   # PyYAML reads a bare `on:` key as True


def test_workflow_is_dispatch_only_and_dry_run_by_default(wf):
    on = triggers(wf)
    assert list(on) == ["workflow_dispatch"], "a release must never start on a push, merge or schedule"
    dry = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry["type"] == "boolean" and dry["default"] is True


def test_workflow_permissions(wf):
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert set(wf["jobs"]) == {"build", "sign-and-publish"}
    for name, job in wf["jobs"].items():
        expected = {"contents": "write"} if name == "sign-and-publish" else {"contents": "read"}
        assert job["permissions"] == expected, name


def test_build_job_never_sees_the_key_or_an_environment(wf):
    build = wf["jobs"]["build"]
    assert "environment" not in build, "the build job runs third-party tooling: no environment, no secrets"
    assert "secrets." not in yaml.safe_dump(build)
    for verb in ("gh release", "git tag", "git push", "/releases"):
        assert verb not in yaml.safe_dump(build), verb
    steps = {s.get("name"): s for s in build["steps"]}
    assert steps["Release public key present (real runs)"]["if"] == "${{ !inputs.dry_run }}"
    assert steps["Hand the unsigned archive to the signing job (real runs)"]["if"] == "${{ !inputs.dry_run }}"
    assert "--signing-key" not in steps["Package (unsigned)"]["run"]


def test_only_the_sign_step_of_real_runs_reads_the_key(wf):
    job = wf["jobs"]["sign-and-publish"]
    assert job["needs"] == "build"
    assert job["if"] == "${{ !inputs.dry_run }}"
    assert job["environment"] == "release", "one approval, on the one job that signs and publishes"
    with_secrets = [s["name"] for s in job["steps"] if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["Sign (the only step that reads the release key)"]
    sign = next(s for s in job["steps"] if s.get("name") == with_secrets[0])
    assert "build/package.py finalize" in sign["run"] and "umask 077" in sign["run"]
    publish = job["steps"][-1]["run"]
    assert "git/ref/tags/$TAG" in publish and "GITHUB_SHA" in publish


def test_every_action_is_pinned_by_sha(wf):
    import re
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            # The repository's own actions run from the same commit; others are pinned by SHA.
            if "uses" in step and not step["uses"].startswith("./.github/actions/"):
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), (name, step["uses"])
    text = WORKFLOW.read_text()
    assert "pip install" not in text and "setup-helm" not in text


def test_workflow_uses_the_plan_and_the_committed_key(wf):
    text = WORKFLOW.read_text()
    assert "build/release.py plan --ref \"$GITHUB_REF\"" in text
    assert "keys/dciab-release.pub" in text
    assert "dciab verify-archive --key" in text
    assert "build/release.py assets" in text


def test_notes_point_at_the_key_at_the_tag_with_its_fingerprint(tmp_path):
    key = tmp_path / "dciab-release.pub"
    key.write_bytes(b"-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n")
    fp = hashlib.sha256(key.read_bytes()).hexdigest()
    assert release.key_fingerprint(key) == fp
    text = release.notes("DCiaB r1alpha1", "r1alpha1", "dciab-1.0.0-alpha.1.tar", "o/dciab", key, split=True)
    assert "https://github.com/o/dciab/blob/r1alpha1/keys/dciab-release.pub" in text
    assert fp in text
    assert "cat dciab-1.0.0-alpha.1.tar.part-* > dciab-1.0.0-alpha.1.tar" in text
    assert "cosign verify-blob --key" in text
    # The trusted verifier is the primary path, and the cosign-only path also rejects unlisted files.
    assert text.index("./dciab verify-archive") < text.index("cosign verify-blob")
    assert "cut -c67- MANIFEST" in text and "nothing unlisted" in text
    assert "-path ./MANIFEST " in text and "-name MANIFEST" not in text
    whole = release.notes("DCiaB r1alpha1", "r1alpha1", "dciab-1.0.0-alpha.1.tar", "o/dciab", key, split=False)
    assert ".part-" not in whole
