"""images/lab-runner: the build plan, the job-started hook's refusals, and what the image wires up."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "images" / "lab-runner"
BUILD = HERE / "build.sh"
LIBEXEC = HERE / "rootfs" / "usr" / "libexec" / "dciab-lab-runner"
HOOK = LIBEXEC / "job-started.sh"
LOOP = LIBEXEC / "loop"
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
REPO = "TrevorBenson/dciab"


def plan(*args):
    return subprocess.run(["bash", str(BUILD), "--print", *args], capture_output=True, text=True)


def test_plan_shows_the_pins_and_the_base():
    r = plan()
    assert r.returncode == 0, r.stderr
    v = LOCK["labRunner"]["actionsRunner"]["version"]
    version = (ROOT / "VERSION").read_text().strip()
    assert f"lab runner {version}: actions runner {v}" in r.stdout
    assert f"https://github.com/actions/runner/releases/download/v{v}/actions-runner-linux-x64-{v}.tar.gz" in r.stdout
    base = LOCK["nodeImages"]["bootc"]["baseImage"] or f"localhost/dciab-rocky-bootc-base:{LOCK['nodeImages']['bootc']['manifest']}"
    assert f"base: {base}" in r.stdout
    assert f"-t localhost/dciab-lab-runner:{version} " in r.stdout
    assert "--type qcow2" in r.stdout  # the default disk
    assert LOCK["nodeImages"]["bootc"]["bootcImageBuilder"] in r.stdout


def test_plan_rejects_unknown_arguments():
    assert plan("--variant", "metal").returncode == 2


def test_actions_runner_is_pinned():
    r = LOCK["labRunner"]["actionsRunner"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", r["version"])
    assert re.fullmatch(r"[0-9a-f]{64}", r["sha256"])


def test_never_part_of_the_archive():
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import package
    finally:
        sys.path.pop(0)
    assert not any(s == "images" or s.startswith("images/lab-runner") for s in package.SOURCES)


@pytest.fixture(autouse=True)
def stub_bin(tmp_path, monkeypatch):
    """logger and pkill stubs that record their arguments, ahead of the real ones on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("logger", "pkill"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/{name}.calls"\n')
        stub.chmod(0o755)
    monkeypatch.setenv("STUB_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return tmp_path


def calls(stub_dir, name):
    f = stub_dir / f"{name}.calls"
    return f.read_text().splitlines() if f.exists() else []


def hook(**env):
    base = {"PATH": os.environ["PATH"], "DCIAB_LAB_REPO": REPO, "DCIAB_LAB_BRANCH": "main",
            "DCIAB_LAB_REFUSE_GRACE": "0",
            "GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/lab.yml@refs/heads/main"}
    base.update(env)
    return subprocess.run(["bash", str(HOOK)], capture_output=True, text=True,
                          env={k: v for k, v in base.items() if v is not None})


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_hook_accepts_the_branch(event):
    r = hook(GITHUB_EVENT_NAME=event)
    assert r.returncode == 0, r.stderr
    assert "accepted" in r.stdout


def test_hook_matches_the_repository_case_insensitively():
    assert hook(GITHUB_REPOSITORY=REPO.lower()).returncode == 0


@pytest.mark.parametrize("env, reason", [
    ({"GITHUB_EVENT_NAME": "pull_request"}, "event 'pull_request'"),
    ({"GITHUB_EVENT_NAME": "pull_request_target"}, "event 'pull_request_target'"),
    ({"GITHUB_EVENT_NAME": "workflow_run"}, "event 'workflow_run'"),
    ({"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/feature",
      "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/lab.yml@refs/heads/feature"}, "ref 'refs/heads/feature'"),
    ({"GITHUB_REF": "refs/tags/v1"}, "ref 'refs/tags/v1'"),
    ({"GITHUB_REPOSITORY": "someone/fork"}, "repository 'someone/fork'"),
    ({"GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/lab.yml@refs/heads/feature"}, "workflow"),
    ({"GITHUB_WORKFLOW_REF": "other/repo/.github/workflows/x.yml@refs/heads/main"}, "workflow"),
    ({"DCIAB_LAB_REPO": None}, "DCIAB_LAB_REPO is not set"),
    ({"GITHUB_EVENT_NAME": None}, "event ''"),
    ({"GITHUB_REF": None}, "ref ''"),
    ({"GITHUB_WORKFLOW_REF": None}, "workflow ''"),
])
def test_hook_refuses(env, reason):
    r = hook(**env)
    assert r.returncode == 1
    assert "refused" in r.stderr and reason in r.stderr


@pytest.mark.parametrize("branch", ["release", "Release"])
def test_hook_follows_the_configured_branch(branch):
    r = hook(DCIAB_LAB_BRANCH=branch, GITHUB_REF=f"refs/heads/{branch}",
             GITHUB_WORKFLOW_REF=f"{REPO.upper()}/.github/workflows/lab.yml@refs/heads/{branch}")
    assert r.returncode == 0, r.stderr
    assert hook(DCIAB_LAB_BRANCH=branch).returncode == 1  # main is now refused


def test_hook_keeps_branch_case():
    # Git branches are case-sensitive: Release is not release.
    r = hook(DCIAB_LAB_BRANCH="Release", GITHUB_REF="refs/heads/Release",
             GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/lab.yml@refs/heads/release")
    assert r.returncode == 1 and "workflow" in r.stderr


def test_refusal_kills_the_runner_before_any_step(stub_bin):
    """A failed hook alone still lets `if: always()` steps run, so a refusal kills the runner user's processes."""
    import getpass
    me = getpass.getuser()
    r = hook(GITHUB_EVENT_NAME="pull_request", DCIAB_LAB_RUNNER_USER=me)
    assert r.returncode == 1
    assert calls(stub_bin, "pkill") == [f"-KILL -u {me}"]
    assert any("refused job" in c for c in calls(stub_bin, "logger"))


def test_refusal_kills_nothing_as_another_user(stub_bin):
    r = hook(GITHUB_EVENT_NAME="pull_request")  # the runner user is dciab-runner, not the test's user
    assert r.returncode == 1
    assert calls(stub_bin, "pkill") == []


def test_acceptance_kills_nothing(stub_bin):
    import getpass
    assert hook(DCIAB_LAB_RUNNER_USER=getpass.getuser()).returncode == 0
    assert calls(stub_bin, "pkill") == []


def test_loop_runs_each_job_with_the_hook_as_the_runner_user():
    text = LOOP.read_text()
    run = text[text.index("runuser -u"):]
    assert 'ACTIONS_RUNNER_HOOK_JOB_STARTED="$HOOK"' in run
    assert 'DCIAB_LAB_REPO="$repo"' in run and 'DCIAB_LAB_BRANCH="$branch"' in run
    assert "env -i" in run  # nothing from root's environment reaches the job
    assert "HOOK=/usr/libexec/dciab-lab-runner/job-started.sh" in text


def test_loop_keeps_the_token_off_command_lines():
    text = LOOP.read_text()
    assert "-H @<(printf 'Authorization: Bearer %s\\n'" in text
    assert "generate-jitconfig" in text
    assert re.search(r"\^0\\ \[46\]00\$", text)  # root-owned, 0600 or 0400
    assert text.count("Authorization: Bearer") == 1  # one helper makes every API call


def test_loop_deletes_each_runner_registration():
    text = LOOP.read_text()
    after_run = text[text.index('"$STATE/runner/run.sh"'):]
    assert 'gh_api DELETE "/$runner_id"' in after_run
    assert ".runner.id" in text


def cleanup_body():
    text = LOOP.read_text()
    start = text.index("cleanup() {")
    return text[start:text.index("\n}\n", start)]


def test_loop_starts_every_round_clean():
    text = LOOP.read_text()
    body = text[text.index("while true; do"):]
    assert body.index("if ! cleanup; then") < body.index("mint")
    assert "not registering a runner" in body
    clean = cleanup_body()
    for step in ("pkill -KILL -u", "podman rm --all", "podman pod rm --all", "podman volume prune",
                 "podman network prune", "firewall-cmd --quiet --reload", "ip addr del", "ip link del",
                 "! -name 'systemd-private-*' -exec rm -rf {} +",
                 'rm -rf "${STATE:?}/runner" "${STATE:?}/home"', 'cp -a "$SRC" "$STATE/runner"'):
        assert step in clean, step
    assert "/var/cache/dciab" not in clean  # the download cache survives


def test_cleanup_failures_stop_the_round():
    clean = cleanup_body()
    assert "|| true" not in clean and "2>/dev/null" not in clean
    for line in clean.splitlines():
        if line.strip().startswith(("podman ", "rm ", "cp ", "chown ", "install ", "find ")):
            assert line.rstrip().endswith("|| return 1"), line
    assert "|| [[ $? == 1 ]] || return 1" in clean  # pkill: no processes is fine
    # The "nothing left" check must fail when podman can't list, not pass on empty output.
    assert 'left_containers="$(podman ps --all --quiet)" || return 1' in clean
    assert 'left_pods="$(podman pod ps --quiet)" || return 1' in clean


def test_loop_removes_network_state_added_after_boot():
    text = LOOP.read_text()
    assert 'comm -13 "$RUN/addrs" <(addrs)' in text and 'comm -13 "$RUN/dummies" <(dummies)' in text
    assert "RUN=/run/dciab-lab-runner" in text  # the baseline is per boot
    assert text.index("baseline\n") < text.index("while true; do")
    assert "export LC_ALL=C" in text


def test_image_wires_up_the_service_user_and_sudoers():
    cf = (HERE / "Containerfile").read_text()
    enable = next(line for line in cf.splitlines() if "systemctl enable" in line)
    assert "dciab-lab-runner.service" in enable
    assert "chmod 0440 /etc/sudoers.d/dciab-lab-runner" in cf
    assert "bootc container lint" in cf
    unit = (HERE / "rootfs/usr/lib/systemd/system/dciab-lab-runner.service").read_text()
    assert "ExecStart=/usr/libexec/dciab-lab-runner/loop" in unit
    assert "ConditionPathExists=/etc/dciab-lab-runner/runner.conf" in unit
    users = (HERE / "rootfs/usr/lib/sysusers.d/dciab-lab-runner.conf").read_text()
    assert re.search(r"^u dciab-runner .* /var/lib/dciab-lab-runner/home ", users, re.M)
    for f in (BUILD, HOOK, LOOP):
        assert os.access(f, os.X_OK), f


def test_ci_shellchecks_the_scripts():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    line = next(line for line in ci.splitlines() if "run: shellcheck" in line).split()
    for f in (BUILD, HOOK, LOOP):
        assert str(f.relative_to(ROOT)) in line


def test_root_grows_with_the_node_images_script():
    cf = (HERE / "Containerfile").read_text()
    assert "COPY images/node-bootc/rootfs/usr/libexec/dciab/growroot /usr/libexec/dciab/growroot" in cf
    assert "COPY images/node-bootc/rootfs/etc/cloud/cloud.cfg.d/90-dciab.cfg" in cf
    enable = next(line for line in cf.splitlines() if "systemctl enable" in line)
    assert "dciab-growroot" in enable.split()
    unit = (HERE / "rootfs/usr/lib/systemd/system/dciab-growroot.service").read_text()
    assert "RequiredBy=dciab-lab-runner.service" in unit and "kubelet" not in unit
    assert (ROOT / "images/node-bootc/rootfs/usr/libexec/dciab/growroot").is_file()


def lab_workflows():
    for f in sorted((ROOT / ".github/workflows").glob("*.yml")):
        wf = yaml.safe_load(f.read_text())
        jobs = wf.get("jobs", {})
        if any("self-hosted" in (j.get("runs-on") if isinstance(j.get("runs-on"), list) else [j.get("runs-on")])
               for j in jobs.values()):
            yield f, wf


def test_lab_workflows_follow_the_rules():
    found = list(lab_workflows())
    assert any(f.name == "lab-runner-smoke.yml" for f, _ in found)
    for f, wf in found:
        triggers = wf.get(True, wf.get("on"))  # PyYAML reads the key `on` as True
        names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
        assert not names & {"pull_request", "pull_request_target", "workflow_run"}, f.name
        assert wf.get("concurrency") == {"group": "dciab-lab", "cancel-in-progress": False}, f.name
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), f"{f.name}: pin {step['uses']} by SHA"


def test_smoke_leaves_state_that_the_next_run_checks():
    wf = yaml.safe_load((ROOT / ".github/workflows/lab-runner-smoke.yml").read_text())
    steps = {s.get("name"): s for s in wf["jobs"]["smoke"]["steps"]}
    check, leave = steps["Nothing left from an earlier job"]["run"], steps["Leave state for the next run to check"]["run"]
    for marker in ("/tmp/dciab-smoke-root", "/var/tmp/dciab-smoke-root", "dciab-smoke0", "192.0.2.1/32", "$HOME/.dciab-smoke"):
        assert marker in leave and marker.replace(".", "\\.") in check or marker in check, marker
    assert steps["Always runs (refused jobs must never get here)"]["if"] == "always()"
    assert "$PAUSE_IMAGE" in leave
