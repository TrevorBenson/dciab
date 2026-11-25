"""The emulated bare server workflow (hack/e2e/hypervisors): hosted, pinned, and its scripts
checked. The scenarios themselves run in .github/workflows/hypervisors-emulated.yml."""

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "hack" / "e2e" / "hypervisors"
WF = ROOT / ".github" / "workflows" / "hypervisors-emulated.yml"
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())


def test_runs_on_hosted_runners_only():
    wf = yaml.safe_load(WF.read_text())
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        assert "timeout-minutes" in job
    legs = wf["jobs"]["emulated"]["strategy"]["matrix"]["include"]
    assert {leg["mode"] for leg in legs} == {"untagged", "tagged"}


def test_legs_cover_every_scenario_once_with_their_own_install():
    """The untagged mode runs as two parallel legs: together they run every scenario of the full
    untagged list, and each leg that needs an installed host installs it itself."""
    import ast
    wf = yaml.safe_load(WF.read_text())
    legs = {leg["leg"]: leg["mode"] for leg in wf["jobs"]["emulated"]["strategy"]["matrix"]["include"]}
    tree = ast.parse((HERE / "run.py").read_text())
    table, full = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "SCENARIOS":
            table = {k.value: [e.id for e in v.elts] for k, v in zip(node.value.keys, node.value.values)}
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
            full[node.targets[0].slice.value] = [e.id for e in node.value.elts]
    assert set(legs) <= set(table), "a CI leg has no scenario list in run.py"
    for leg, mode in legs.items():
        assert leg == mode or leg.startswith(mode + "-")
    untagged = [s for leg, mode in legs.items() if mode == "untagged" for s in table[leg]]
    assert sorted(set(untagged)) == sorted(set(full["untagged"]))
    needs_install = {"rerun", "lost_state", "lost_state_host_off", "recorded_host_off", "reinstall", "reboot"}
    for leg, names in table.items():
        if needs_install & set(names):
            assert "first_install" in names and names.index("first_install") < min(
                names.index(n) for n in needs_install & set(names)), leg


def test_sushy_tools_is_pinned_with_its_checksum():
    s = LOCK["hypervisorEmulator"]["sushyTools"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", s["version"])
    assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"])
    setup = (HERE / "setup.sh").read_text()
    assert "hypervisorEmulator/sushyTools/version" in setup and "--require-hashes" in setup


def test_sushy_tools_and_its_dependencies_are_hashed():
    e = LOCK["hypervisorEmulator"]
    text = (ROOT / e["requirements"]).read_text()
    block = re.search(r"^sushy-tools==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M)
    assert block and block.group(1) == e["sushyTools"]["version"]
    assert f"sha256:{e['sushyTools']['sha256']}" in block.group(2)
    entries = re.split(r"\n(?=[A-Za-z0-9])", "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).strip())
    assert len(entries) > 5 and all("--hash=sha256:" in x for x in entries)
    assert not re.search(r"^libvirt", text, re.M)     # python3-libvirt comes from the distro


def test_path_filters_cover_what_the_emulator_runs():
    wf = yaml.safe_load(WF.read_text())
    on = wf.get("on") or wf.get(True)
    need = {"bundle.lock", "lib/dciab_hypervisors/**", "lib/dciab_validate/**", "lib/dciab_services/**",
            "lib/steps/services.sh", "examples/proxmox-bare.yaml", "schema/**", "dciab", "hack/e2e/hypervisors/**"}
    for event in ("pull_request", "push"):
        assert need <= set(on[event]["paths"]), (event, need - set(on[event]["paths"]))
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in WF.read_text()


def test_scripts_are_executable_and_shellchecked_by_ci():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in ("setup.sh", "run.py", "patch_sushy.py"):
        assert os.access(HERE / name, os.X_OK), name
    assert "hack/e2e/hypervisors/setup.sh" in ci


def test_never_part_of_the_archive():
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import package
    finally:
        sys.path.pop(0)
    assert not any(s.startswith("hack/e2e") for s in package.SOURCES)


def test_the_vm_port_modes_match_the_scenarios():
    setup = (HERE / "setup.sh").read_text()
    # access port: the node VLAN untagged; tagged: the node VLAN tagged, nothing untagged
    assert 'bridge vlan add dev "$tap" vid "$vid" pvid untagged' in setup
    assert 'bridge vlan add dev "$tap" vid "$vid"\n' in setup
    assert "vlan_default_pvid 0" in setup
    assert "<on_reboot>destroy</on_reboot>" in setup


def test_patch_sushy_applies_once(tmp_path):
    driver = tmp_path / "libvirtdriver.py"
    anchor = ("        os_element = os_elements[0]\n\n        if self._is_firmware_autoselection(tree):\n"
              "            self._build_os_element_fw_autoselection(boot_mode, secure,")
    driver.write_text("x = 1\n" + anchor + "\n")
    for _ in range(2):
        r = subprocess.run([sys.executable, str(HERE / "patch_sushy.py"), str(driver)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    assert driver.read_text().count("dciab-emulator: keep the current Secure Boot setting") == 1
    driver.write_text("something else\n")
    assert subprocess.run([sys.executable, str(HERE / "patch_sushy.py"), str(driver)], capture_output=True).returncode == 1


def test_run_writes_a_valid_site(tmp_path):
    """run.py's site config passes dciab validate (offline), in both modes."""
    if subprocess.run(["which", "openssl"], capture_output=True).returncode != 0:
        return
    sys.path.insert(0, str(HERE))
    try:
        import run
    finally:
        sys.path.pop(0)
    crt = tmp_path / "bmc.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=x",
                    "-keyout", "/dev/null", "-out", str(crt)], check=True, capture_output=True)
    (tmp_path / "bin").mkdir()
    keygen = tmp_path / "bin" / "age-keygen"
    keys = {"id1.txt": "age1j5xfcwdps86qjrrxvv8uresmxqessnxskgml6p7x7xw8az4th6mpns0acs",
            "id2.txt": "age1skgjkfxj3tkldtdnkd9cwuhennz9uuvg4q058rte5yhwk2vypq0g0u2207"}
    keygen.write_text("#!/bin/sh\ncase \"$1\" in -y) case \"$2\" in "
                      + " ".join(f"*{k}) echo {v};;" for k, v in keys.items()) + " esac;; *) : > \"$2\";; esac\n")
    keygen.chmod(0o755)
    for mode in ("untagged", "tagged"):
        (tmp_path / "env").write_text(
            f"MODE={mode}\nVM=pve01\nVM_UUID=7f3e9a5c-1d2b-4c6e-9f80-dc1ab0000001\nVM_MAC=52:54:00:dc:1a:01\n"
            "TAP=hv-pve01\nVLAN=110\nBOOT_MAC=b8:ca:3a:00:00:05\nBMC_URL=https://10.30.0.21:8000\nDISK_SERIAL=1234570\n"
            "OVMF_VARS_MS=/usr/share/OVMF/OVMF_VARS_4M.ms.fd\n")
        lab = run.Lab(tmp_path)
        config = lab.write_site(tmp_path / f"site-{mode}", "1234570")
        doc = yaml.safe_load(config.read_text())
        hv = doc["spec"]["hypervisors"]
        assert hv["managementVLAN"] == mode
        assert hv["bootMode"] == ("UEFI" if mode == "untagged" else "UEFISecureBoot")
        assert hv["hosts"][0]["bmc"]["address"].startswith("redfish-virtualmedia+https://10.30.0.21:8000/redfish/v1/Systems/")
        r = subprocess.run([str(ROOT / "dciab"), "validate", str(config)], capture_output=True, text=True,
                           env=dict(os.environ, DCIAB_PVE_TOKEN="x", DCIAB_PVE_CSI_TOKEN="x"))
        assert r.returncode == 0, r.stdout + r.stderr
