#!/usr/bin/env python3
"""dciab hypervisors against an emulated bare server (hack/e2e/hypervisors/README.md).

    run.py WORK          (as root, after setup.sh MODE WORK and with the Proxmox VE ISO in place)

Writes the site config, runs dciab services, then the scenarios for the mode setup.sh set up, in
order, stopping at the first failure. Writes WORK/report.json either way and exits 0 only when
every scenario passed.

untagged (managementVLAN untagged, bootMode UEFI, the VM on an access port):
  disk-refused         a hinted serial number no drive has: refused before anything is written
  held-without-flag    no install record, no boot options: held until --first-install
  first-install        validate passes before it (token and certificate pending); --first-install:
                       installs, waits, post (token, ACLs, bridge, first-contact pin, the token
                       written where tokenSecret points); validate --live then uses both; then
                       dciab templates publishes a tiny node disk as root@pam (published, then kept)
  rerun                the API answers: nothing is reset
  lost-state           the install record removed: the 8006 check still holds the host
  reinstall            --reinstall: a new install, post again with the new certificate, old token retired
tagged (managementVLAN tagged, bootMode UEFISecureBoot, the node VLAN tagged only on the VM's port):
  port-is-tagged-only  the switch carries the node VLAN tagged only, no untagged VLAN, on the VM's port
  held-without-flag
  first-install        Secure Boot staged on; the first-boot hook puts the address on vmbr0.<vlan>
  reboot               after a reboot the host comes back on vmbr0.<vlan>, and a rerun resets nothing
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
HOST_ADDR = "10.30.110.6"
TIMEOUT = 1200      # dciab hypervisors --timeout; an install here takes about 3.5 minutes


class Failed(Exception):
    pass


def sh(*args: str, check: bool = True, env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True, env=env, timeout=timeout)
    if check and r.returncode != 0:
        raise Failed(f"{' '.join(args)} exited {r.returncode}: {r.stderr.strip()[-2000:]}")
    return r


class Lab:
    def __init__(self, work: Path):
        self.work = work
        self.env = dict(line.split("=", 1) for line in (work / "env").read_text().split())
        self.mode = self.env["MODE"]
        self.vm = self.env["VM"]
        self.site = work / "site"
        self.state = self.site / ".dciab" / "state"
        self.ctx = ssl.create_default_context(cafile=str(work / "bmc.crt"))
        self.watching = threading.Event()
        self.stop = threading.Event()
        self.log_lines: list[str] = []

    # -- the emulated BMC and VM ------------------------------------------------------------------

    def redfish(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"{self.env['BMC_URL']}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.ctx))
        with opener.open(req, timeout=60) as resp:
            text = resp.read().decode() if resp.status != 204 else ""
        return json.loads(text) if text else {}

    def system_path(self) -> str:
        return f"/redfish/v1/Systems/{self.env['VM_UUID']}"

    def domstate(self) -> str:
        return sh("virsh", "-c", "qemu:///system", "domstate", self.vm).stdout.strip()

    def qemu_pid(self) -> str:
        pidfile = Path(f"/run/libvirt/qemu/{self.vm}.pid")
        return pidfile.read_text().strip() if pidfile.exists() else ""

    def cd_inserted(self) -> bool:
        sysd = self.redfish("GET", self.system_path())
        coll = self.redfish("GET", sysd["VirtualMedia"]["@odata.id"])
        for m in coll.get("Members", []):
            slot = self.redfish("GET", m["@odata.id"])
            if "CD" in [str(t).upper() for t in slot.get("MediaTypes", [])]:
                return bool(slot.get("Inserted"))
        return False

    def note(self, line: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {line}"
        self.log_lines.append(line)
        print(line, flush=True)

    def watcher(self) -> None:
        """A BMC's one-time boot: the VM stops at the end of each boot (on_reboot=destroy); while
        watching, a VM that ran and then stayed stopped for 10 s boots again, from its disk once it
        has booted from Cd. A ForceRestart (destroy, then create at once) never counts."""
        ran = False
        while not self.stop.is_set():
            if not self.watching.is_set():
                ran = False
            else:
                try:
                    state = self.domstate()
                    if state == "running":
                        ran = True
                    elif state == "shut off" and ran and not self.stop.wait(10) and self.domstate() == "shut off":
                        ran = False
                        boot = self.redfish("GET", self.system_path()).get("Boot", {})
                        if boot.get("BootSourceOverrideTarget") != "Hdd":
                            self.redfish("PATCH", self.system_path(), {"Boot": {"BootSourceOverrideTarget": "Hdd"}})
                            self.note("watcher: the boot from Cd ended; next boot from Hdd (one-time boot)")
                        self.redfish("POST", f"{self.system_path()}/Actions/ComputerSystem.Reset", {"ResetType": "On"})
                        self.note("watcher: powered the VM on again (its guest rebooted)")
                except Exception as exc:  # noqa: BLE001 - keep watching; the scenario times out if it matters
                    self.note(f"watcher: {exc}")
            self.stop.wait(5)

    # -- the site ---------------------------------------------------------------------------------

    def write_site(self, site: Path, serial: str) -> Path:
        doc = yaml.safe_load((ROOT / "examples" / "proxmox-bare.yaml").read_text())
        spec = doc["spec"]
        keygen = self.work / "bin" / "age-keygen"
        recipients = []
        for i in (1, 2):
            ident = self.work / f"id{i}.txt"
            if not ident.exists():
                sh(str(keygen), "-o", str(ident))
            recipients.append(sh(str(keygen), "-y", str(ident)).stdout.strip())
        spec["bootstrap"]["interface"] = self.env["BOOT_MAC"]
        spec["bootstrap"]["recipients"] = recipients
        # The URL names pve01, and the config has no tlsFingerprint and no token file: validate
        # reports both as pending until post pins the certificate it sees on first contact and
        # writes the token to ./secrets/pve/token (the bare-site flow, #276 and #279).
        spec["platform"]["proxmox"]["url"] = f"https://{HOST_ADDR}:8006"
        spec["platform"]["proxmox"].pop("tlsFingerprint", None)
        spec["platform"]["proxmox"]["credentials"]["tokenSecret"] = {"file": "./secrets/pve/token"}
        der = ssl.PEM_cert_to_DER_cert((self.work / "bmc.crt").read_text())
        pin = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        hv = spec["hypervisors"]
        hv["bootMode"] = "UEFI" if self.mode == "untagged" else "UEFISecureBoot"
        hv["managementVLAN"] = self.mode
        if self.mode == "tagged":
            spec["platform"]["proxmox"]["vlanTag"] = int(self.env["VLAN"])
        assert spec["network"]["nodes"]["vlan"] == int(self.env["VLAN"])
        host = dict(hv["hosts"][0])
        host.update(address=HOST_ADDR, bootMACAddress=self.env["VM_MAC"], rootDeviceHints={"serialNumber": serial})
        host["bmc"] = {"address": f"redfish-virtualmedia+{self.env['BMC_URL']}{self.system_path()}",
                       "tlsFingerprint": pin,
                       "credentials": {"username": "dciab", "password": {"file": "./secrets/bmc/pve01"}}}
        hv["hosts"] = [host]
        site.mkdir(parents=True, exist_ok=True)
        for s in ("bmc/pve01", "pki/intermediate.key", "pki/chain.pem"):
            p = site / "secrets" / s
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("emulator\n")
            p.chmod(0o600)
        for d in (site / "secrets", *(site / "secrets").iterdir()):
            d.chmod(0o700)                       # post refuses folders others can write (#280)
        sh("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
           "-keyout", "/dev/null", "-subj", "/CN=dciab-emulator", "-days", "3650",
           "-addext", "basicConstraints=critical,CA:TRUE", "-out", str(site / "secrets/pki/intermediate.crt"))
        (site / "dciab.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
        return site / "dciab.yaml"

    def dciab(self, *args: str, timeout: float = TIMEOUT + 600) -> subprocess.CompletedProcess:
        env = dict(os.environ, DCIAB_PVE_TOKEN="emulator", DCIAB_PVE_CSI_TOKEN="emulator",
                   PATH=f"{self.work / 'bin'}:{os.environ['PATH']}")
        self.note(f"$ dciab {' '.join(args)}")
        r = subprocess.run([str(ROOT / "dciab"), *args], capture_output=True, text=True, env=env, timeout=timeout)
        out = r.stdout + r.stderr
        (self.work / "logs").mkdir(exist_ok=True)
        with open(self.work / "logs" / "dciab.log", "a") as f:
            f.write(f"\n$ dciab {' '.join(args)}\n{out}\n[exit {r.returncode}]\n")
        print(out, flush=True)
        r.out = out  # type: ignore[attr-defined]
        return r

    def hv(self, *extra: str, config: Path | None = None) -> subprocess.CompletedProcess:
        return self.dciab("hypervisors", str(config or self.config), "--age-identity", str(self.work / "id1.txt"),
                          "--timeout", str(TIMEOUT), *extra)

    def record(self) -> dict:
        return json.loads((self.state / "installs" / f"{self.vm}.json").read_text())

    def api_token_get(self, path: str) -> object:
        """GET through the Proxmox VE API with the CAPMOX token post created (opened with age)."""
        sealed = self.state / "proxmox" / f"{self.vm}.token.age"
        secret = sh(str(self.work / "bin" / "age"), "-d", "-i", str(self.work / "id1.txt"), str(sealed)).stdout.strip()
        token_id = "dciab@pve!capmox"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # the pin is post's job; this only proves the token works
        req = urllib.request.Request(f"https://{HOST_ADDR}:8006/api2/json{path}",
                                     headers={"Authorization": f"PVEAPIToken={token_id}={secret}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())["data"]

    def token_file(self) -> Path:
        return self.config.parent / "secrets" / "pve" / "token"   # type: ignore[attr-defined]

    def sealed_secret(self) -> str:
        sealed = self.state / "proxmox" / f"{self.vm}.token.age"
        return sh(str(self.work / "bin" / "age"), "-d", "-i", str(self.work / "id1.txt"), str(sealed)).stdout.strip()

    def wait_api(self, timeout: float = 900) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = sh("curl", "-sk", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
                   f"https://{HOST_ADDR}:8006/api2/json/version", check=False)
            if r.stdout.strip() in ("200", "401"):
                return
            time.sleep(10)
        raise Failed(f"the Proxmox VE API on {HOST_ADDR} didn't answer within {timeout:.0f} s")


def expect(cond: bool, what: str) -> None:
    if not cond:
        raise Failed(what)


# -- scenarios -----------------------------------------------------------------------------------

def needs_installed_host(scenario):
    """Marks a scenario that runs on the host first_install installed. The tests check that
    every leg running one of these runs first_install before it."""
    scenario.needs_installed_host = True
    return scenario


def disk_refused(lab: Lab) -> dict:
    config = lab.write_site(lab.work / "site-wrong-serial", "NOT-ON-THIS-SERVER")
    r = lab.hv("--until", "media", "--first-install", lab.vm, "--isos", str(lab.state / "isos"), config=config)
    expect(r.returncode == 1, f"exit {r.returncode}, want 1")
    expect("no drive the BMC lists matches" in r.out, "no disk refusal in the output")
    expect(lab.domstate() == "shut off", "the VM was powered on")
    expect(not lab.cd_inserted(), "an ISO was attached")
    return {"refused": True}


def held_without_flag(lab: Lab) -> dict:
    r = lab.hv("--until", "media")
    expect(r.returncode == 1, f"exit {r.returncode}, want 1")
    expect(f"pass --first-install {lab.vm}" in r.out, "no --first-install hold in the output")
    expect(lab.domstate() == "shut off", "the VM was powered on")
    expect(not lab.cd_inserted(), "an ISO was attached")
    return {"held": True}


def pending_before_install(lab: Lab) -> None:
    """Before the install, validate passes, with the token and the certificate pending (#276)."""
    r = lab.dciab("validate", str(lab.config))
    expect(r.returncode == 0, f"validate before the install exited {r.returncode}, want 0")
    expect("pending: the CAPMOX token" in r.out, "validate didn't report the token as pending")
    expect("pending: the Proxmox API's certificate" in r.out, "validate didn't report the certificate as pending")


def token_and_pin_used(lab: Lab) -> dict:
    """After post: the token is where tokenSecret points (mode 600) and validate --live trusts
    the API by the install record's pin with it. Only the node template may be missing (the
    template check runs once the node reports online, which a fresh install may not yet)."""
    tf = lab.token_file()
    expect(tf.is_file() and tf.stat().st_mode & 0o777 == 0o600, f"post didn't write {tf} (mode 600)")
    expect(tf.read_text().strip() == lab.sealed_secret(), "the token file doesn't hold the sealed token")
    r = lab.dciab("validate", str(lab.config), "--live", "--output", "json")
    report = json.loads(r.stdout)
    px = [f for f in report["findings"] if f["severity"] == "error" and f["path"].startswith("spec.platform.proxmox")]
    expect({f["path"] for f in px} <= {"spec.platform.proxmox.templateID"},
           f"validate --live's Proxmox errors: {[(f['path'], f['found']) for f in px]}")
    expect(any("accepted token" in n for n in report["notes"]), "validate --live didn't use the token")
    expect(any("has every privilege" in n for n in report["notes"]), "validate --live didn't check the token")
    expect(any("pinned in its install record" in n for n in report["notes"]), "validate didn't use the pin")
    return {"tokenFile": str(tf), "liveProxmoxErrors": [f["path"] for f in px]}


def node_template(lab: Lab) -> dict:
    """dciab templates against the real Proxmox VE post set up (#244): as root@pam with the
    generated root password (a host DCiaB installed, no templateImport), from a payload that
    build/archive.py nodes --local-disk lays out around a tiny qcow2. It must report published,
    then kept on a second run."""
    disk = lab.work / "node-disk" / "disk.qcow2"
    if not disk.is_file():
        disk.parent.mkdir(parents=True, exist_ok=True)
        if not shutil.which("qemu-img"):
            raise Failed("qemu-img is needed to make the test node disk (qemu-utils)")
        sh("qemu-img", "create", "-f", "qcow2", str(disk), "64M")
    payload = lab.work / "payload"
    sh(sys.executable, str(ROOT / "build" / "archive.py"), "nodes", "--out", str(payload),
       "--local-disk", f"proxmox={disk}")
    px = yaml.safe_load(lab.config.read_text())["spec"]["platform"]["proxmox"]  # type: ignore[attr-defined]
    vmid, node = px["templateID"], px["templateNode"]
    args = ("templates", str(lab.config), "--state", str(lab.state), "--payload", str(payload),  # type: ignore[attr-defined]
            "--age-identity", str(lab.work / "id1.txt"), "--timeout", "900")
    r = lab.dciab(*args, timeout=1200)
    if r.returncode != 0:
        raise Failed(f"dciab templates exited {r.returncode}, want 0; {curl_upload(lab, disk)}")
    expect(f"template {vmid} on {node}: published" in r.out, "dciab templates didn't report published")
    expect("root@pam" in r.out, "dciab templates didn't log in as root@pam")
    r = lab.dciab(*args, timeout=600)
    expect(r.returncode == 0, f"the second dciab templates exited {r.returncode}, want 0")
    expect(f"template {vmid} on {node}: kept" in r.out, "the second dciab templates didn't keep the template")
    return {"templateID": vmid, "node": node, "results": ["published", "kept"]}


def curl_upload(lab: Lab, disk: Path) -> str:
    """Diagnostics only, when dciab templates fails: the same upload by curl as root@pam, so the
    run says whether Proxmox VE takes it from another client (the status line holds its message).
    The root password goes to curl on stdin, never in its arguments (an error or a timeout prints
    those), and both requests have a time limit, so the original failure is always reported."""
    try:
        sealed = lab.state / "answers" / f"{lab.vm}.root-password.age"
        pw = sh(str(lab.work / "bin" / "age"), "-d", "-i", str(lab.work / "id1.txt"), str(sealed),
                timeout=30).stdout.rstrip("\n")
        base = f"https://{HOST_ADDR}:8006/api2/json"
        r = subprocess.run(["curl", "-sk", "--noproxy", "*", "--max-time", "30", "--data-urlencode", "username=root@pam",
                            "--data-urlencode", "password@-", f"{base}/access/ticket"],
                           input=pw, capture_output=True, text=True, timeout=60)
        del pw
        if r.returncode != 0:
            return f"curl's ticket request as root@pam failed (curl exit {r.returncode})"
        t = json.loads(r.stdout)["data"]
        r = sh("curl", "-sk", "--noproxy", "*", "--max-time", "300", "-D", "-", "-o", "/dev/stderr",
               "-H", f"CSRFPreventionToken: {t['CSRFPreventionToken']}", "-b", f"PVEAuthCookie={t['ticket']}",
               "-F", "content=import", "-F", f"filename=@{disk};filename=dciab-curl-test.qcow2",
               f"{base}/nodes/{lab.vm}/storage/local/upload", check=False, timeout=360)
        status = (r.stdout.splitlines() or ["no answer"])[0].strip()
        return f"curl's upload as root@pam: {status} {r.stderr.strip()[-300:]}"
    except subprocess.TimeoutExpired as exc:
        return f"curl's diagnostic request didn't finish within {exc.timeout:g}s"
    except Exception as exc:  # noqa: BLE001 - diagnostics must never hide the failure
        return f"curl's upload couldn't run: {type(exc).__name__}"


def first_install(lab: Lab) -> dict:
    lab.watching.set()
    pending_before_install(lab)
    started = time.time()
    r = lab.hv("--first-install", lab.vm)
    expect(r.returncode == 0, f"exit {r.returncode}, want 0")
    expect("--first-install: no install recorded" in r.out, "the install didn't go through --first-install")
    rec = lab.record()
    expect(rec.get("outcome") == "installed", f"install record outcome {rec.get('outcome')}")
    expect(rec.get("post") == "done", f"post {rec.get('post')}")
    expect(bool(rec.get("api_fingerprint")), "no pinned API certificate in the install record")
    expect((lab.state / "proxmox" / f"{lab.vm}.token.age").is_file(), "no sealed token")
    version = lab.api_token_get("/version")
    nodes = sorted(n.get("node") for n in lab.api_token_get("/nodes") or [])
    expect(nodes == [lab.vm], f"the token sees nodes {nodes}")
    out = {"minutes": round((time.time() - started) / 60, 1), "pveVersion": version.get("version"),
           "api_fingerprint": rec["api_fingerprint"], "tokenSeesNodes": nodes, "bareSite": token_and_pin_used(lab),
           "nodeTemplate": node_template(lab)}
    if lab.mode == "tagged":
        expect("Secure Boot set on" in r.out, "Secure Boot wasn't staged")
        sb = lab.redfish("GET", f"{lab.system_path()}/SecureBoot")
        expect(sb.get("SecureBootEnable") is True, "the BMC doesn't report Secure Boot on")
        out["secureBoot"] = check_secure_boot_domain(lab)
        out["vmbr0"] = check_tagged_network(lab)
    lab.first_fingerprint = rec["api_fingerprint"]  # type: ignore[attr-defined]
    return out


def check_secure_boot_domain(lab: Lab) -> dict:
    """What the VM really boots with, read from libvirt (not from sushy-tools' Redfish answer): a
    Secure Boot loader, and NVRAM from the template with Microsoft's keys."""
    xml = sh("virsh", "-c", "qemu:///system", "dumpxml", lab.vm).stdout
    loader = re.search(r"<loader\b[^>]*>", xml)
    nvram = re.search(r"<nvram\b([^>]*)>([^<]*)</nvram>", xml)
    expect(bool(loader) and "secure='yes'" in loader.group(0), f"the VM's loader isn't secure: {loader and loader.group(0)}")
    expect(bool(nvram) and f"template='{lab.env['OVMF_VARS_MS']}'" in nvram.group(1),
           f"the VM's NVRAM doesn't come from the Microsoft-keys template: {nvram and nvram.group(0)}")
    expect(nvram.group(2).endswith(".secboot.fd"), f"the VM's NVRAM file isn't the Secure Boot one: {nvram.group(2)}")
    return {"loader": loader.group(0), "nvram": nvram.group(2)}


def check_tagged_network(lab: Lab) -> dict:
    vid = lab.env["VLAN"]
    ifaces = {i.get("iface"): i for i in lab.api_token_get(f"/nodes/{lab.vm}/network") or []}
    vlan_if = ifaces.get(f"vmbr0.{vid}") or {}
    bridge = ifaces.get("vmbr0") or {}
    expect(str(vlan_if.get("cidr", "")).startswith(HOST_ADDR), f"vmbr0.{vid} doesn't hold {HOST_ADDR}: {vlan_if}")
    expect(str(bridge.get("bridge_vlan_aware")) in ("1", "True", "true"), f"vmbr0 isn't VLAN-aware: {bridge}")
    expect(not bridge.get("cidr"), f"vmbr0 still holds an address: {bridge.get('cidr')}")
    return {"vlanInterface": f"vmbr0.{vid}", "cidr": vlan_if.get("cidr"), "gateway": vlan_if.get("gateway")}


@needs_installed_host
def rerun(lab: Lab) -> dict:
    pid = lab.qemu_pid()
    r = lab.hv()
    expect(r.returncode == 0, f"exit {r.returncode}, want 0")
    expect("already answers" in r.out, "the rerun didn't see the installed host")
    expect(lab.qemu_pid() == pid, "the VM was reset")
    return {"reset": False}


@needs_installed_host
def lost_state(lab: Lab) -> dict:
    rec = lab.state / "installs" / f"{lab.vm}.json"
    aside = lab.work / "install-record.json"
    shutil.move(str(rec), aside)
    try:
        pid = lab.qemu_pid()
        r = lab.hv("--until", "media")
        expect(r.returncode == 0, f"exit {r.returncode}, want 0")
        expect("already answers" in r.out, "the 8006 check didn't hold the host")
        expect(lab.qemu_pid() == pid, "the VM was reset")
    finally:
        shutil.move(str(aside), rec)
    return {"reset": False}


def power_off(lab: Lab) -> None:
    lab.watching.clear()
    sh("virsh", "-c", "qemu:///system", "destroy", lab.vm, check=False)
    expect(lab.domstate() == "shut off", "the VM didn't power off")


def power_on_installed(lab: Lab) -> None:
    lab.redfish("POST", f"{lab.system_path()}/Actions/ComputerSystem.Reset", {"ResetType": "On"})
    lab.watching.set()
    lab.wait_api()


@needs_installed_host
def lost_state_host_off(lab: Lab) -> dict:
    """The risky case: no install record and the host off, so no API can answer. sushy-tools
    lists no boot options, so the host must be held until --first-install, nothing written."""
    power_off(lab)
    rec = lab.state / "installs" / f"{lab.vm}.json"
    aside = lab.work / "install-record.json"
    shutil.move(str(rec), aside)
    try:
        r = lab.hv("--until", "media")
        expect(r.returncode == 1, f"exit {r.returncode}, want 1")
        expect(f"pass --first-install {lab.vm}" in r.out, "no --first-install hold in the output")
        expect(lab.domstate() == "shut off", "the VM was powered on")
        expect(not lab.cd_inserted(), "an ISO was attached")
    finally:
        shutil.move(str(aside), rec)
    return {"held": True}


@needs_installed_host
def recorded_host_off(lab: Lab) -> dict:
    """An install recorded and the host off: held (the API doesn't answer), never reinstalled."""
    power_off(lab)
    try:
        r = lab.hv("--until", "media")
        expect(r.returncode == 1, f"exit {r.returncode}, want 1")
        expect("install recorded (installed" in r.out and "doesn't answer" in r.out, "no recorded-install hold in the output")
        expect(lab.domstate() == "shut off", "the VM was powered on")
        expect(not lab.cd_inserted(), "an ISO was attached")
    finally:
        power_on_installed(lab)
    return {"held": True}


@needs_installed_host
def reinstall(lab: Lab) -> dict:
    pid = lab.qemu_pid()
    started = time.time()
    r = lab.hv("--reinstall", lab.vm)
    expect(r.returncode == 0, f"exit {r.returncode}, want 0")
    expect(lab.qemu_pid() != pid, "the VM wasn't reset")
    rec = lab.record()
    expect(rec.get("outcome") == "installed" and rec.get("post") == "done", f"record {rec.get('outcome')}/{rec.get('post')}")
    expect(rec.get("api_fingerprint") and rec["api_fingerprint"] != getattr(lab, "first_fingerprint", None),
           "post didn't pin the new install's certificate")
    retired = sorted(p.name for p in (lab.state / "proxmox").glob(f"{lab.vm}.token.age.replaced-*"))
    expect(bool(retired), "the old token wasn't moved aside")
    lab.api_token_get("/version")
    expect(lab.token_file().read_text().strip() == lab.sealed_secret(), "post didn't write the new token's secret")
    return {"minutes": round((time.time() - started) / 60, 1), "retiredTokens": retired}


def port_is_tagged_only(lab: Lab) -> dict:
    # Read from the switch: the VM's port must carry the node VLAN tagged and nothing untagged.
    lab.watching.clear()
    r = sh("virsh", "-c", "qemu:///system", "start", lab.vm)
    try:
        time.sleep(3)
        show = sh("bridge", "-j", "vlan", "show", "dev", lab.env["TAP"]).stdout
        vlans = [v for p in json.loads(show) for v in p.get("vlans", [])]
        expect([v.get("vlan") for v in vlans] == [int(lab.env["VLAN"])], f"VLANs on the VM's port: {vlans}")
        expect(not any("PVID" in v.get("flags", []) or "Egress Untagged" in v.get("flags", []) for v in vlans),
               f"the node VLAN is untagged on the VM's port: {vlans}")
    finally:
        sh("virsh", "-c", "qemu:///system", "destroy", lab.vm, check=False)
        del r
    return {"vlans": vlans}


@needs_installed_host
def reboot(lab: Lab) -> dict:
    pid = lab.qemu_pid()
    lab.watching.set()
    sh("virsh", "-c", "qemu:///system", "reboot", lab.vm)     # the guest reboots; the watcher powers it on again
    deadline = time.time() + 600
    while lab.qemu_pid() in ("", pid) and time.time() < deadline:
        time.sleep(5)
    expect(lab.qemu_pid() not in ("", pid), "the VM didn't come back after the reboot")
    lab.wait_api()
    net = check_tagged_network(lab)
    pid = lab.qemu_pid()
    r = lab.hv()
    expect(r.returncode == 0 and "already answers" in r.out, "the rerun after the reboot didn't see the host")
    expect(lab.qemu_pid() == pid, "the rerun after the reboot reset the VM")
    return {"vmbr0": net}


# One list per CI leg (DCIAB_LEG, hypervisors-emulated.yml's matrix). Each leg runs on its own
# runner from a blank VM, so every list that needs an installed host does its own first_install.
# The untagged mode is split in two legs so its two installs run in parallel, not one after the other.
SCENARIOS = {
    # The reinstall leg runs two installs back to back, so the quick pre-install checks run here.
    "untagged-install": [disk_refused, held_without_flag, first_install, rerun, lost_state,
                         lost_state_host_off, recorded_host_off],
    "untagged-reinstall": [first_install, reinstall],
    "tagged": [port_is_tagged_only, held_without_flag, first_install, reboot],
}
# Running run.py by hand without DCIAB_LEG runs every scenario of the mode setup.sh set up.
SCENARIOS["untagged"] = [disk_refused, held_without_flag, first_install, rerun, lost_state,
                         lost_state_host_off, recorded_host_off, reinstall]


def leg_of(lab: "Lab") -> str:
    leg = os.environ.get("DCIAB_LEG") or lab.mode
    if leg not in SCENARIOS or not (leg == lab.mode or leg.startswith(lab.mode + "-")):
        raise Failed(f"leg {leg!r} doesn't fit mode {lab.mode!r} (known legs: {', '.join(sorted(SCENARIOS))})")
    return leg


def main(argv: list[str]) -> int:
    lab = Lab(Path(argv[0]).resolve())
    report = {"mode": lab.mode, "leg": os.environ.get("DCIAB_LEG") or lab.mode, "scenarios": [], "passed": False}
    threading.Thread(target=lab.watcher, daemon=True).start()
    try:
        leg = leg_of(lab)
        lab.config = lab.write_site(lab.site, lab.env["DISK_SERIAL"])  # type: ignore[attr-defined]
        # The installer ISO dciab services serves and dciab hypervisors builds each host's ISO from.
        isos = lab.state / "isos"
        isos.mkdir(parents=True, exist_ok=True)
        for iso in (lab.work / "isos").glob("*.iso"):
            if not (isos / iso.name).exists():
                os.link(iso, isos / iso.name)
        r = lab.dciab("services", str(lab.config), timeout=900)
        if r.returncode != 0:
            raise Failed(f"dciab services exited {r.returncode}")
        for scenario in SCENARIOS[leg]:
            name = scenario.__name__.replace("_", "-")
            t0 = time.time()
            entry = {"name": name}
            report["scenarios"].append(entry)
            lab.note(f"== {name}")
            try:
                entry["details"] = scenario(lab)
                entry["result"] = "pass"
            except (Failed, subprocess.TimeoutExpired, OSError, KeyError, ValueError) as exc:
                entry["result"] = "fail"
                entry["error"] = str(exc)
                lab.note(f"FAIL {name}: {exc}")
                break
            finally:
                entry["seconds"] = round(time.time() - t0)
                lab.note(f"{entry['result'].upper()} {name} ({entry['seconds']} s)")
        report["passed"] = bool(report["scenarios"]) and all(s["result"] == "pass" for s in report["scenarios"]) \
            and len(report["scenarios"]) == len(SCENARIOS[leg])
    except Failed as exc:
        report["error"] = str(exc)
        lab.note(f"FAIL setup: {exc}")
    finally:
        lab.stop.set()
        report["log"] = lab.log_lines
        (lab.work / "report.json").write_text(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
