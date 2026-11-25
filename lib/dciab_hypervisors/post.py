"""Post-install, through the Proxmox VE API: the bridge, the storage and the CAPMOX API token.

Runs as the `post` step for each host DCiaB installed (its install record says `installed`)
that is one of spec.platform.proxmox.nodes. It logs in as root@pam with the root password: the
site's (spec.hypervisors.rootPassword) or the generated one, opened from
<state>/answers/<host>.root-password.age with --age-identity. Nothing uses SSH.

  trust    the API's certificate: spec.platform.proxmox.tlsFingerprint when the platform URL
           names this host, else the fingerprint seen on first contact, recorded in the
           install record (api_fingerprint) and pinned from then on
  network  spec.platform.proxmox.bridge must exist (the installer makes vmbr0 on the
           management NIC). With platform.proxmox.vlanTag it is made VLAN-aware, keeping its
           address, and the network is reloaded. managementVLAN: tagged isn't built: the
           installer's network is untagged, and moving the address onto a VLAN needs the
           switch port changed at the same moment, so the step refuses it
  storage  spec.platform.proxmox.storage must exist on the node, enabled, holding VM disks
           (images); `local` must take ISOs (CAPMOX's bootstrap data), and gains `iso` when not
  token    the roles, user, token and ACLs of lib/dciab_validate/proxmox_privs.py (the one
           source of truth for docs/PROXMOX.md and validate --live). The token is
           privilege-separated; its secret, shown once, is sealed at once to
           spec.bootstrap.recipients in <state>/proxmox/<host>.token.age and never written in
           plain text. Roles whose privileges differ are corrected; an existing token is kept
           only if its sealed secret is here
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

from dciab_validate.probes import HttpsClient, ProbeError, Trust, fingerprint_matches, format_fingerprint


class PostError(Exception):
    """A post-install step that can't go on. The message is safe to print."""


TAGGED = ("managementVLAN: tagged isn't built yet: the installer's network is untagged, and moving the host's "
          "address onto the VLAN needs its switch port changed at the same moment (docs/HYPERVISORS.md). Use an "
          "access port, or a trunk with network.nodes.vlan as the native VLAN, and managementVLAN: untagged")


def refuse_tagged(doc: dict) -> None:
    """managementVLAN: tagged isn't built: refused before anything is written or sent."""
    if (doc["spec"].get("hypervisors") or {}).get("managementVLAN") == "tagged":
        raise PostError(TAGGED)


def seen_certificate(address: str, port: int = 8006, timeout: float = 10.0) -> bytes:
    """The DER certificate address:port presents. Sends nothing."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((address, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=address) as tls:
                der = tls.getpeercert(binary_form=True) or b""
    except (OSError, ssl.SSLError) as exc:
        raise PostError(f"{address}:{port}: can't read the API certificate ({exc})") from None
    if not der:
        raise PostError(f"{address}:{port}: the API presented no certificate")
    return der


def trust_for(host: dict, doc: dict, record: dict, seen: Callable[[str], bytes] = seen_certificate) -> Tuple[str, Optional[str]]:
    """(fingerprint to pin, a note or None). Never trusts the system CAs: a fresh install's
    certificate is self-signed. tlsFingerprint may be SHA-256 or SHA-1, as the schema allows."""
    px = doc["spec"]["platform"]["proxmox"]
    fqdn = f"{host['name']}.{doc['spec']['network']['domain']}"
    url_host = urlsplit(px["url"]).hostname
    observed = record.get("api_fingerprint")
    if url_host in (host["address"], fqdn, host["name"]):
        pin = px["tlsFingerprint"]
        der = seen(host["address"])
        if not fingerprint_matches(der, pin):
            current = format_fingerprint(der)
            raise PostError(f"the API certificate's fingerprint is {current}, not spec.platform.proxmox.tlsFingerprint "
                            f"({pin}). A host DCiaB just installed has a new certificate: set tlsFingerprint to "
                            f"{current} if that is this host's, and run again")
        return pin, None
    if observed:
        return observed, None
    current = format_fingerprint(seen(host["address"]))
    return current, (f"pinned the API certificate seen on first contact, {current} (recorded in the install record); "
                     "every later run checks it")


class PveApi:
    """The Proxmox VE API of one host, logged in with a ticket."""

    def __init__(self, address: str, fingerprint: str, timeout: float = 30.0, port: int = 8006):
        self.client = HttpsClient(f"https://{address}:{port}", Trust(fingerprint=fingerprint), timeout)
        self.headers: Dict[str, str] = {}

    def _send(self, method: str, path: str, form: Optional[dict] = None, ok=(200,)) -> object:
        body = None
        headers = dict(self.headers)
        if form is not None:
            body = urlencode({k: v for k, v in form.items() if v is not None}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self.client.request(method, "/api2/json" + path, headers, body)
        except ProbeError as exc:
            raise PostError(str(exc)) from None
        if resp.status not in ok:
            reason = ""
            try:
                errors = resp.json().get("errors")
                reason = f": {json.dumps(errors)}" if errors else ""
            except (ProbeError, AttributeError, ValueError):
                pass
            raise PostError(f"{self.client.where}: {method} {path} answered HTTP {resp.status}{reason}")
        try:
            return resp.json().get("data") if resp.body else None
        except (ProbeError, AttributeError, ValueError):
            return None

    def login(self, password: str, user: str = "root@pam") -> None:
        try:
            data = self._send("POST", "/access/ticket", {"username": user, "password": password})
        except PostError as exc:
            raise PostError(f"logging in as {user} failed ({exc}); check the root password") from None
        if not isinstance(data, dict) or not data.get("ticket"):
            raise PostError(f"{self.client.where}: logging in as {user} gave no ticket")
        self.headers = {"Cookie": f"PVEAuthCookie={data['ticket']}",
                        "CSRFPreventionToken": str(data.get("CSRFPreventionToken") or "")}

    def get(self, path: str) -> object:
        return self._send("GET", path)

    def post(self, path: str, form: Optional[dict] = None) -> object:
        return self._send("POST", path, form or {})

    def put(self, path: str, form: Optional[dict] = None) -> object:
        return self._send("PUT", path, form or {})

    def exists(self, path: str) -> bool:
        try:
            self._send("GET", path)
            return True
        except PostError as exc:
            if "HTTP 404" in str(exc) or "HTTP 500" in str(exc):   # PVE answers 500 "does not exist" for some
                return False
            raise


# -- the steps ------------------------------------------------------------------------------

def network(api: PveApi, node: str, doc: dict, log: Callable[[str], None], tries: int = 6, delay: float = 5.0,
            sleep: Callable[[float], None] = time.sleep) -> None:
    px = doc["spec"]["platform"]["proxmox"]
    refuse_tagged(doc)
    ifaces = api.get(f"/nodes/{quote(node)}/network") or []
    bridge = next((i for i in ifaces if isinstance(i, dict) and i.get("iface") == px["bridge"]), None)
    if bridge is None or bridge.get("type") != "bridge":
        raise PostError(f"{node} has no bridge {px['bridge']} (spec.platform.proxmox.bridge); the installer makes "
                        "vmbr0 on the management NIC, so name that, or create the bridge before running again")
    if not px.get("vlanTag"):
        log(f"bridge {px['bridge']} present (no vlanTag, so it needn't be VLAN-aware)")
        return
    if str(bridge.get("bridge_vlan_aware") or "0") == "1":
        log(f"bridge {px['bridge']} is VLAN-aware")
        return
    form = {"type": "bridge", "bridge_vlan_aware": 1, "bridge_vids": "2-4094"}
    for key in ("cidr", "gateway", "bridge_ports", "autostart", "comments", "mtu", "cidr6", "gateway6"):
        if bridge.get(key) not in (None, ""):
            form[key] = bridge[key]
    api.put(f"/nodes/{quote(node)}/network/{quote(px['bridge'])}", form)
    api.put(f"/nodes/{quote(node)}/network")        # apply (ifupdown2 reload)
    # The reload can drop the host off the network; make sure the API still answers.
    for attempt in range(tries):
        try:
            api.get("/version")
            break
        except PostError:
            if attempt == tries - 1:
                raise PostError(f"bridge {px['bridge']} was made VLAN-aware, but the network reload left the API "
                                "unreachable; check the host's console through its BMC") from None
            sleep(delay)
    log(f"bridge {px['bridge']} made VLAN-aware for vlanTag {px['vlanTag']}, address kept; network reloaded")


def storage(api: PveApi, node: str, doc: dict, log: Callable[[str], None]) -> None:
    from dciab_validate.proxmox_privs import BOOTSTRAP_ISO_STORAGE
    px = doc["spec"]["platform"]["proxmox"]
    stores = {s.get("storage"): s for s in (api.get(f"/nodes/{quote(node)}/storage") or []) if isinstance(s, dict)}
    want = stores.get(px["storage"])
    if want is None:
        raise PostError(f"{node} has no storage {px['storage']} (spec.platform.proxmox.storage); a fresh install has "
                        f"{', '.join(sorted(k for k in stores if k))}. DCiaB doesn't create other storage yet")
    if str(want.get("enabled", 1)) == "0" or str(want.get("active", 1)) == "0":
        raise PostError(f"storage {px['storage']} on {node} isn't enabled and active")
    if "images" not in str(want.get("content") or "").split(","):
        raise PostError(f"storage {px['storage']} on {node} doesn't hold VM disks (content images)")
    log(f"storage {px['storage']} holds VM disks")
    iso = stores.get(BOOTSTRAP_ISO_STORAGE)
    if iso is None:
        raise PostError(f"{node} has no storage {BOOTSTRAP_ISO_STORAGE}, where CAPMOX uploads each machine's bootstrap ISO")
    content = [c for c in str(iso.get("content") or "").split(",") if c]
    if "iso" not in content:
        api.put(f"/storage/{BOOTSTRAP_ISO_STORAGE}", {"content": ",".join(content + ["iso"])})
        log(f"storage {BOOTSTRAP_ISO_STORAGE} now takes ISOs (CAPMOX's bootstrap data)")


def token_file(state: Path, name: str) -> Path:
    return Path(os.path.abspath(state)) / "proxmox" / f"{name}.token.age"


def token(api: PveApi, name: str, doc: dict, state: Path, sealer, log: Callable[[str], None]) -> None:
    from dciab_validate.proxmox_privs import grants
    px = doc["spec"]["platform"]["proxmox"]
    tokenid = px["credentials"]["tokenID"]
    user, _, tname = tokenid.partition("!")
    if not tname or not user.endswith("@pve"):
        raise PostError(f"tokenID {tokenid} must be <user>@pve!<token>: DCiaB creates the user in the Proxmox VE realm")
    want = grants(px)

    roles = {r.get("roleid"): r for r in (api.get("/access/roles") or []) if isinstance(r, dict)}
    for g in want:
        privs = ",".join(g.privileges)
        have = roles.get(g.role)
        if have is None:
            api.post("/access/roles", {"roleid": g.role, "privs": privs})
            log(f"role {g.role} created")
        elif sorted(p for p in str(have.get("privs") or "").split(",") if p) != sorted(g.privileges):
            api.put(f"/access/roles/{quote(g.role)}", {"privs": privs})
            log(f"role {g.role} corrected to exactly {privs}")

    if not api.exists(f"/access/users/{quote(user)}"):
        api.post("/access/users", {"userid": user, "comment": "DCiaB: CAPMOX"})
        log(f"user {user} created")

    sealed = token_file(state, name)
    tokens = [t.get("tokenid") for t in (api.get(f"/access/users/{quote(user)}/token") or []) if isinstance(t, dict)]
    if tname in tokens:
        if not sealed.is_file():
            raise PostError(f"token {tokenid} already exists on {name}, but its secret isn't sealed in {sealed}. Its secret "
                            "can't be read back: delete the token (Datacenter > Permissions > API Tokens) and run "
                            "again, or put its secret where tokenSecret points yourself")
        log(f"token {tokenid} present; secret sealed in {sealed}")
    else:
        data = api.post(f"/access/users/{quote(user)}/token/{quote(tname)}",
                        {"privsep": 1, "comment": "DCiaB: CAPMOX"})
        secret = (data or {}).get("value") if isinstance(data, dict) else None
        if not secret:
            raise PostError(f"creating token {tokenid} returned no secret")
        sealed.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(sealed.parent, 0o700)
        sealer(doc["spec"]["bootstrap"]["recipients"], secret, sealed)
        del secret
        log(f"token {tokenid} created (privilege-separated); its secret is sealed in {sealed}")

    for g in want:
        api.put("/access/acl", {"path": g.path, "roles": g.role, "users": user, "tokens": tokenid,
                                "propagate": 1 if g.propagate else 0})
    log(f"ACLs set for {user} and {tokenid}: " + ", ".join(f"{g.path} {g.role}" for g in want))


def root_password(answers: Path, name: str, site: Optional[str], identity: Optional[Path],
                  run=None) -> str:
    """The host's root password: the site's, or the generated one opened with the age identity."""
    import subprocess
    if site:
        return site
    sealed = answers / f"{name}.root-password.age"
    if not sealed.is_file():
        raise PostError(f"no rootPassword in the config and no {sealed}: the host's root password is unknown")
    if identity is None:
        raise PostError(f"the root password is generated: pass --age-identity FILE (a key for one of "
                        f"spec.bootstrap.recipients) to open {sealed}")
    run = run or (lambda cmd: subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30))
    try:
        return run(["age", "-d", "-i", str(identity), str(sealed)]).stdout.rstrip("\n")
    except FileNotFoundError:
        raise PostError("age is needed to open the root password (the payload's bin/ has it)") from None
    except subprocess.CalledProcessError:
        raise PostError(f"age couldn't open {sealed} with {identity}") from None


def run(api: PveApi, host: dict, doc: dict, state: Path, password: str, sealer,
        log: Callable[[str], None]) -> List[str]:
    """Log in and run network, storage and token for one host. Returns what was done."""
    done: List[str] = []

    def say(line: str) -> None:
        done.append(line)
        log(line)

    refuse_tagged(doc)       # before the root password is sent
    api.login(password)
    del password
    node = host["name"]
    network(api, node, doc, say)
    storage(api, node, doc, say)
    token(api, node, doc, state, sealer, say)
    return done
