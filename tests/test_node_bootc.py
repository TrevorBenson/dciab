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


# Lab run 46169994618 (#292): Proxmox VE's cloud-init data asked for `package_upgrade`, cloud-init
# ran `dnf -y upgrade`, dnf refused on the read-only bootc /usr, and cloud-init ended in
# "status: error", which CAPMOX reads as a node that never finished.
NO_PKG = ROOT / "images" / "node-bootc" / "no-package-module.py"
RHEL_FINAL = """\
cloud_final_modules:
 - package_update_upgrade_install
 - write_files_deferred
 - puppet
 - [scripts_user, always]
 - ssh_authkey_fingerprints
 - final_message
 - power_state_change
"""


def run_no_pkg(tmp_path, cfg):
    src, dst = tmp_path / "cloud.cfg", tmp_path / "91.cfg"
    src.write_text(cfg)
    r = subprocess.run(["python3", str(NO_PKG), str(src), str(dst)], capture_output=True, text=True)
    return r, dst


def test_the_package_module_comes_out_of_the_final_modules_and_nothing_else(tmp_path):
    r, dst = run_no_pkg(tmp_path, RHEL_FINAL)
    assert r.returncode == 0, r.stderr
    final = yaml.safe_load(dst.read_text())["cloud_final_modules"]
    assert final == ["write_files_deferred", "puppet", ["scripts_user", "always"],
                     "ssh_authkey_fingerprints", "final_message", "power_state_change"]
    r, dst = run_no_pkg(tmp_path, RHEL_FINAL.replace(" - package_update_upgrade_install",
                                                     " - [package_update_upgrade_install, once-per-instance]"))
    assert r.returncode == 0 and "package_update_upgrade_install" not in dst.read_text().split("\n", 2)[2]


@pytest.mark.parametrize("cfg", ["cloud_final_modules:\n - final_message\n", "datasource_list: [NoCloud]\n", ""])
def test_the_build_fails_when_the_package_layout_changed(tmp_path, cfg):
    r, dst = run_no_pkg(tmp_path, cfg)
    assert r.returncode != 0 and "no-package-module" in r.stderr and not dst.exists()


def test_the_image_runs_it_and_the_boot_test_asks_for_an_upgrade():
    cf = (ROOT / "images" / "node-bootc" / "Containerfile").read_text()
    assert "COPY no-package-module.py /tmp/no-package-module.py" in cf
    assert "RUN python3 /tmp/no-package-module.py" in cf
    assert cf.index("RUN python3 /tmp/no-package-module.py") > cf.index("COPY rootfs/ /")   # after 90-dciab.cfg
    boot = (ROOT / "hack" / "e2e" / "boot-node-image.sh").read_text()
    assert "package_upgrade: true" in boot                     # as Proxmox VE's data does by default
