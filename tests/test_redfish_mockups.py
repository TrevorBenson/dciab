"""dciab hypervisors' Redfish code against DMTF's Redfish Mockup Server.

The guard (boot options, paging), the preparation (BIOS boot mode and virtualization, Secure
Boot, drives) and the media steps run through the real HTTPS client against DMTF's public
mockups, plus overlays in tests/fixtures/redfish/overlays that add what the public mockups don't
have (boot option lists, vendor BIOS attribute names). Each overlay mirrors the mockup tree:
index.json adds or replaces a resource, index.patch.json is a JSON merge patch (RFC 7386: null
deletes a key) on the base resource.

Runs when DCIAB_REDFISH_MOCKUPS names a directory made by hack/redfish/fetch.py and the Mockup
Server's Python requirements are installed (the CI job "Redfish mockups"); skipped otherwise.
Locally:

    python3 hack/redfish/fetch.py /tmp/rf
    pip install requests grequests multipart     # versions: bundle.lock redfishTesting
    DCIAB_REDFISH_MOCKUPS=/tmp/rf python3 -m pytest -q tests/test_redfish_mockups.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dciab_hypervisors import prepare
from dciab_hypervisors.redfish import Bmc, RedfishError
from dciab_validate.probes import Trust

ROOT = Path(__file__).resolve().parent.parent
OVERLAYS = ROOT / "tests" / "fixtures" / "redfish" / "overlays"
CACHE = os.environ.get("DCIAB_REDFISH_MOCKUPS")

pytestmark = pytest.mark.skipif(
    not CACHE or importlib.util.find_spec("grequests") is None or importlib.util.find_spec("multipart") is None,
    reason="needs DCIAB_REDFISH_MOCKUPS (hack/redfish/fetch.py) and the Mockup Server's requirements")

RACK = ("public-rackmount1", "437XR1138R2")
TOWER = ("public-tower", "437XR1238R2")
LOCAL = ("public-localstorage", "437XR1138R2")


def merge_patch(target, patch):
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def build_tree(base: Path, overlay: str | None, dest: Path) -> Path:
    shutil.copytree(base, dest, symlinks=False)
    if overlay:
        src = OVERLAYS / overlay
        assert src.is_dir(), f"no overlay {overlay}"
        for f in sorted(src.rglob("*.json")):
            rel = f.relative_to(src)
            target = dest / rel.parent / "index.json"
            if f.name == "index.patch.json":
                assert target.is_file(), f"{overlay}: {rel} patches a resource the mockup doesn't have"
                target.write_text(json.dumps(merge_patch(json.loads(target.read_text()), json.loads(f.read_text())),
                                             indent=2))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f.read_text())
    return dest


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    d = tmp_path_factory.mktemp("mockup-tls")
    key, crt = d / "key.pem", d / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    h = hashlib.sha256(ssl.PEM_cert_to_DER_cert(crt.read_text())).hexdigest().upper()
    return {"key": key, "crt": crt, "pin": ":".join(h[i:i + 2] for i in range(0, len(h), 2))}


class Mockup:
    """One Mockup Server process serving one (possibly overlaid) mockup over HTTPS."""

    def __init__(self, tree: Path, system: str, tls: dict):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        server = Path(CACHE) / "mockup-server"
        self.proc = subprocess.Popen(
            [sys.executable, "redfishMockupServer.py", "-S", "-D", str(tree), "-H", "127.0.0.1", "-p", str(self.port),
             "-s", "--cert", str(tls["crt"]), "--key", str(tls["key"])],
            cwd=server, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.system = system
        self.bmc = Bmc(f"redfish-virtualmedia+https://127.0.0.1:{self.port}/redfish/v1/Systems/{system}",
                       "dciab", "not-checked-by-the-mockup", Trust(fingerprint=tls["pin"]), timeout=10)
        deadline = time.time() + 30
        while True:
            try:
                self.bmc.system()
                return
            except RedfishError:
                if self.proc.poll() is not None or time.time() > deadline:
                    err = self.proc.stderr.read().decode(errors="replace") if self.proc.poll() is not None else ""
                    self.stop()
                    raise RuntimeError(f"the Mockup Server didn't come up on {self.port}: {err[-2000:]}")
                time.sleep(0.3)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def mockup(tmp_path, tls):
    started = []

    def start(base, overlay=None):
        name, system = base
        tree = build_tree(Path(CACHE) / "mockups" / name, overlay, tmp_path / f"{name}-{overlay or 'as-published'}")
        m = Mockup(tree, system, tls)
        started.append(m)
        return m

    yield start
    for m in started:
        m.stop()


# -- the reinstall guard: boot options -------------------------------------------------------------

@pytest.mark.parametrize("base", [RACK, TOWER, LOCAL], ids=lambda b: b[0])
def test_published_mockups_list_no_boot_options_so_a_new_host_needs_first_install(mockup, base):
    assert mockup(base).bmc.os_boot_entries() is None


def test_a_raid_controller_entry_with_no_device_path_holds_the_host(mockup):
    assert mockup(RACK, "legacy-raid").bmc.os_boot_entries() == [
        "Integrated RAID Controller 1: PERC H730P Mini(bus 18 dev 00)"]


def test_an_os_entry_on_the_second_page_holds_the_host(mockup):
    assert mockup(RACK, "uefi-os-on-page-2").bmc.os_boot_entries() == ["Red Hat Enterprise Linux"]


def test_network_entries_alone_hold_nothing(mockup):
    assert mockup(RACK, "network-only").bmc.os_boot_entries() == []


def test_an_empty_boot_option_list_is_no_evidence(mockup):
    assert mockup(RACK, "empty-boot-options").bmc.os_boot_entries() is None


def test_a_boot_option_page_without_members_is_unreadable(mockup):
    with pytest.raises(RedfishError, match="makes no sense|no Members"):
        mockup(RACK, "boot-options-no-members").bmc.os_boot_entries()


# -- preparation: boot mode, virtualization, Secure Boot, the disk --------------------------------

def test_uefi_server_with_no_drives_listed_installs_with_notes(mockup):
    m = mockup(RACK)
    prep = prepare.run(m.bmc, "UEFI", {"serialNumber": "S6XXNX0T500001"})
    assert prep.hold is None and prep.refuse is None
    assert "boot mode Uefi" in prep.done
    assert "Secure Boot off (UEFI)" in prep.done
    assert any("lists no drives" in n for n in prep.notes)
    assert any("virtualization extensions can't be read" in n for n in prep.notes)
    assert prep.staged == []


def test_secure_boot_is_staged_on_for_uefisecureboot_and_lands_on_the_bmc(mockup):
    m = mockup(RACK)
    prep = prepare.run(m.bmc, "UEFISecureBoot", {"deviceName": "/dev/sda"})
    assert prep.refuse is None and prep.hold is None
    assert prep.staged == ["Secure Boot on"]
    assert m.bmc.secure_boot()[1]["SecureBootEnable"] is True


@pytest.mark.parametrize("overlay,value", [("legacy-raid", "Bios"), ("supermicro-dual", "DUAL"),
                                           ("lenovo-legacy", "LegacyMode")])
def test_a_legacy_or_mixed_boot_mode_holds_a_host_with_no_record_before_any_write(mockup, overlay, value):
    m = mockup(RACK, overlay)
    before = m.bmc.get(f"/redfish/v1/Systems/{m.system}/Bios/Settings")
    prep = prepare.run(m.bmc, "UEFISecureBoot", {"deviceName": "/dev/sda"})
    assert prep.hold and value in prep.hold
    assert prep.staged == [] and prep.bios == {}
    assert m.bmc.get(f"/redfish/v1/Systems/{m.system}/Bios/Settings") == before
    assert m.bmc.secure_boot()[1]["SecureBootEnable"] is False


def test_reinstall_stages_uefi_where_bootmode_is_writable(mockup):
    m = mockup(RACK, "legacy-raid")
    prep = prepare.run(m.bmc, "UEFISecureBoot", {"deviceName": "/dev/sda"}, hold_legacy=False)
    assert prep.hold is None and prep.refuse is None
    assert prep.bios == {"BootMode": "Uefi"}
    assert m.bmc.get(f"/redfish/v1/Systems/{m.system}/Bios/Settings")["Attributes"]["BootMode"] == "Uefi"
    # Secure Boot isn't touched while the boot mode still reads legacy.
    assert any("Secure Boot isn't changed while the boot mode reads legacy" in n for n in prep.notes)
    assert m.bmc.secure_boot()[1]["SecureBootEnable"] is False


def test_reinstall_leaves_vendor_boot_mode_names_to_the_operator_and_stages_virtualization(mockup):
    m = mockup(RACK, "supermicro-dual")
    prep = prepare.run(m.bmc, "UEFI", {"deviceName": "/dev/sda"}, hold_legacy=False)
    assert prep.bios == {"IntelVirtualizationTechnology": "Enabled"}
    assert any("DCiaB doesn't set BootModeSelect" in n for n in prep.notes)
    attrs = m.bmc.get(f"/redfish/v1/Systems/{m.system}/Bios/Settings")["Attributes"]
    assert attrs["IntelVirtualizationTechnology"] == "Enabled" and "BootModeSelect" not in attrs


def test_virtualization_turned_off_is_staged_on(mockup):
    m = mockup(RACK, "uefi-virt-off")
    prep = prepare.run(m.bmc, "UEFI", {"deviceName": "/dev/sda"})
    assert prep.bios == {"ProcVirtualization": "Enabled"}
    assert prep.staged == ["BIOS ProcVirtualization -> Enabled"]
    assert m.bmc.get(f"/redfish/v1/Systems/{m.system}/Bios/Settings")["Attributes"]["ProcVirtualization"] == "Enabled"


def test_a_hinted_serial_matches_a_listed_drive(mockup):
    m = mockup(LOCAL)
    prep = prepare.run(m.bmc, "UEFI", {"serialNumber": "1234570"})
    assert prep.refuse is None
    assert any("disk matching serialNumber=1234570" in d for d in prep.done)
    # This mockup has no Secure Boot resource: a note, never a refusal.
    assert any("offers no Secure Boot setting" in n for n in prep.notes)


def test_an_unknown_serial_behind_raid_volumes_is_left_to_the_installer(mockup):
    prep = prepare.run(mockup(LOCAL).bmc, "UEFI", {"serialNumber": "NOT-THERE"})
    assert prep.refuse is None
    assert any("behind a RAID controller" in n for n in prep.notes)


def test_an_unknown_serial_with_plain_drives_is_refused_before_any_write(mockup):
    m = mockup(LOCAL, "drives-no-volumes")
    prep = prepare.run(m.bmc, "UEFI", {"serialNumber": "NOT-THERE"})
    assert prep.refuse and "no drive the BMC lists matches" in prep.refuse
    assert "1234567" in prep.refuse
    assert prep.staged == []


def test_the_tower_mockups_many_controllers_are_read(mockup):
    drives = mockup(TOWER).bmc.drives()
    assert drives and any(d.get("_volume") for d in drives) and any(d.get("Model") == "NVMe 1000" for d in drives)


# -- media: attach, one-time boot, reset ----------------------------------------------------------

def test_media_attaches_by_patch_where_the_slot_has_no_actions(mockup):
    m = mockup(RACK)          # CD1 under the System, no InsertMedia action, another image inserted
    image = "http://10.30.110.5/host-isos/pve01.iso"
    done = m.bmc.prepare(image)
    assert done == ["ejected the attached media", f"attached {image}", "one-time boot from Cd (UEFI)"]
    st = m.bmc.attached(image)
    assert st["attached"] is True
    assert (st["BootSourceOverrideTarget"], st["BootSourceOverrideEnabled"]) == ("Cd", "Once")
    assert m.bmc.reset() == "ForceRestart"     # the mockup's system is On


def test_media_attaches_by_action_under_the_manager(mockup):
    """The client uses InsertMedia when the slot offers it, never a PATCH of the slot. The Mockup
    Server acknowledges actions without changing anything, so the slot still holds its published
    media afterwards; a PATCH would have replaced it. A real insert through the action is proven
    by the emulated bare server workflow (sushy-tools)."""
    m = mockup(TOWER, "vmedia-actions")    # CD1 under the Manager, with InsertMedia/EjectMedia actions
    path, slot = m.bmc.cd_slot()
    assert path == "/redfish/v1/Managers/BMC/VirtualMedia/CD1"
    assert "#VirtualMedia.InsertMedia" in slot["Actions"]
    image = "http://10.30.110.5/host-isos/pve01.iso"
    done = m.bmc.prepare(image)
    assert f"attached {image}" in done
    after = m.bmc.get(path)
    assert after.get("Image") == slot.get("Image") and after.get("Image") != image   # the action, not a PATCH
    assert m.bmc.state()["BootSourceOverrideTarget"] == "Cd"
