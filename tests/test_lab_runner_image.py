"""The workflow that builds and publishes the lab runner image, and the scripts around it."""
import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows/lab-runner-image.yml"


def wf():
    return yaml.safe_load(WF.read_text())


def test_it_builds_on_the_lab_runner_and_never_runs_for_a_pull_request():
    """A hosted Ubuntu runner can't build the base (nested rpm-ostree compose fails with
    'cannot open /etc/gshadow'); the lab runner is Rocky 10 with SELinux. The job-started
    hook refuses pull requests, so the workflow must not offer that trigger."""
    w = wf()
    job = w["jobs"]["image"]
    assert job["runs-on"] == ["self-hosted", "dciab-lab"]
    triggers = w[True]
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert "images/lab-runner/**" in triggers["push"]["paths"] and "images/node-bootc/**" in triggers["push"]["paths"]
    assert w["concurrency"] == {"group": "dciab-lab", "cancel-in-progress": False}
    # The runner image has its own python with PyYAML; setup-python is for Ubuntu runners.
    assert "setup-python" not in WF.read_text()


def test_only_the_job_can_write_packages_and_only_main_publishes():
    w = wf()
    assert w["permissions"] == {"contents": "read"}
    job = w["jobs"]["image"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    publish = next(s for s in job["steps"] if s.get("name", "").startswith("Publish"))
    assert publish["if"] == "github.ref == 'refs/heads/main'"
    # Every step that logs in or pushes is the publish step.
    for step in job["steps"]:
        if step is not publish:
            assert "login" not in step.get("run", "") and "push" not in step.get("run", ""), step.get("name", "")


def test_actions_are_pinned_by_sha_because_the_job_can_write_packages():
    for step in wf()["jobs"]["image"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_a_tag_is_published_once_and_both_digests_are_reported():
    text = WF.read_text()
    assert "already published" in text and "skopeo inspect" in text and "manifest fetch" in text
    assert "--digestfile" in text and "disk.qcow2.sha256" in text
    assert "dciab-lab-runner-disk" in text and "bundle.lock" in text
    assert "GITHUB_STEP_SUMMARY" in text


def test_it_builds_the_base_then_the_image_in_that_order():
    steps = [s.get("run", "") for s in wf()["jobs"]["image"]["steps"]]
    base = next(i for i, r in enumerate(steps) if "node-bootc/build.sh --base-only" in r)
    lab = next(i for i, r in enumerate(steps) if "lab-runner/build.sh --type qcow2" in r)
    assert base < lab


def test_node_build_has_a_base_only_mode_that_stops_before_the_node_image():
    text = (ROOT / "images/node-bootc/build.sh").read_text()
    assert "--base-only) base_only=true" in text
    assert text.index('if [[ "$base_only" == true ]]') < text.index('echo "== node: $tag"')
    plan = subprocess.run([str(ROOT / "images/node-bootc/build.sh"), "--print"], capture_output=True, text=True, check=True).stdout
    assert "base:" in plan  # the plan is unchanged by the new flag


def test_image_id_is_a_version_and_eight_hex_digits():
    out = subprocess.run([str(ROOT / "hack/lab/runner-image-id.sh")], capture_output=True, text=True, check=True).stdout.strip()
    assert re.fullmatch(re.escape((ROOT / "VERSION").read_text().strip()) + r"-[0-9a-f]{8}", out)


def test_fetch_script_needs_a_tag_and_oras():
    script = str(ROOT / "hack/lab/fetch-runner-disk.sh")
    no_tag = subprocess.run(["bash", script], capture_output=True, text=True)
    assert no_tag.returncode != 0 and "usage" in no_tag.stderr
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}  # no oras here
    if subprocess.run(["bash", "-c", "command -v oras"], env=env, capture_output=True).returncode != 0:
        no_oras = subprocess.run(["bash", script, "0.1.0-x"], capture_output=True, text=True, env=env)
        assert no_oras.returncode == 1 and "oras is not on PATH" in no_oras.stderr


def test_ci_shellchecks_the_new_scripts():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "hack/lab/runner-image-id.sh" in ci and "hack/lab/fetch-runner-disk.sh" in ci


def test_the_published_base_is_pushed_once_per_recipe_commit():
    publish = next(s for s in wf()["jobs"]["image"]["steps"] if s.get("name", "").startswith("Publish"))
    run = publish["run"]
    assert "dciab-rocky-bootc-base:$manifest-$commit" in run
    assert "skopeo inspect" in run.split("dciab-rocky-bootc-base:$manifest-$commit", 1)[1].split("podman push", 1)[0]


def test_publish_uses_oras_from_the_runner_and_checks_each_artifact_alone():
    """Run 45319946414 pushed the image, then failed on a hosted-runner oras path; a rerun that
    skipped everything because the image existed would never have pushed the disk."""
    publish = next(s for s in wf()["jobs"]["image"]["steps"] if s.get("name", "").startswith("Publish"))["run"]
    assert "images/lab-runner/build/bin/oras" not in publish
    assert 'oras=oras' in publish
    assert 'skopeo inspect --raw "docker://$image"' in publish
    assert '"$oras" manifest fetch "$disk"' in publish
    assert "exit 0" not in publish


def test_a_pinned_base_is_not_republished():
    publish = next(s for s in wf()["jobs"]["image"]["steps"] if s.get("name", "").startswith("Publish"))["run"]
    assert 'podman image exists "localhost/dciab-rocky-bootc-base:$manifest"' in publish
