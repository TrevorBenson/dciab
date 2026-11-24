"""The hosted KVM job that boots the node image for pull requests."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows/node-image-boot.yml"


def wf():
    return yaml.safe_load(WF.read_text())


def test_it_runs_hosted_with_no_credentials_for_pull_requests():
    w = wf()
    job = w["jobs"]["boot"]
    assert job["runs-on"] == "ubuntu-24.04"          # never the credentialed lab runner
    assert "pull_request" in w[True]
    assert w["permissions"] == {"contents": "read"}
    text = WF.read_text()
    assert "secrets." not in text and "environment:" not in text and "self-hosted" not in text


def test_actions_are_pinned_by_sha():
    for uses in re.findall(r"uses:\s*(\S+)", WF.read_text()):
        assert re.search(r"@[0-9a-f]{40}$", uses), uses


def test_it_refuses_to_run_without_a_pinned_base():
    """A hosted runner can't build the base (nested rpm-ostree compose fails on Ubuntu)."""
    step = next(s for s in wf()["jobs"]["boot"]["steps"] if s["name"].startswith("The base must be pinned"))
    assert "baseImage" in step["run"] and "sha256" in step["run"]
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    assert re.search(r"@sha256:[0-9a-f]{64}$", lock["nodeImages"]["bootc"]["baseImage"])


def test_the_boot_script_checks_what_a_proxmox_clone_needs():
    script = (ROOT / "hack/e2e/boot-node-image.sh").read_text()
    for needle in ("Enforcing", "dciab-qemu-ga", "guest-exec", "cloud-init status --wait", "iscsiadm", "crio"):
        assert needle in script, needle
