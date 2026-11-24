"""The lab CI workflows (.github/workflows/lab*.yml) and their scripts (hack/lab/)."""
import re
import subprocess
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
    assert action["runs"]["steps"][0]["run"] == "hack/lab/check-token.sh"
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
