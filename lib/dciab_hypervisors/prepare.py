"""Redfish preparation before an install: boot mode, Secure Boot, virtualization, the target disk.

Runs in the media step for a host it is about to install, after the reinstall guard and before
the ISO is attached. Standard Redfish where it exists (ComputerSystem.SecureBoot, Storage and
Drives); BIOS attributes where it doesn't (boot mode, virtualization), only under the attribute
names and values the major vendors share. BIOS and Secure Boot changes are staged to apply at
the next reset, which is the reset that boots the installer.

What it does with each:
  boot mode       UEFI required. A BIOS BootMode attribute set to legacy is staged to Uefi;
                  no readable boot mode is a note, not a failure
  Secure Boot     set to spec.hypervisors.bootMode (on for UEFISecureBoot, off for UEFI) when
                  the BMC offers ComputerSystem.SecureBoot; a note when it doesn't
  virtualization  a known BIOS attribute set to Disabled is staged to Enabled; unreadable is a
                  note, never a failure
  disk            rootDeviceHints against the drives the BMC lists. No drive with the hinted
                  serial number or WWN is a refusal (the installer would pick nothing, or the
                  wrong disk), unless the storage has RAID volumes (the installer may see a
                  virtual disk under other identifiers); model and vendor only, no drives
                  listed, or nothing to compare, is a note: Redfish and udev name models and
                  vendors differently
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# BIOS attributes, by the names the major vendors use (Dell, HPE, Supermicro, Lenovo).
BOOT_MODE_KEYS = ("BootMode",)
LEGACY_BOOT_VALUES = ("Bios", "LegacyBios", "Legacy")
VIRT_KEYS = ("ProcVirtualization", "IntelVirtualizationTechnology", "VirtualizationTechnology",
             "IntelVTX", "VMX", "SVMMode", "SvmMode", "AmdVirtualization", "ProcAmdVirtualization")
ON_FOR = {"Disabled": "Enabled", "Disable": "Enable", False: True}


@dataclass
class Prep:
    done: List[str] = field(default_factory=list)      # what was checked or changed
    notes: List[str] = field(default_factory=list)     # what couldn't be checked; never blocks
    refuse: Optional[str] = None                        # why the install must not go ahead
    bios: Dict[str, object] = field(default_factory=dict)   # BIOS attributes staged


def plan_lines(boot_mode: str, hints: Dict[str, object]) -> List[str]:
    """What preparation will check and set, for --print. Talks to nothing."""
    sb = "on" if boot_mode == "UEFISecureBoot" else "off"
    return [f"Redfish preparation before the install: UEFI boot mode, Secure Boot {sb} ({boot_mode}), "
            "virtualization extensions on where the BIOS exposes them, a disk matching "
            + ", ".join(f"{k}={v}" for k, v in sorted(hints.items()))]


def _norm(v: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(v)).lower()


def _wwn(v: object) -> str:
    s = str(v).strip().lower()
    for prefix in ("naa.", "eui.", "0x", "wwn-0x", "wwn-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _drive_wwns(drive: dict) -> List[str]:
    out = [_wwn(i.get("DurableName")) for i in drive.get("Identifiers") or []
           if isinstance(i, dict) and i.get("DurableName")]
    return [w for w in out if w]


def describe(drive: dict) -> str:
    bits = ["volume"] if drive.get("_volume") else []
    bits += [str(drive.get(k)) for k in ("Manufacturer", "Model", "SerialNumber", "Name") if drive.get(k)][:3]
    if drive.get("CapacityBytes"):
        bits.append(f"{int(drive['CapacityBytes']) / 1e9:.0f} GB")
    return " ".join(bits) or str(drive.get("Id") or drive.get("Name") or "?")


def match_drive(hints: Dict[str, object], drive: dict) -> bool:
    """Every hint Redfish can show matches this drive. deviceName can't be compared."""
    if "serialNumber" in hints and _norm(drive.get("SerialNumber") or "") != _norm(hints["serialNumber"]):
        return False
    if "wwn" in hints:
        want = _wwn(hints["wwn"])
        if not any(w.endswith(want) or want.endswith(w) for w in _drive_wwns(drive)):
            return False
    if "model" in hints and _norm(hints["model"]) not in _norm(drive.get("Model") or ""):
        return False
    if "vendor" in hints and _norm(hints["vendor"]) not in _norm(
            f"{drive.get('Manufacturer') or ''} {drive.get('Model') or ''}"):
        return False
    return True


def disk_check(hints: Dict[str, object], drives: Optional[List[dict]]) -> Tuple[Optional[bool], str]:
    """(True, what matched) | (False, why to refuse) | (None, why it's unknown)."""
    comparable = {k: v for k, v in hints.items() if k in ("serialNumber", "wwn", "model", "vendor")}
    if not comparable:
        return None, "the disk can't be checked over Redfish (deviceName names the installer's view, not the BMC's)"
    if not drives:
        return None, "the BMC lists no drives, so the disk matching rootDeviceHints can't be checked before the install"
    matches = [d for d in drives if match_drive(comparable, d)]
    hinted = ", ".join(f"{k}={v}" for k, v in sorted(comparable.items()))
    if matches:
        return True, f"disk matching {hinted}: {describe(matches[0])}"
    listed = "; ".join(describe(d) for d in drives) or "none"
    if any(d.get("_volume") for d in drives):
        return None, (f"no drive or volume the BMC lists matches {hinted} (it lists: {listed}); behind a RAID "
                      "controller the installer may see the disk under other identifiers, so the installer decides")
    if "serialNumber" in comparable or "wwn" in comparable:
        return False, (f"no drive the BMC lists matches rootDeviceHints ({hinted}); it lists: {listed}. "
                       "Fix the hints, or check the disk is in the server and visible to the BMC")
    return None, (f"no drive the BMC lists matches {hinted} (it lists: {listed}); model and vendor names can differ "
                  "between the BMC and the installer, so the installer decides")


def run(bmc, boot_mode: str, hints: Dict[str, object]) -> Prep:
    """Check and stage what an install needs. Never resets; raises RedfishError only for a
    write the BMC refused."""
    from .redfish import RedfishError
    prep = Prep()

    # BIOS attributes: boot mode and virtualization.
    try:
        bios = bmc.bios()
    except RedfishError as exc:
        bios = None
        prep.notes.append(f"BIOS attributes couldn't be read ({exc})")
    attrs = (bios or {}).get("Attributes") or {}
    mode_key = next((k for k in BOOT_MODE_KEYS if k in attrs), None)
    if mode_key is None:
        prep.notes.append("the boot mode can't be read over Redfish; the install boots the ISO in UEFI mode, "
                          "which fails on a server set to legacy BIOS boot")
    elif attrs[mode_key] in LEGACY_BOOT_VALUES:
        prep.bios[mode_key] = "Uefi"
    else:
        prep.done.append(f"boot mode {attrs[mode_key]}")
    virt = [k for k in VIRT_KEYS if k in attrs]
    if not virt:
        prep.notes.append("virtualization extensions can't be read over Redfish; check them in the firmware setup, "
                          "or VMs won't start on this host")
    for k in virt:
        if attrs[k] in ON_FOR:
            prep.bios[k] = ON_FOR[attrs[k]]
        else:
            prep.done.append(f"{k} {attrs[k]}")

    # Secure Boot.
    want = boot_mode == "UEFISecureBoot"
    sb_change = None
    try:
        sb = bmc.secure_boot()
    except RedfishError as exc:
        sb = False
        prep.notes.append(f"Secure Boot couldn't be read ({exc}); set it {'on' if want else 'off'} in the "
                          f"firmware setup to match bootMode {boot_mode}")
    if sb is False:
        pass
    elif sb is None:
        prep.notes.append(f"the BMC offers no Secure Boot setting; set Secure Boot {'on' if want else 'off'} "
                          f"in the firmware setup to match bootMode {boot_mode}")
    else:
        path, res = sb
        if res.get("SecureBootEnable") is want:
            prep.done.append(f"Secure Boot {'on' if want else 'off'} ({boot_mode})")
        else:
            sb_change = path

    # The disk.
    try:
        drives = bmc.drives()
    except RedfishError as exc:
        drives = None
        prep.notes.append(f"the BMC's storage couldn't be read ({exc})")
    ok, msg = disk_check(hints, drives)
    if ok is False:
        prep.refuse = msg
        return prep          # nothing written: a refused host keeps its settings
    (prep.done if ok else prep.notes).append(msg)

    # Writes, only once nothing refuses the install.
    if sb_change:
        bmc.set_secure_boot(sb_change, want)
        prep.done.append(f"Secure Boot set {'on' if want else 'off'} for bootMode {boot_mode}; it applies at the reset")
    if prep.bios:
        bmc.set_bios(bios, prep.bios)
        prep.done.append("BIOS " + ", ".join(f"{k} -> {v}" for k, v in prep.bios.items()) + "; applies at the reset")
    return prep
