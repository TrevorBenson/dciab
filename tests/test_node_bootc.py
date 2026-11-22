"""images/node-bootc/build.sh: the plan it prints for each variant and version."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "images" / "node-bootc" / "build.sh"
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())


def plan(*args):
    return subprocess.run(["bash", str(BUILD), "--print", *args], capture_output=True, text=True)


def test_default_is_the_bundle_version_for_vms():
    out = plan().stdout
    v = LOCK["kubernetes"]["version"]
    assert f"kubernetes {v}" in out and "variant vm" in out
    assert f"-t localhost/dciab-node:{v} " in out
    assert "VARIANT=vm" in out


def test_metal_variant_gets_its_own_tag():
    out = plan("--variant", "metal").stdout
    assert f"localhost/dciab-node:{LOCK['kubernetes']['version']}-metal" in out
    assert "VARIANT=metal" in out
    assert plan("--variant", "arm").returncode == 2


def test_upgrade_from_is_one_minor_below_with_its_own_cri_o():
    start = LOCK["kubernetes"]["upgradeFrom"]
    out = plan("--upgrade-from").stdout
    minor = ".".join(start.split(".")[:2])
    assert f"kubernetes {start} (series {minor}), cri-o {minor}" in out
    assert f"KUBERNETES_VERSION={start[1:]}" in out


def test_lock_versions_line_up():
    v = LOCK["kubernetes"]["version"]
    assert LOCK["nodeImages"]["bootc"]["crioSeries"] == ".".join(v.split(".")[:2])
    a = [int(x) for x in re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", LOCK["kubernetes"]["upgradeFrom"]).groups()]
    b = [int(x) for x in re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", v).groups()]
    assert (a[0], a[1] + 1) == (b[0], b[1])


@pytest.mark.parametrize("unit", ["qemu-guest-agent", "vmtoolsd"])
def test_both_guest_agents_enabled(unit):
    text = (ROOT / "images" / "node-bootc" / "Containerfile").read_text()
    enable = next(line for line in text.splitlines() if line.startswith("RUN systemctl enable"))
    assert unit in enable.split()
