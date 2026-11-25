"""Redfish preparation before an install: boot mode, Secure Boot, virtualization, the target disk.

Runs in the media step for a host it is about to install, after the reinstall guard and before
the ISO is attached. Standard Redfish where it exists (ComputerSystem.SecureBoot, Storage and
Drives); BIOS attributes where it doesn't (boot mode, virtualization), only under the attribute
names and values the major vendors share. BIOS and Secure Boot changes are staged to apply at
the next reset, which is the reset that boots the installer.

What it does with each:
  boot mode       UEFI required. A BIOS BootMode attribute set to legacy holds a host with no
                  install record (a legacy-mode server with a disk may hold an installed OS);
                  with --reinstall it is staged to Uefi. No readable boot mode is a note
  Secure Boot     set to spec.hypervisors.bootMode (on for UEFISecureBoot, off for UEFI) when
                  the BMC offers ComputerSystem.SecureBoot; a note when it doesn't. Never
                  changed while the boot mode still reads legacy (firmware refuses Secure Boot
                  in legacy mode): a note says to set it once the boot mode is UEFI
  virtualization  a known BIOS attribute set to Disabled is staged to Enabled; unreadable is a
                  note, never a failure
  disk            rootDeviceHints against the drives the BMC lists. Serial number and WWN alone
                  decide: no drive with the hinted serial number or WWN is a refusal (the
                  installer would pick nothing, or the wrong disk), unless the storage has RAID
                  volumes (the installer may see a virtual disk under other identifiers). A
                  model or vendor that differs on the drive matching the serial or WWN is a
                  note. Model and vendor only, no drives listed, or nothing to compare, is a
                  note: Redfish and udev name models and vendors differently. A WWN hint with no
                  digits is refused
Writes happen only when nothing holds or refuses the host: BIOS attributes first, then Secure
Boot. If a write fails, the error says which changes are already staged in the firmware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# BIOS attributes, by the names the major vendors use (Dell, HPE, Supermicro, Lenovo).
# BootMode (Dell, HPE) is read and set. BootModeSelect (Supermicro) and BootModes_SystemBootMode
# (Lenovo) are only read: a legacy or mixed value holds a host with no install record, and a
# --reinstall run gets a note to set UEFI in the firmware setup, since their write values aren't
# tested.
BOOT_MODE_KEYS = ("BootMode", "BootModeSelect", "BootModes_SystemBootMode")
WRITABLE_BOOT_MODE_KEYS = ("BootMode",)
# Words in a boot mode value that mean legacy BIOS boot is on: legacy only, or mixed (DUAL,
# "UEFI and Legacy"), which can boot a legacy OS too.
LEGACY_BOOT_WORDS = ("legacy", "bios", "dual")


def is_legacy(value: object) -> bool:
    """Whether a boot mode attribute value allows legacy BIOS boot (legacy only, or mixed)."""
    return isinstance(value, str) and any(w in value.lower() for w in LEGACY_BOOT_WORDS)
VIRT_KEYS = ("ProcVirtualization", "IntelVirtualizationTechnology", "VirtualizationTechnology",
             "IntelVTX", "VMX", "SVMMode", "SvmMode", "AmdVirtualization", "ProcAmdVirtualization")
ON_FOR = {"Disabled": "Enabled", "Disable": "Enable"}


def turned_on(value: object) -> Optional[object]:
    """The "on" value for an attribute that reads off, or None. Only a real boolean False maps to
    True: an integer 0 equals False in Python, but it isn't a boolean attribute."""
    if type(value) is bool:
        return True if value is False else None
    if isinstance(value, str):
        return ON_FOR.get(value)
    return None


@dataclass
class Prep:
    done: List[str] = field(default_factory=list)      # what was checked or changed
    notes: List[str] = field(default_factory=list)     # what couldn't be checked; never blocks
    refuse: Optional[str] = None                        # why the install must not go ahead
    hold: Optional[str] = None                          # why the host may hold an install
    bios: Dict[str, object] = field(default_factory=dict)   # BIOS attributes to stage
    staged: List[str] = field(default_factory=list)     # changes written, pending until a reset


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


def wwn_matches(want: str, have: str) -> bool:
    """Equal, or one a suffix of the other with at least 16 hex digits in common (vendors report
    a NAA WWN with or without its leading digits). Never matches an empty WWN."""
    if not want or not have:
        return False
    if want == have:
        return True
    short = min(len(want), len(have))
    return short >= 16 and (want.endswith(have) or have.endswith(want))


def match_identity(hints: Dict[str, object], drive: dict) -> bool:
    """The hinted serial number and WWN (those given) match this drive."""
    if "serialNumber" in hints and _norm(drive.get("SerialNumber") or "") != _norm(hints["serialNumber"]):
        return False
    if "wwn" in hints and not any(wwn_matches(_wwn(hints["wwn"]), w) for w in _drive_wwns(drive)):
        return False
    return True


def match_drive(hints: Dict[str, object], drive: dict) -> bool:
    """Every hint Redfish can show matches this drive. deviceName can't be compared."""
    if not match_identity(hints, drive):
        return False
    if "model" in hints and _norm(hints["model"]) not in _norm(drive.get("Model") or ""):
        return False
    if "vendor" in hints and _norm(hints["vendor"]) not in _norm(
            f"{drive.get('Manufacturer') or ''} {drive.get('Model') or ''}"):
        return False
    return True


def _descriptive_mismatch(hints: Dict[str, object], drive: dict) -> List[str]:
    out = []
    if "model" in hints and _norm(hints["model"]) not in _norm(drive.get("Model") or ""):
        out.append(f"model {hints['model']!r} (the BMC reports {drive.get('Model') or 'none'!r})")
    if "vendor" in hints and _norm(hints["vendor"]) not in _norm(
            f"{drive.get('Manufacturer') or ''} {drive.get('Model') or ''}"):
        out.append(f"vendor {hints['vendor']!r} (the BMC reports {drive.get('Manufacturer') or 'none'!r})")
    return out


def disk_check(hints: Dict[str, object], drives: Optional[List[dict]]) -> Tuple[Optional[bool], str, List[str]]:
    """(True, what matched, notes) | (False, why to refuse, []) | (None, why it's unknown, []).
    Serial number and WWN alone decide a refusal; model and vendor only add notes."""
    comparable = {k: v for k, v in hints.items() if k in ("serialNumber", "wwn", "model", "vendor")}
    if "wwn" in comparable and not _wwn(comparable["wwn"]):
        return False, f"rootDeviceHints.wwn {comparable['wwn']!r} has no digits; give the disk's full WWN", []
    if not comparable:
        return None, "the disk can't be checked over Redfish (deviceName names the installer's view, not the BMC's)", []
    if not drives:
        return None, "the BMC lists no drives, so the disk matching rootDeviceHints can't be checked before the install", []
    hinted = ", ".join(f"{k}={v}" for k, v in sorted(comparable.items()))
    listed = "; ".join(describe(d) for d in drives) or "none"
    identity = {k: v for k, v in comparable.items() if k in ("serialNumber", "wwn")}
    if identity:
        matches = [d for d in drives if match_identity(identity, d)]
        if matches:
            notes = [f"the drive matching {', '.join(f'{k}={v}' for k, v in sorted(identity.items()))} differs in "
                     + " and ".join(_descriptive_mismatch(comparable, matches[0]))
                     + "; serial number and WWN decide, so the install goes ahead"] \
                if _descriptive_mismatch(comparable, matches[0]) else []
            return True, f"disk matching {hinted}: {describe(matches[0])}", notes
        if any(d.get("_volume") for d in drives):
            return None, (f"no drive or volume the BMC lists matches {hinted} (it lists: {listed}); behind a RAID "
                          "controller the installer may see the disk under other identifiers, so the installer decides"), []
        return False, (f"no drive the BMC lists matches rootDeviceHints ({hinted}); it lists: {listed}. "
                       "Fix the hints, or check the disk is in the server and visible to the BMC"), []
    matches = [d for d in drives if match_drive(comparable, d)]
    if matches:
        return True, f"disk matching {hinted}: {describe(matches[0])}", []
    return None, (f"no drive the BMC lists matches {hinted} (it lists: {listed}); model and vendor names can differ "
                  "between the BMC and the installer, so the installer decides"), []


def run(bmc, boot_mode: str, hints: Dict[str, object], hold_legacy: bool = True) -> Prep:
    """Check and stage what an install needs. Never resets. hold_legacy (a host with no
    --reinstall) holds the host when its boot mode reads legacy, before any write. Raises
    RedfishError only for a write the BMC refused; its message lists what is already staged."""
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
    legacy = mode_key is not None and is_legacy(attrs[mode_key])
    if mode_key is None:
        prep.notes.append("the boot mode can't be read over Redfish; the install boots the ISO in UEFI mode, "
                          "which fails on a server set to legacy BIOS boot")
    elif legacy:
        if hold_legacy:
            prep.hold = (f"the BIOS boot mode reads {attrs[mode_key]} (legacy), and a legacy-mode server may hold an "
                         "installed OS that its boot entries don't show")
            return prep      # nothing written
        if mode_key in WRITABLE_BOOT_MODE_KEYS:
            prep.bios[mode_key] = "Uefi"
        else:
            prep.notes.append(f"the BIOS boot mode reads {mode_key} {attrs[mode_key]} (legacy); DCiaB doesn't set "
                              f"{mode_key}: set it to UEFI in the firmware setup or through the BMC, or the "
                              "installer won't boot")
    else:
        prep.done.append(f"boot mode {attrs[mode_key]}")
    virt = [k for k in VIRT_KEYS if k in attrs]
    if not virt:
        prep.notes.append("virtualization extensions can't be read over Redfish; check them in the firmware setup, "
                          "or VMs won't start on this host")
    for k in virt:
        on = turned_on(attrs[k])
        if on is not None:
            prep.bios[k] = on
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
        elif legacy:
            # Firmware refuses Secure Boot while the boot mode is legacy; the boot mode change only
            # applies at the reset.
            prep.notes.append(f"Secure Boot isn't changed while the boot mode reads legacy; once the boot mode is "
                              f"UEFI, set Secure Boot {'on' if want else 'off'} to match bootMode {boot_mode} "
                              "in the firmware setup or through the BMC")
        else:
            sb_change = path

    # The disk.
    try:
        drives = bmc.drives()
    except RedfishError as exc:
        drives = None
        prep.notes.append(f"the BMC's storage couldn't be read ({exc})")
    ok, msg, disk_notes = disk_check(hints, drives)
    if ok is False:
        prep.refuse = msg
        return prep          # nothing written: a refused host keeps its settings
    (prep.done if ok else prep.notes).append(msg)
    prep.notes.extend(disk_notes)

    # Writes, only once nothing holds or refuses the install: BIOS first (boot mode), then Secure Boot.
    try:
        if prep.bios:
            bmc.set_bios(bios, prep.bios)
            change = "BIOS " + ", ".join(f"{k} -> {v}" for k, v in prep.bios.items())
            prep.staged.append(change)
            prep.done.append(change + "; applies at the reset")
        if sb_change:
            bmc.set_secure_boot(sb_change, want)
            prep.staged.append(f"Secure Boot {'on' if want else 'off'}")
            prep.done.append(f"Secure Boot set {'on' if want else 'off'} for bootMode {boot_mode}; it applies at the reset")
    except RedfishError as exc:
        raise RedfishError(f"{exc}{pending_note(prep.staged)}") from None
    return prep


def pending_note(staged: List[str]) -> str:
    """'' or a sentence naming the changes already staged in the firmware."""
    if not staged:
        return ""
    return (f". Already staged in the firmware, pending until the next reset: {'; '.join(staged)}. "
            "They apply at any reset, so revert them in the BMC if the host shouldn't change")
