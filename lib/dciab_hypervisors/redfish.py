"""Redfish virtual media for one BMC: attach an installer ISO, boot it once, reset the server.

Standard library only, over dciab_validate.probes.HttpsClient (certificate pin or CA bundle).
Handles the usual vendor differences: VirtualMedia under the System or under its Manager, and
InsertMedia/EjectMedia actions or a PATCH of the slot.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from dciab_validate.probes import HttpsClient, ProbeError, Trust


class RedfishError(Exception):
    """Something the BMC refused or doesn't offer. The message is safe to print."""


# Boot option names that load an installed OS. Vendors name entries freely, so this only adds
# to the device-path test below; "uefi os" is the generic entry firmware creates for a disk's
# \EFI\BOOT\BOOTX64.EFI (after an NVRAM reset or a board swap, for example).
OS_HINTS = ("proxmox", "uefi os", "windows boot manager", "rocky", "red hat", "redhat", "centos", "almalinux",
            "fedora", "debian", "ubuntu", "suse", "sles", "vmware", "esxi", "grub", "shim", "linux")
# Names of network boot entries. A network entry never loads an installed OS from a local disk,
# so it never holds a host, whatever else its name says.
NETWORK_HINTS = ("pxe", "http boot", "httpv", "network", "ipv4", "ipv6")
# Device path nodes of a network boot.
NETWORK_PATH = ("mac(", "ipv4(", "ipv6(", "uri(")
# Device path nodes of a local disk: a partition (HD), a legacy BIOS disk entry (BBS(HD...)), or a
# whole disk on a storage bus with no partition node (some firmware lists only the disk).
DISK_PATH = ("hd(", "bbs(hd", "sata(", "ata(", "scsi(", "sas(", "nvme(")
# Names of legacy (BIOS) disk entries, which often have no device path at all.
DISK_NAMES = ("hard drive", "hard disk")
# Words in the name of a boot option with no device path that point at a local disk or its
# controller, such as "Integrated RAID Controller 1: PERC H730" or "Embedded SATA Port Disk A".
# Legacy firmware often lists only these. Matched as whole words.
PATHLESS_DISK_WORDS = ("disk", "drive", "raid", "sata", "sas", "nvme", "ssd", "hdd")
# Removable optical or floppy entries: a path-less name with one of these doesn't count.
REMOVABLE_WORDS = ("cd", "dvd", "optical", "floppy", "cdrom")


def is_os_entry(name: str, device_path: str) -> bool:
    """Whether a boot option may load an installed OS, so the host must be held.

    Explicit OS evidence wins over any other word in the name: Proxmox VE, a name in OS_HINTS
    (so "UEFI OS (Virtual Disk)" counts), a legacy disk entry ("Hard drive C:", BBS(HD,...)), or
    a device path into a local disk: a partition (HD(...)), or a whole disk on a SATA, SCSI, SAS
    or NVMe bus with no partition node (a path ending in a CDROM(...) node is an optical drive,
    not a disk, and doesn't count unless it also has a partition node). So a USB-named entry on a partition, such as a bootable
    stick, counts too and holds the host. An entry with no device path whose name names a disk
    or a disk controller (a whole word from PATHLESS_DISK_WORDS, such as "Integrated RAID
    Controller 1: PERC H730" or "Embedded SATA Port Disk A") counts too, unless it names an
    optical or floppy drive. Only unambiguous network entries are left out: a network name or
    network path nodes, with no disk node in the path. Anything else (CD/DVD, shell, setup, an
    unnamed entry with no disk path) doesn't count."""
    lname, lpath = name.lower(), device_path.lower()
    text = f"{lname} {lpath}"
    if "proxmox" in text:
        return True
    network = any(n in lpath for n in NETWORK_PATH)
    optical = "cdrom(" in lpath       # a CD/DVD drive on a SATA, SCSI or SAS bus isn't a disk
    on_disk = "hd(" in lpath or "bbs(hd" in lpath or (
        not network and not optical and any(n in lpath for n in DISK_PATH))
    if not on_disk and (network or any(h in lname for h in NETWORK_HINTS)):
        return False
    if on_disk or any(h in text for h in OS_HINTS + DISK_NAMES):
        return True
    if not lpath.strip():
        words = set(re.findall(r"[a-z0-9]+", lname))      # "NVMe-1" -> {"nvme", "1"}
        return bool(words & set(PATHLESS_DISK_WORDS)) and not words & set(REMOVABLE_WORDS)
    return False


MAX_PAGES = 64


def _required_members(page: object) -> object:
    """A collection page's Members value; a page without one is unreadable, not empty."""
    if not isinstance(page, dict) or "Members" not in page:
        raise TypeError("a page of the list has no Members")
    return page["Members"]


def _members(value: object) -> list:
    """A collection's Members, which must be a JSON array; anything else is unreadable."""
    if not isinstance(value, list):
        raise TypeError(f"Members is {type(value).__name__}, not a list")
    return value


class Bmc:
    def __init__(self, address: str, username: str, password: str, trust: Trust, timeout: float = 30.0,
                 task_timeout: float = 300.0, poll: float = 2.0):
        self.task_timeout = task_timeout
        self.poll = poll
        url = address.split("+", 1)[1] if "+" in address else address
        parts = urlsplit(url)
        self.system_path = parts.path.rstrip("/")
        self.client = HttpsClient(f"https://{parts.netloc}", trust, timeout)
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.auth = {"Authorization": f"Basic {basic}"}

    # -- plumbing ------------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Optional[dict] = None, ok=(200, 202, 204)):
        data = None if body is None else json.dumps(body).encode("utf-8")
        try:
            resp = self.client.request(method, path, self.auth, data)
        except ProbeError as exc:
            raise RedfishError(str(exc)) from None
        if resp.status not in ok:
            raise RedfishError(f"{self.client.where}: {method} {path} answered HTTP {resp.status}")
        if resp.status == 202 and method != "GET":
            self._wait_task(method, path, resp.headers.get("location"))
        return resp

    def _wait_task(self, method: str, path: str, monitor: Optional[str]) -> None:
        """An accepted request runs as a task: poll its monitor until it's done."""
        if not monitor:
            return
        monitor = urlsplit(monitor).path or monitor
        deadline = time.monotonic() + self.task_timeout
        while True:
            try:
                resp = self.client.request("GET", monitor, self.auth)
            except ProbeError as exc:
                raise RedfishError(str(exc)) from None
            state = None
            if resp.status == 200:
                try:
                    state = resp.json().get("TaskState")
                except ProbeError:
                    state = None
            if resp.status in (200, 204) and state in (None, "Completed"):
                return
            if state in ("Exception", "Killed", "Cancelled") or resp.status not in (200, 202, 204):
                raise RedfishError(f"{self.client.where}: {method} {path} failed (task {state or resp.status})")
            if time.monotonic() > deadline:
                raise RedfishError(f"{self.client.where}: {method} {path} still running after {self.task_timeout:g}s")
            time.sleep(self.poll)

    def get(self, path: str) -> dict:
        resp = self._call("GET", path, ok=(200,))
        try:
            return resp.json()
        except ProbeError as exc:
            raise RedfishError(f"{self.client.where}: GET {path}: {exc}") from None

    # -- discovery -----------------------------------------------------------------------------

    def system(self) -> dict:
        return self.get(self.system_path)

    def cd_slot(self) -> Tuple[str, dict]:
        """(path, slot) of the first virtual media slot that takes CD/DVD images."""
        system = self.system()
        collections = []
        if "VirtualMedia" in system:
            collections.append(system["VirtualMedia"]["@odata.id"])
        for mgr in system.get("Links", {}).get("ManagedBy", []):
            manager = self.get(mgr["@odata.id"])
            if "VirtualMedia" in manager:
                collections.append(manager["VirtualMedia"]["@odata.id"])
        if not collections:
            raise RedfishError(f"{self.client.where}: the BMC offers no virtual media")
        for coll in collections:
            for member in self.get(coll).get("Members", []):
                path = member["@odata.id"]
                slot = self.get(path)
                types = [str(t).upper() for t in slot.get("MediaTypes", [])]
                if "CD" in types or "DVD" in types:
                    return path, slot
        raise RedfishError(f"{self.client.where}: the BMC's virtual media takes no CD or DVD images")

    # -- actions -------------------------------------------------------------------------------

    @staticmethod
    def _action(resource: dict, name: str) -> Optional[str]:
        target = resource.get("Actions", {}).get(name, {}).get("target")
        return str(target) if target else None

    def eject(self, path: str, slot: dict) -> None:
        target = self._action(slot, "#VirtualMedia.EjectMedia")
        if target:
            self._call("POST", target, {})
        else:
            self._call("PATCH", path, {"Image": None, "Inserted": False})

    def insert(self, path: str, slot: dict, image: str) -> None:
        target = self._action(slot, "#VirtualMedia.InsertMedia")
        if target:
            self._call("POST", target, {"Image": image, "Inserted": True, "WriteProtected": True})
        else:
            self._call("PATCH", path, {"Image": image, "Inserted": True, "WriteProtected": True})

    def boot_once_from_cd(self) -> None:
        self._call("PATCH", self.system_path, {"Boot": {
            "BootSourceOverrideTarget": "Cd",
            "BootSourceOverrideEnabled": "Once",
            "BootSourceOverrideMode": "UEFI",
        }})

    def reset(self) -> str:
        """ForceRestart when the server is on, On when it's off. Returns the reset type sent."""
        system = self.system()
        power = system.get("PowerState")
        if power not in ("On", "Off"):
            raise RedfishError(f"{self.client.where}: the server is {power}; run again once it is On or Off")
        reset_type = "ForceRestart" if power == "On" else "On"
        target = self._action(system, "#ComputerSystem.Reset") or f"{self.system_path}/Actions/ComputerSystem.Reset"
        self._call("POST", target, {"ResetType": reset_type})
        return reset_type

    def state(self) -> Dict[str, Optional[str]]:
        system = self.system()
        boot = system.get("Boot", {})
        return {"PowerState": system.get("PowerState"),
                "BootSourceOverrideTarget": boot.get("BootSourceOverrideTarget"),
                "BootSourceOverrideEnabled": boot.get("BootSourceOverrideEnabled")}

    def attached(self, image: str) -> Dict[str, object]:
        """state() plus whether image is the media in the CD slot. Reads only."""
        _, slot = self.cd_slot()
        return dict(self.state(), attached=bool(slot.get("Inserted")) and slot.get("Image") == image)

    def members(self, path: str) -> list:
        """Every member of a collection, across all its pages (Members@odata.nextLink). Raises
        RedfishError when a page can't be read, a page has no Members (first page or later, even
        one with only a nextLink), Members isn't an array, or the pages don't end. Only an explicit
        Members: [] is an empty collection: a missing Members can't be told from a page the BMC
        left out, so callers must treat it as unreadable."""
        try:
            page = self.get(path)
            members = list(_members(_required_members(page)))
            seen = {path}
            while page.get("Members@odata.nextLink"):
                link = page["Members@odata.nextLink"]
                if link in seen or len(seen) > MAX_PAGES:
                    raise RedfishError(f"{self.client.where}: the list at {path} doesn't end ({link})")
                seen.add(link)
                page = self.get(link)
                members += _members(_required_members(page))
            return members
        except (TypeError, AttributeError) as exc:
            raise RedfishError(f"{self.client.where}: the list at {path} makes no sense ({exc})") from None

    def os_boot_entries(self) -> Optional[List[str]]:
        """Names of UEFI boot options that may load an installed OS (is_os_entry); [] when the
        BMC lists options and none does. None when the BMC lists no boot options at all: no
        BootOptions collection, or one with no members, so nothing can be told either way and
        the caller needs --first-install. Reads only.

        Every page is read (Members@odata.nextLink). Raises RedfishError when the list can't be
        read in full, its Members isn't an array, or a member makes no sense: the caller must then
        assume an OS may be there."""
        coll = (self.system().get("Boot") or {}).get("BootOptions")
        if not coll:
            return None
        try:
            members = self.members(coll["@odata.id"])
            if not members:
                return None
            found = []
            for member in members:
                opt = self.get(member["@odata.id"])
                if is_os_entry(str(opt.get("DisplayName") or ""), str(opt.get("UefiDevicePath") or "")):
                    found.append(str(opt.get("DisplayName") or opt.get("Id") or member["@odata.id"]))
            return found
        except (KeyError, TypeError, AttributeError) as exc:
            raise RedfishError(f"{self.client.where}: a boot option makes no sense ({type(exc).__name__}: {exc})") from None

    def disarm(self) -> List[str]:
        """Best effort: eject the CD and clear the boot override, so the server doesn't boot the
        installer at its next power-on. Returns what was done, failures included; never raises."""
        done = []
        try:
            path, slot = self.cd_slot()
            if slot.get("Inserted"):
                self.eject(path, slot)
                done.append("ejected the installer ISO")
        except RedfishError as exc:
            done.append(f"couldn't eject the installer ISO ({exc}); it will boot at the next power-on")
        try:
            self._call("PATCH", self.system_path, {"Boot": {"BootSourceOverrideEnabled": "Disabled"}})
            done.append("cleared the one-time boot override")
        except RedfishError as exc:
            done.append(f"couldn't clear the boot override ({exc})")
        return done

    def release(self, image: str, clear_override: bool) -> Tuple[List[str], List[str], Optional[str]]:
        """After an install: eject image if it is still in the CD slot and, when clear_override
        (DCiaB set it), clear a boot override that still points at Cd, so a fallback to CD never
        boots the installer again. Other media are left alone. Returns (done, failed, other):
        other is the Image of media left in the slot because it isn't image, else None. Never raises."""
        done: List[str] = []
        failed: List[str] = []
        other: Optional[str] = None
        try:
            path, slot = self.cd_slot()
            if slot.get("Inserted") and slot.get("Image") == image:
                self.eject(path, slot)
                done.append(f"ejected {image}")
            elif slot.get("Inserted"):
                other = str(slot.get("Image"))
        except RedfishError as exc:
            failed.append(f"couldn't eject {image} ({exc})")
        if clear_override:
            try:
                boot = self.system().get("Boot") or {}
                if boot.get("BootSourceOverrideTarget") == "Cd" and \
                        boot.get("BootSourceOverrideEnabled") not in (None, "Disabled"):
                    self._call("PATCH", self.system_path, {"Boot": {"BootSourceOverrideEnabled": "Disabled"}})
                    done.append("cleared the boot override from Cd")
            except RedfishError as exc:
                failed.append(f"couldn't clear the boot override from Cd ({exc})")
        return done, failed, other

    def prepare(self, image: str) -> List[str]:
        """Attach image (ejecting anything else) and set a one-time boot from it. No reset."""
        done = []
        path, slot = self.cd_slot()
        if slot.get("Inserted") and slot.get("Image") == image:
            done.append(f"{image} already attached")
        else:
            if slot.get("Inserted"):
                self.eject(path, slot)
                done.append("ejected the attached media")
            self.insert(path, slot, image)
            done.append(f"attached {image}")
        self.boot_once_from_cd()
        done.append("one-time boot from Cd (UEFI)")
        return done

    def install_from(self, image: str) -> List[str]:
        """prepare(image), then reset. Returns what was done. Callers decide whether a reset is safe."""
        done = self.prepare(image)
        done.append(f"reset ({self.reset()})")
        return done

    # -- preparation before an install (prepare.py) ----------------------------------------------

    def bios(self) -> Optional[dict]:
        """The system's Bios resource (Attributes, and @Redfish.Settings when it has a pending
        settings object), or None when the BMC offers none. Reads only."""
        link = (self.system().get("Bios") or {}).get("@odata.id")
        if not link:
            return None
        body = self.get(link)
        body.setdefault("@odata.id", link)
        return body

    def set_bios(self, bios: dict, attrs: Dict[str, object]) -> None:
        """Stage BIOS attributes to apply at the next reset: PATCH the settings object the Bios
        resource names (Bios/Settings by convention), with ApplyTime OnReset when it's offered."""
        settings = bios.get("@Redfish.Settings") or {}
        target = (settings.get("SettingsObject") or {}).get("@odata.id") or f"{bios['@odata.id'].rstrip('/')}/Settings"
        body: Dict[str, object] = {"Attributes": attrs}
        if "OnReset" in (settings.get("SupportedApplyTimes") or []):
            body["@Redfish.SettingsApplyTime"] = {"ApplyTime": "OnReset"}
        self._call("PATCH", target, body)

    def secure_boot(self) -> Optional[Tuple[str, dict]]:
        """(path, resource) of ComputerSystem.SecureBoot, or None when the BMC offers none."""
        link = (self.system().get("SecureBoot") or {}).get("@odata.id")
        if not link:
            return None
        return link, self.get(link)

    def set_secure_boot(self, path: str, enable: bool) -> None:
        """SecureBootEnable takes effect at the next reset."""
        self._call("PATCH", path, {"SecureBootEnable": enable})

    def drives(self) -> Optional[List[dict]]:
        """Every drive and volume under the system's Storage (volumes, a RAID controller's
        virtual disks, are marked "_volume": True), or None when the BMC lists no Storage.
        Every page of the Storage and Volumes collections is read (Bmc.members). Raises
        RedfishError when the list can't be read in full."""
        link = (self.system().get("Storage") or {}).get("@odata.id")
        if not link:
            return None
        out: List[dict] = []
        try:
            for member in self.members(link):
                storage = self.get(member["@odata.id"])
                for d in storage.get("Drives") or []:
                    out.append(self.get(d["@odata.id"]))
                vols = (storage.get("Volumes") or {}).get("@odata.id")
                if vols:
                    for v in self.members(vols):
                        out.append(dict(self.get(v["@odata.id"]), _volume=True))
        except (KeyError, TypeError, AttributeError) as exc:
            raise RedfishError(f"{self.client.where}: the storage list makes no sense ({type(exc).__name__}: {exc})") from None
        return out
