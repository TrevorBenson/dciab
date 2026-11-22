"""Redfish virtual media for one BMC: attach an installer ISO, boot it once, reset the server.

Standard library only, over dciab_validate.probes.HttpsClient (certificate pin or CA bundle).
Handles the usual vendor differences: VirtualMedia under the System or under its Manager, and
InsertMedia/EjectMedia actions or a PATCH of the slot.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from dciab_validate.probes import HttpsClient, ProbeError, Trust


class RedfishError(Exception):
    """Something the BMC refused or doesn't offer. The message is safe to print."""


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

    def install_from(self, image: str) -> List[str]:
        """Attach image (ejecting anything else), boot from it once, and reset. Returns what was done."""
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
        done.append(f"reset ({self.reset()})")
        return done
