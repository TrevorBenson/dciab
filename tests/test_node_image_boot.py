"""The workflow that builds the node image and boots it under QEMU/KVM on the lab runner."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows/node-image-boot.yml"


def wf():
    return yaml.safe_load(WF.read_text())


def test_it_runs_on_the_lab_runner_and_never_for_a_pull_request():
    """Hosted Ubuntu can't do it (#151); the runner hook refuses pull requests."""
    w = wf()
    assert w["jobs"]["boot"]["runs-on"] == ["self-hosted", "dciab-lab"]
    assert set(w[True]) == {"push", "workflow_dispatch"}
    assert w[True]["push"]["branches"] == ["main"]
    assert w["concurrency"] == {"group": "dciab-lab", "cancel-in-progress": False}
    assert w["permissions"] == {"contents": "read"}
    text = WF.read_text()
    assert "secrets." not in text and "environment:" not in text


def test_actions_are_pinned_by_sha():
    for uses in re.findall(r"uses:\s*(\S+)", WF.read_text()):
        assert re.search(r"@[0-9a-f]{40}$", uses), uses


def test_it_fails_early_with_a_clear_message_without_qemu_or_kvm():
    step = next(s for s in wf()["jobs"]["boot"]["steps"] if s.get("name", "").startswith("Tools"))
    assert "/dev/kvm" in step["run"] and "qemu" in step["run"]


def test_the_runner_image_carries_qemu_kvm():
    assert "qemu-kvm" in (ROOT / "images/lab-runner/Containerfile").read_text()


def test_the_boot_script_checks_what_a_proxmox_clone_needs():
    script = (ROOT / "hack/e2e/boot-node-image.sh").read_text()
    for needle in ("Enforcing", "dciab-qemu-ga", "guest-exec", "cloud-init status --wait", "iscsiadm", "crio", "/usr/libexec/qemu-kvm"):
        assert needle in script, needle
