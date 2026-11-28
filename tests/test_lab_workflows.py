"""The lab CI workflows (.github/workflows/lab*.yml) and their scripts (hack/lab/)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows"
REUSABLE = ["lab-template.yml", "lab-cluster.yml", "lab-mgmt.yml"]
SCRIPTS = ["pve.sh", "check-token.sh", "template.sh", "cluster.sh", "mgmt.sh"]


def load(name):
    return yaml.safe_load((WF / name).read_text())


def triggers(wf):
    t = wf.get(True, wf.get("on"))
    return set(t) if isinstance(t, (dict, list)) else {t}


@pytest.mark.parametrize("name", REUSABLE)
def test_reusable_workflows_run_on_the_lab_in_the_write_environment(name):
    wf = load(name)
    assert triggers(wf) == {"workflow_call"}
    for job in wf["jobs"].values():
        assert job["runs-on"] == ["self-hosted", "dciab-lab"]
        assert job["environment"] == "lab-write"
        assert "timeout-minutes" in job
        assert wf["permissions"] == {"contents": "read"}


def test_entry_chains_the_stages_in_order_without_overlap():
    jobs = load("lab.yml")["jobs"]
    assert list(jobs) == ["template", "cluster", "mgmt"]
    assert jobs["template"]["uses"] == "./.github/workflows/lab-template.yml"
    assert jobs["cluster"]["needs"] == ["template"]
    assert jobs["mgmt"]["needs"] == ["template", "cluster"]
    for name in ("cluster", "mgmt"):
        # A stage still runs when an earlier one was skipped (an existing template), not when it failed.
        assert "needs.template.result != 'failure'" in jobs[name]["if"] and "!cancelled()" in jobs[name]["if"]
        assert "inputs.template-name" in jobs[name]["with"]["template-name"]


def test_cleanup_always_runs_and_nothing_interpolates_inputs_into_scripts():
    for name in ("lab-cluster.yml", "lab-mgmt.yml"):
        steps = load(name)["jobs"][name.split("-")[1].split(".")[0]]["steps"]
        assert steps[-1]["if"] == "always()" and steps[-1]["name"] == "Clean up"
    for name in REUSABLE:
        for job in load(name)["jobs"].values():
            for step in job["steps"]:
                assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')} puts an expression in run:"


def test_token_check_action_wraps_the_script():
    action = yaml.safe_load((ROOT / ".github/actions/lab-pve/action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert action["runs"]["steps"][-1]["run"] == "hack/lab/check-token.sh"
    assert all("${{" not in step["run"] for step in action["runs"]["steps"])
    assert set(action["inputs"]) == {"url", "fingerprint", "token-id", "token-secret"}


def test_environments_match_what_the_lab_has():
    # handoff/LAB.md: lab-write holds the publisher, capmox and janitor tokens; both have the variables.
    text = "\n".join((WF / n).read_text() for n in REUSABLE)
    for secret in ("PVE_TOKEN_ID", "PVE_TOKEN_SECRET", "CAPMOX_TOKEN_ID", "CAPMOX_TOKEN_SECRET"):
        assert f"secrets.{secret}" in text
    assert "vars.PVE_URL" in text and "vars.PVE_TLS_FINGERPRINT" in text


def test_scripts_exist_are_executable_and_shellchecked_by_ci():
    ci = (WF / "ci.yml").read_text()
    for s in SCRIPTS:
        path = ROOT / "hack/lab" / s
        assert path.is_file(), s
        if s != "pve.sh":  # pve.sh is sourced, never run
            assert path.stat().st_mode & 0o111, s
            assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
        assert f"hack/lab/{s}" in ci, f"{s} is not in CI's shellcheck list"


def test_scripts_never_use_ssh_to_proxmox_or_put_secrets_on_command_lines():
    for s in SCRIPTS:
        text = (ROOT / "hack/lab" / s).read_text()
        assert not re.search(r"ssh[^\n]*root@", text) and "qm " not in re.sub(r"#.*", "", text), s
        for line in text.splitlines():
            if "PVE_TOKEN_SECRET" in line and not line.lstrip().startswith("#"):
                assert "printf" in line or ":?" in line or "add-mask" in line or "export" in line, f"{s}: {line}"
    # The seed's token reaches root through the environment.
    assert "--preserve-env=DCIAB_PVE_TOKEN" in (ROOT / "hack/lab/mgmt.sh").read_text()


def test_masking_only_happens_inside_actions():
    # Outside Actions the ::add-mask:: line would print the secret.
    assert '[[ "${GITHUB_ACTIONS:-}" == true ]] && echo "::add-mask::' in (ROOT / "hack/lab/pve.sh").read_text()


def test_the_lab_network_file_has_what_the_scripts_read():
    n = yaml.safe_load((ROOT / "handoff/lab/network.yaml").read_text())
    assert {"proxmox", "network", "vmid", "testVM", "clusters", "site"} <= set(n)
    assert {"node", "storage", "import_storage", "bridge"} <= set(n["proxmox"])
    assert n["clusters"]["lab01"] == n["clusters"]["mgmt"]  # one lab: these never run together
    assert n["vmid"]["template"]["last"] < n["vmid"]["test"]["first"] and n["vmid"]["test"]["last"] < 9900
    assert n["vmid"]["template"]["first"] >= 900  # hand-made VMs stay below
    # lab_get reads it from the shell.
    out = subprocess.run(["bash", "-c", ". hack/lab/pve.sh; lab_get network.gateway; lab_get vmid.test.first"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert out == [n["network"]["gateway"], str(n["vmid"]["test"]["first"])]


def test_mgmt_run_is_also_the_token_only_end_to_end():
    """Task 0008 (#212): MGMT's token is the only credential, checked live before seed,
    CAPMOX's logs checked for permission failures, and nothing left behind after cleanup."""
    steps = load("lab-mgmt.yml")["jobs"]["mgmt"]["steps"]
    runs = [s.get("run", "") for s in steps]
    order = ["hack/lab/mgmt.sh token", "hack/lab/mgmt.sh seed", "hack/lab/mgmt.sh rerun",
             "hack/lab/mgmt.sh denials", "hack/lab/mgmt.sh down"]
    assert [r for r in runs if r in order] == order
    denials = next(s for s in steps if s.get("run") == "hack/lab/mgmt.sh denials")
    assert denials["if"] == "${{ !cancelled() }}"  # the evidence matters most after a failure
    script = (ROOT / "hack/lab/mgmt.sh").read_text()
    down = script[script.index("\ndown() {"):]
    assert "\n  leftovers\n}" in down[:down.index("\n}\n") + 3]
    assert "--live --output json" in script
    # Only the CAPMOX token reaches dciab: the site config carries no other Proxmox credential.
    assert 'px["credentials"]["tokenID"] = os.environ["CAPMOX_TOKEN_ID"]' in script


def _live_report(tmp_path, token, *, missing=None, broad=None, confirmed=True, other_error=False):
    """A real `dciab validate --live --output json` report, built with live.Results."""
    sys.path.insert(0, str(ROOT / "lib"))
    from dciab_validate import findings, live
    r = live.Results()
    base = ("spec", "platform", "proxmox")
    if missing:
        r.error(base + ("credentials", "tokenID"), f"the token lacks {missing}", "grant it")
    if broad:
        r.warn(base + ("credentials", "tokenID"), f"the token can do more than DCiaB needs: {broad}", "remove it")
    if confirmed and not missing:
        r.note(f"token {token} has every privilege docs/PROXMOX.md lists")
    if other_error:
        r.error(("spec", "time", "servers"), "pool.ntp.org did not answer", "check NTP")
    path = tmp_path / "live.json"
    path.write_text(findings.render_json("dciab.yaml", r.findings, r.notes))
    return path


@pytest.mark.parametrize("case,ok", [
    ({}, True),
    ({"other_error": True}, True),            # unrelated live findings don't fail the token check
    ({"missing": "VM.Allocate on /vms"}, False),
    ({"broad": "Sys.Modify on /"}, False),
    ({"confirmed": False}, False),            # the Proxmox check stopped before confirming
])
def test_token_findings_decides_on_the_token_alone(tmp_path, case, ok):
    token = "capmox@pve!dciab"
    report = _live_report(tmp_path, token, **case)
    r = subprocess.run([sys.executable, str(ROOT / "hack/lab/token_findings.py"), str(report), token],
                       capture_output=True, text=True)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr


def test_token_findings_without_a_report_fails_clearly(tmp_path):
    empty = tmp_path / "live.json"
    empty.write_text("")
    r = subprocess.run([sys.executable, str(ROOT / "hack/lab/token_findings.py"), str(empty), "t@pve!x"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "wrote no report" in r.stderr and "Traceback" not in r.stderr


def test_denial_pattern_matches_proxmox_refusals_only(tmp_path):
    script = (ROOT / "hack/lab/mgmt.sh").read_text()
    pattern = re.search(r"^DENIAL='([^']+)'$", script, re.M).group(1)
    hits = ['error: 403 Permission check failed (/vms/110, VM.Config.Disk)',
            'unable to clone: 403 Forbidden',
            'request failed: status code: 403']
    misses = ['resourceVersion "403" changed', 'took 0.403s', 'Reconciling ProxmoxMachine mgmt-cp-403',
              'machines.cluster.x-k8s.io is forbidden: User "x" cannot list']
    for line in hits + misses:
        found = subprocess.run(["grep", "-qiE", pattern], input=line, text=True).returncode == 0
        assert found is (line in hits), line
    # Thousands of matches are still found (no head/pipefail race).
    log = tmp_path / "capmox.log"
    log.write_text("403 Permission check failed (/vms/1, VM.Allocate)\n" * 5000)
    assert subprocess.run(["bash", "-o", "pipefail", "-c", f"grep -qiE '{pattern}' {log}"]).returncode == 0


def test_denials_keeps_capmox_logs_a_drain_would_lose_and_fails_on_lookup_errors():
    script = (ROOT / "hack/lab/mgmt.sh").read_text()
    checks = script[script.index("\nchecks() {"):]
    checks = checks[:checks.index("\n}\n")]
    # MGMT's CAPMOX logs are saved before the replacement deletes a Machine (its drain can evict the pod).
    assert checks.index("save_capmox_logs mgmt") < checks.index("delete machine")
    save = script[script.index("\nsave_capmox_logs() {"):]
    save = save[:save.index("\n}\n")]
    assert "--ignore-not-found" in save and "|| return 1" in save  # absent != lookup failed
    denials = script[script.index("\ndenials() {"):]
    denials = denials[:denials.index("\n}\n")]
    assert '"$LAB_DIR"/capmox-*.log' in denials and 'lab_die "can\'t read' in denials
    seed = script[script.index("\nseed() {"):]
    assert 'rm -f "$LAB_DIR"/capmox-*.log' in seed[:seed.index("\n}\n")]


def test_lab_yml_lets_the_reusable_workflows_read_lab_write_secrets():
    """Without secrets: inherit, a called workflow's job deployed to lab-write gets the
    environment's variables but empty secrets (lab runs 45745673160, 45751385186)."""
    calls = [job for job in load("lab.yml")["jobs"].values() if "uses" in job]
    assert len(calls) == 3
    for job in calls:
        assert job.get("secrets") == "inherit", job["uses"]


def test_lab_pve_action_names_empty_inputs_before_running_the_check():
    action = yaml.safe_load((ROOT / ".github/actions/lab-pve/action.yml").read_text())
    steps = action["runs"]["steps"]
    assert "::error::lab-pve: input" in steps[0]["run"]
    assert steps[-1]["run"] == "hack/lab/check-token.sh"


def _bash(script: str) -> str:
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _functions(*names: str) -> str:
    text = (ROOT / "hack" / "lab" / "template.sh").read_text()
    out = []
    for name in names:
        start = text.index(f"\n{name}() {{") + 1
        out.append(text[start:text.index("\n}\n", start) + 3])
    return "".join(out)


FAKE_PVE = r'''
set -euo pipefail
PVE_NODE=pve; Q=/nodes/pve/qemu
lab_log() { echo "== $*"; }
lab_get() { case "$1" in vmid.template.first) echo 9000;; vmid.template.last) echo 9099;; esac; }
pve() {
  case "$1 $2" in
    "GET /cluster/resources?type=vm") echo '[{"vmid":9001,"node":"pve","name":"dciab-node-aaaaaaaaaaaa","template":1},
      {"vmid":9003,"node":"pve","template":1},{"vmid":9002,"node":"pve","name":"dciab-node-bbbbbbbbbbbb","template":0},
      {"vmid":9004,"node":"other","template":1},{"vmid":100,"node":"pve","template":1},
      {"vmid":9000,"node":"pve","name":"dciab-node-bbbbbbbbbbbb","template":0}]';;
    "GET /nodes/pve/qemu/9000/config") echo '{"description":"someone else'"'"'s","template":0}';;
    "GET /nodes/pve/qemu/9001/config") echo '{"description":"DCiaB node template (dciab templates). Do not edit.\nsha256: aaaa\nfrom: x","template":1}';;
    "GET /nodes/pve/qemu/9003/config") echo '{"description":"DCiaB node template (dciab templates). Do not edit.\nsha256: cccc\n","template":1}';;
    "GET /nodes/pve/qemu/9002/config") echo '{"description":"DCiaB node template (dciab templates). Do not edit.\nsha256: bbbb\n","template":0}';;
    "GET /nodes/pve/qemu/9004/config"|"GET /nodes/pve/qemu/100/config") echo '{"description":"sha256: cccc"}';;
    "DELETE "*) echo "DELETED $2" >&2; echo UPID;;
    *) return 1;;
  esac
}
task() { :; }
'''


def test_lab_template_reuses_the_template_of_the_same_disk():
    # PR #280 review: every run took a new VMID, and a second template with the same name could follow.
    script = FAKE_PVE + _functions("find_disk_template") + '''
echo "a=$(find_disk_template aaaa)"; echo "b=$(find_disk_template bbbb)"; echo "c=$(find_disk_template cccc)"
echo "d=$(find_disk_template dddd)"'''
    out = _bash(script).split()
    # Matched by the description's sha256 (a lagging /cluster/resources has no name for a new one),
    # in CI's range on the node only.
    assert out == ["a=9001", "1", "b=9002", "0", "c=9003", "1", "d="]


def test_lab_template_cleans_up_what_it_made_on_any_failure():
    # PR #280 review: a failure after the template existed (the final name lookup) skipped cleanup.
    cases = {"made": ("PUB_VMID=9001; PUB_REUSED=0; PUB_DONE=0", True),
             "reused": ("PUB_VMID=9001; PUB_REUSED=1; PUB_DONE=0", False),
             "done": ("PUB_VMID=9001; PUB_REUSED=0; PUB_DONE=1", False),
             "none yet": ('PUB_VMID=""; PUB_REUSED=0; PUB_DONE=0', False)}
    for what, (state, deleted) in cases.items():
        script = FAKE_PVE + 'sudo() { "$@"; }\nPUB_WORK=""\n' + _functions("dciab_publish_cleanup") + \
            f"{state}\ntrap dciab_publish_cleanup EXIT\nfalse\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 1, what
        assert ("DELETED /nodes/pve/qemu/9001?purge=1" in r.stderr) is deleted, (what, r.stderr)
    text = (ROOT / "hack" / "lab" / "template.sh").read_text()
    body = _functions("dciab_publish")
    assert "trap dciab_publish_cleanup EXIT" in body and "failed()" not in text
    assert body.index("find_disk_template") < body.index("pve_free_vmid")
    assert "pve_vmid_by_name" not in body          # the final check reads the VM's own config


def test_pve_vmid_by_name_prefers_a_template_then_the_lowest_vmid():
    script = (ROOT / "hack" / "lab" / "pve.sh").read_text() + r'''
pve() {
  case "$2" in
    /nodes) echo '[{"node":"pve1","status":"online"},{"node":"pve2","status":"online"},{"node":"pve3","status":"offline"}]';;
    /nodes/pve1/qemu) echo '[{"vmid":9005,"name":"n"},{"vmid":9007,"name":"n","template":1},{"vmid":9001,"name":"m","template":1}]';;
    /nodes/pve2/qemu) echo '[{"vmid":9006,"name":"n","template":1}]';;
    /nodes/pve3/qemu) echo "pve3 must not be asked" >&2; return 22;;
    *) echo "unexpected $*" >&2; return 22;;
  esac
}
pve_vmid_by_name n; pve_vmid_by_name m; echo "[$(pve_vmid_by_name none)]"'''
    assert _bash(script).split() == ["9006", "9001", "[]"]


def test_pve_vmid_by_name_reads_the_nodes_not_the_lagging_cluster_resources():
    # Lab run 46166604667: a template made seconds earlier was in /cluster/resources without its name.
    text = (ROOT / "hack" / "lab" / "pve.sh").read_text()
    body = text[text.index("pve_vmid_by_name() {"):text.index("\n}\n", text.index("pve_vmid_by_name() {"))]
    assert "/cluster/resources" not in body and '"/nodes/$node/qemu"' in body
    script = text + r'''
pve() {
  case "$2" in
    /nodes) echo '[{"node":"pve1","status":"online"},{"node":"pve2","status":"online"}]';;
    /nodes/pve1/qemu) echo '[{"vmid":9001,"name":"dciab-node-aaaa","template":1}]';;
    /nodes/pve2/qemu) return 22;;                    # one node failing doesn't hide the rest
    /cluster/resources*) echo '[{"vmid":9001,"template":0}]';;
  esac
}
pve_vmid_by_name dciab-node-aaaa'''
    assert _bash(script).split() == ["9001"]

def test_lab_template_test_shows_why_before_dropping_a_failed_clone(tmp_path):
    # #292: cloud-init ended in "status: error", and the clone was dropped without a word on why.
    fakes = r'''
set -euo pipefail
Q=/nodes/pve/qemu
pve() { echo "PVE $*" >>"$LOG"; echo "PVE $*" >&2; [[ "$*" != *agent/ping* || "${AGENT_UP:-1}" == 1 ]]; }
sleep() { :; }
vm_ssh() { [[ "${SSH_OK:-1}" == 1 ]] || return 255; echo "SSH ${*: -1}" | head -2 >&2; echo SSH >>"$LOG"; }
agent_exec() { echo "AGENT $*" >&2; echo AGENT >>"$LOG"; [[ "${AGENT_DIES:-0}" == 0 ]] || { echo "lab: died" >&2; exit 1; }; }
''' + _functions("diagnose_test_vm", "drop_test_vm")
    runs = iter(range(100))

    def run(tail, **env):
        log = tmp_path / f"log{next(runs)}"
        log.touch()
        r = subprocess.run(["bash", "-c", fakes + tail], capture_output=True, text=True, cwd=ROOT,
                           env={"PATH": "/usr/bin:/bin", "LOG": str(log), **env})
        return r.returncode, r.stderr, log.read_text()

    rc, err, log = run('TEST_VM=9100; IP=192.0.2.9; KEY="$(mktemp)"; WORK="$(mktemp -d)"\ntrap drop_test_vm EXIT\nfalse\n')
    assert rc == 1 and "diagnosis of test VM 9100" in err and "cloud-init status --long" in err
    assert log.index("SSH") < log.index("PVE DELETE")                               # before the clone goes
    rc, err, log = run('TEST_VM=9100; IP=192.0.2.9; KEY="$(mktemp)"; WORK="$(mktemp -d)"\ntrap drop_test_vm EXIT\nfalse\n',
                  SSH_OK="0")
    assert rc == 1 and "AGENT 9100 cloud-init status --long" in err
    assert log.index("AGENT") < log.index("PVE DELETE")
    rc, err, log = run('TEST_VM=9100; IP=192.0.2.9; KEY="$(mktemp)"; WORK="$(mktemp -d)"\ntrap drop_test_vm EXIT\ntrue\n')
    assert rc == 0 and "diagnosis" not in err and "PVE DELETE" in log             # a pass stays quiet
    start = 'TEST_VM=9100; IP=192.0.2.9; KEY="$(mktemp)"; WORK="$(mktemp -d)"\ntrap drop_test_vm EXIT\n'
    # Never fails: a diagnosis that dies (lab_die in agent_exec) still leaves cleanup and the status.
    rc, err, log = run(start + "exit 3\n", SSH_OK="0", AGENT_DIES="1")
    assert rc == 3 and "AGENT" in log and log.index("AGENT") < log.index("PVE DELETE")
    # No agent either: one ping, no 60-second wait on exec-status.
    rc, err, log = run(start + "false\n", SSH_OK="0", AGENT_UP="0")
    assert rc == 1 and "AGENT" not in log and "no guest agent either" in err and "PVE DELETE" in log
    # Cancelled (a signal's status): straight to cleanup.
    rc, err, log = run(start + "exit 130\n")
    assert rc == 130 and "diagnosis" not in err and "PVE DELETE" in log
    # A command that hangs on the clone can't hold cleanup up: the diagnosis's session is bounded.
    text = (ROOT / "hack" / "lab" / "template.sh").read_text()
    assert "VM_SSH_TIMEOUT=120 vm_ssh 'set +e" in _functions("diagnose_test_vm")
    start_ssh = text.index("vm_ssh() {")
    real_ssh = text[start_ssh:text.index('"$@"; }', start_ssh) + len('"$@"; }')]
    out = _bash("KEY=k; IP=192.0.2.9\nssh() { echo \"ARGS $*\"; }\ntimeout() { echo \"TIMEOUT $*\"; }\n" + real_ssh
                + "\nvm_ssh plain\nVM_SSH_TIMEOUT=120 vm_ssh bounded\n")
    assert out.splitlines()[0].startswith("ARGS -i k") and out.splitlines()[1].startswith("TIMEOUT --kill-after=10 120 ssh -i k")
    text = (ROOT / "hack" / "lab" / "template.sh").read_text()
    assert "cloud-init status --long | head" not in text
