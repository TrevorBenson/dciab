"""Post-install, through the Proxmox VE API: the bridge, the storage and the CAPMOX API token.

Runs as the `post` step for each host DCiaB installed (its install record says `installed`)
that is one of spec.platform.proxmox.nodes. It logs in as root@pam with the root password: the
site's (spec.hypervisors.rootPassword) or the generated one, opened from
<state>/answers/<host>.root-password.age with --age-identity. Nothing uses SSH.

  trust    the API's certificate: spec.platform.proxmox.tlsFingerprint when the platform URL
           names this host and the config gives one, else the fingerprint seen on first
           contact, recorded in the install record (api_fingerprint) and pinned from then on
           (validate and the later steps trust the API by it: dciab_validate.pending)
  network  spec.platform.proxmox.bridge must exist (the installer makes vmbr0 on the
           management NIC). With platform.proxmox.vlanTag it is made VLAN-aware, keeping its
           address, and the network is reloaded. With managementVLAN: tagged the first-boot
           hook (firstboot.py) already made it VLAN-aware and put the address on
           <bridge>.<network.nodes.vlan>; the step checks that and changes nothing, so the
           address never moves
  storage  spec.platform.proxmox.storage must exist on the node, enabled, holding VM disks
           (images); `local` must take ISOs (CAPMOX's bootstrap data), and gains `iso` when not
  token    the roles, user, token and ACLs of lib/dciab_validate/proxmox_privs.py (the one
           source of truth for docs/PROXMOX.md and validate --live). The token is
           privilege-separated; its secret, shown once, is sealed at once to
           spec.bootstrap.recipients in <state>/proxmox/<host>.token.age. For the host the
           platform URL names, it is also written where tokenSecret points (mode 600; on the
           appliance, also in the appliance-local secrets that survive a resync), never to
           removable media (write_token), after the ACLs and never fatally. Roles whose
           privileges differ are corrected; an existing token is kept only if its sealed secret
           is here, and with --age-identity its file is written again from the sealed copy when
           missing or stale (TokenHome; the post step does that on later runs too)
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from dciab_validate.probes import HttpsClient, ProbeError, Trust, fingerprint_matches, format_fingerprint


class PostError(Exception):
    """A post-install step that can't go on. The message is safe to print."""


def host_cidr(doc: dict, node: str) -> str:
    """The host's management address with the node network's prefix."""
    import ipaddress
    nodes = doc["spec"]["network"]["nodes"]
    host = next(h for h in doc["spec"]["hypervisors"]["hosts"] if h["name"] == node)
    return f"{host['address']}/{ipaddress.ip_network(nodes['cidr'], strict=False).prefixlen}"


def check_tagged(ifaces: list, bridge: dict, node: str, doc: dict, log: Callable[[str], None]) -> None:
    """managementVLAN: tagged: the first-boot hook made the bridge VLAN-aware and put the address
    on <bridge>.<vlan>. Checked only: changing either now could cut the host off."""
    px = doc["spec"]["platform"]["proxmox"]
    vlan = int(doc["spec"]["network"]["nodes"]["vlan"])
    name = f"{px['bridge']}.{vlan}"
    want = host_cidr(doc, node)
    sub = next((i for i in ifaces if isinstance(i, dict) and i.get("iface") == name), None)
    hint = ("the first-boot hook didn't apply; on the host's console (through its BMC) see "
            "`journalctl -t dciab-first-boot` and /etc/network/interfaces.dciab-installer")
    if str(bridge.get("bridge_vlan_aware") or "0") != "1":
        raise PostError(f"bridge {px['bridge']} on {node} isn't VLAN-aware with managementVLAN: tagged: {hint}")
    if sub is None or str(sub.get("cidr") or "") != want:
        got = (sub or {}).get("cidr") or "no address"
        raise PostError(f"{name} on {node} doesn't hold {want} ({got}) with managementVLAN: tagged: {hint}")
    log(f"bridge {px['bridge']} is VLAN-aware and {name} holds {want} (managementVLAN: tagged)")


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
    certificate is self-signed. tlsFingerprint may be SHA-256 or SHA-1, as the schema allows.

    The host platform.proxmox.url names is checked against tlsFingerprint when the config gives
    one. Otherwise, as for every other host, the certificate seen on first contact is pinned in
    the install record, and validate and the later steps trust the API by that pin
    (dciab_validate.pending)."""
    from dciab_validate.pending import names_host
    px = doc["spec"]["platform"]["proxmox"]
    observed = record.get("api_fingerprint")
    if names_host(px["url"], host, doc["spec"]["network"]["domain"]) and px.get("tlsFingerprint"):
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
                     "every later run, validate and the later steps check it")


class PveApi:
    """The Proxmox VE API of one host, logged in with a ticket."""

    def __init__(self, address: str, fingerprint: str, timeout: float = 30.0, port: int = 8006, url: Optional[str] = None):
        self.client = HttpsClient(url or f"https://{address}:{port}", Trust(fingerprint=fingerprint), timeout)
        self.headers: Dict[str, str] = {}

    @classmethod
    def from_url(cls, url: str, fingerprint: str, timeout: float = 30.0) -> "PveApi":
        return cls("", fingerprint, timeout, url=url.rstrip("/"))

    def use_token(self, tokenid: str, secret: str) -> None:
        """Authenticate every request with an API token instead of a ticket."""
        self.headers = {"Authorization": f"PVEAPIToken={tokenid}={secret}"}

    def delete(self, path: str) -> object:
        return self._send("DELETE", path)

    def upload(self, path: str, fields: Dict[str, str], filename: str, fileobj, size: int,
               timeout: float = 3600.0) -> object:
        """POST a multipart form with `fields` first, then the file (Proxmox VE reads the fields
        before the file part), streamed with a Content-Length: never all in memory."""
        import uuid
        boundary = f"dciab-{uuid.uuid4().hex}"
        head = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="filename"; filename="{filename}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        def body():
            yield head
            while True:
                chunk = fileobj.read(1 << 20)
                if not chunk:
                    break
                yield chunk
            yield tail
        headers = dict(self.headers, **{"Content-Type": f"multipart/form-data; boundary={boundary}",
                                        "Content-Length": str(len(head) + size + len(tail))})
        old = self.client.timeout
        self.client.timeout = max(old, timeout)
        try:
            resp = self.client.request("POST", "/api2/json" + path, headers, body())
        except ProbeError as exc:
            raise PostError(str(exc)) from None
        finally:
            self.client.timeout = old
        if resp.status != 200:
            raise PostError(f"{self.client.where}: POST {path} (upload) answered HTTP {resp.status}")
        try:
            return resp.json().get("data") if resp.body else None
        except (ProbeError, AttributeError, ValueError):
            return None

    def wait_task(self, node: str, upid: object, timeout: float = 3600.0, interval: float = 2.0,
                  sleep: Callable[[float], None] = time.sleep, what: str = "the task") -> None:
        """Wait for a worker task (a UPID an API call returned) to stop; fail unless it ended OK.
        Anything but a UPID string means the call finished at once."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        deadline = time.monotonic() + timeout
        while True:
            st = self.get(f"/nodes/{quote(node)}/tasks/{quote(upid, safe='')}/status") or {}
            if isinstance(st, dict) and st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise PostError(f"{what} failed on {node}: {st.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise PostError(f"{what} on {node} didn't finish within {timeout:g}s ({upid})")
            sleep(interval)

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
    ifaces = api.get(f"/nodes/{quote(node)}/network") or []
    bridge = next((i for i in ifaces if isinstance(i, dict) and i.get("iface") == px["bridge"]), None)
    if bridge is None or bridge.get("type") != "bridge":
        raise PostError(f"{node} has no bridge {px['bridge']} (spec.platform.proxmox.bridge); the installer makes "
                        "vmbr0 on the management NIC, so name that, or create the bridge before running again")
    if (doc["spec"].get("hypervisors") or {}).get("managementVLAN") == "tagged":
        check_tagged(ifaces, bridge, node, doc, log)
        return
    if not px.get("vlanTag"):
        log(f"bridge {px['bridge']} present (no vlanTag, so it needn't be VLAN-aware)")
        return
    if str(bridge.get("bridge_vlan_aware") or "0") == "1":
        log(f"bridge {px['bridge']} is VLAN-aware")
        return
    from .firstboot import bridge_vids
    form = {"type": "bridge", "bridge_vlan_aware": 1, "bridge_vids": bridge_vids(doc)}
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


# File systems a USB stick or other removable media uses: the token is never written to them in
# plain text (the DCiaB stick's exFAT partition is readable on any laptop).
PORTABLE_FS = frozenset({"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "iso9660", "udf"})


def _unescape_mount(text: str) -> str:
    return text.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def fs_type(path: Path, mounts: Optional[str] = None) -> str:
    """The file system type `path` (or its nearest existing parent) is on, from /proc/self/mounts;
    "" when it can't be told."""
    if mounts is None:
        try:
            mounts = Path("/proc/self/mounts").read_text()
        except OSError:
            return ""
    p = Path(os.path.abspath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    real = os.path.realpath(p)
    best, kind = "", ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point = _unescape_mount(parts[1]).rstrip("/") or "/"
        if (real == point or real.startswith(point.rstrip("/") + "/") or point == "/") and len(point) >= len(best):
            best, kind = point, parts[2]
    return kind


def _secret_files(node, config_dir: Path, skip: tuple, path: tuple = ()) -> List[Tuple[tuple, Path]]:
    """Every {file: ...} secret reference in the config, but the one at `skip`, as real paths."""
    from dciab_validate.pending import secret_files
    return secret_files(node, config_dir, skip, path)


def token_targets(doc: dict, config: Path, site=None) -> Tuple[List[Path], Optional[str]]:
    """Where `post` writes the CAPMOX token's secret in plain text (mode 600), so the steps that
    read tokenSecret find it: ([files], None), or ([], why it can't). On the bootstrap appliance
    that is the file in the site copy and the same file in the appliance-local secrets
    (dciab_state.site_local), which every sync puts back over the stick's; elsewhere the file
    tokenSecret names. Never the config file, another secret's file, a file on the appliance
    outside the site's secrets/, or removable media (write_token). validate refuses the same
    files (dciab_validate.pending.token_home)."""
    from dciab_validate.pending import token_home
    targets, why, _ = token_home(doc, config, site)
    return targets, why


def write_token(secret: str, doc: dict, config: Path, log: Callable[[str], None], site=None,
                mounts: Optional[str] = None) -> List[Path]:
    """Write the token's secret where token_targets says, atomically, mode 600 in folders of mode
    700; a file that already holds it is left alone. Returns the target files; logs why when
    there are none."""
    import tempfile
    targets, why = token_targets(doc, config, site)
    for t in targets:
        kind = fs_type(t, mounts)
        if kind in PORTABLE_FS:
            targets, why = [], (f"{t} is on {kind}, removable media: the token is never written there in plain "
                                "text. Run dciab from the bootstrap host's own copy of the config")
            break
    if not targets:
        log(f"the token's secret wasn't written in plain text ({why}); open its sealed copy with age")
        return []
    for t in targets:
        missing = []
        p = t.parent
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):                   # every folder made here is root-only
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
        had = t.is_file() and t.stat().st_size > 0
        try:
            if had and t.read_text().strip() == secret:
                log(f"the token's secret is in {t} already")
                continue
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=t.parent, prefix=f".{t.name}.", suffix=".tmp")   # mode 600
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(secret + "\n")
            os.replace(tmp, t)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        log(f"the token's secret is in {t} (mode 600){', replacing what was there' if had else ''}")
    return targets


def token_missing(doc: dict, config: Path, site=None) -> List[Path]:
    """The files token_targets names that don't hold a secret yet (missing or empty)."""
    targets, _ = token_targets(doc, config, site)
    return [t for t in targets if not (t.is_file() and t.stat().st_size > 0)]


class TokenHome:
    """Where post writes the CAPMOX token's secret in plain text (write_token), for the host
    platform.proxmox.url names. reopen opens the sealed copy (with --age-identity) so a rerun
    can write a file that's missing or stale; None when there's no identity."""

    def __init__(self, doc: dict, config: Path, log: Callable[[str], None],
                 reopen: Optional[Callable[[Path], str]] = None, site=None):
        self.doc, self.config, self.log, self.reopen, self.site = doc, config, log, reopen, site

    def write(self, secret: str, sealed: Path) -> None:
        """Never fatal: the token exists and is sealed, so a write that fails only costs a copy
        by hand (or a rerun with --age-identity), and the ACLs must still be set."""
        try:
            write_token(secret, self.doc, self.config, self.log, self.site)
        except OSError as exc:
            self.log(f"couldn't write the token's secret where tokenSecret points ({exc}); it is sealed in "
                     f"{sealed}: run dciab hypervisors again with --age-identity to have post write it, or open "
                     "it with age (docs/TROUBLESHOOTING.md)")

    def refresh(self, sealed: Path) -> None:
        """A kept token: write its secret from the sealed copy when a file is missing or stale."""
        if self.reopen is None:
            missing = token_missing(self.doc, self.config, self.site)
            if missing:
                self.log(f"the token's secret isn't in {', '.join(map(str, missing))}; pass --age-identity to have "
                         f"post write it from {sealed}, or open it with age (docs/TROUBLESHOOTING.md)")
            return
        try:
            secret = self.reopen(sealed)
        except PostError as exc:
            self.log(f"couldn't open the token's sealed copy to check where tokenSecret points ({exc})")
            return
        if secret:
            self.write(secret, sealed)
        del secret


def open_sealed(identity: Path, sealed: Path, run=None) -> str:
    """The plain text of a file sealed with age, opened with the identity."""
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30))
    try:
        return run(["age", "-d", "-i", str(identity), str(sealed)]).stdout.rstrip("\n")
    except FileNotFoundError:
        raise PostError(f"age is needed to open {sealed} (the payload's bin/ has it)") from None
    except subprocess.CalledProcessError:
        raise PostError(f"age couldn't open {sealed} with {identity}") from None


def token(api: PveApi, name: str, doc: dict, state: Path, sealer, log: Callable[[str], None],
          home: Optional[TokenHome] = None) -> None:
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
        secret = None
    else:
        data = api.post(f"/access/users/{quote(user)}/token/{quote(tname)}",
                        {"privsep": 1, "comment": "DCiaB: CAPMOX"})
        secret = (data or {}).get("value") if isinstance(data, dict) else None
        if not secret:
            raise PostError(f"creating token {tokenid} returned no secret")
        sealed.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(sealed.parent, 0o700)
        sealer(doc["spec"]["bootstrap"]["recipients"], secret, sealed)
        log(f"token {tokenid} created (privilege-separated); its secret is sealed in {sealed}")

    for g in want:
        api.put("/access/acl", {"path": g.path, "roles": g.role, "users": user, "tokens": tokenid,
                                "propagate": 1 if g.propagate else 0})
    log(f"ACLs set for {user} and {tokenid}: " + ", ".join(f"{g.path} {g.role}" for g in want))

    # After the ACLs, so a file that can't be written never leaves the token without them. A
    # kept token's file is written from the sealed copy when missing or stale (a rerun).
    if home is not None:
        if secret:
            home.write(secret, sealed)
        else:
            home.refresh(sealed)
    del secret


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
        log: Callable[[str], None], config: Optional[Path] = None,
        identity: Optional[Path] = None) -> List[str]:
    """Log in and run network, storage and token for one host. Returns what was done.

    For the host platform.proxmox.url names, the token's secret is also written where
    tokenSecret points (write_token), given the config's path: a new token's at once; a kept
    token's from its sealed copy when the file is missing or stale and an age identity is given."""
    from dciab_validate.pending import names_host
    done: List[str] = []

    def say(line: str) -> None:
        done.append(line)
        log(line)

    api.login(password)
    del password
    node = host["name"]
    network(api, node, doc, say)
    storage(api, node, doc, say)
    home = None
    if config is not None and names_host(doc["spec"]["platform"]["proxmox"]["url"], host, doc["spec"]["network"]["domain"]):
        reopen = (lambda sealed: open_sealed(identity, sealed)) if identity is not None else None
        home = TokenHome(doc, config, say, reopen)
    token(api, node, doc, state, sealer, say, home)
    return done
