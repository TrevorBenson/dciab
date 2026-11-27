"""Live checks (`dciab validate --live`): ask the site's real services whether the config fits them.

Run only on a config that passed the offline checks. Each group of checks runs in its own
thread with a per-request timeout, and reports through the same findings as the offline
checks. Secrets are read from their references, sent only in request headers, and never
printed.
"""

from __future__ import annotations

import base64
import ipaddress
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path as FsPath
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import dciab_backup as bk
from dciab_storage import tier_block

from . import netutil as nu
from . import probes
from .findings import ERROR, WARNING, Finding, Path
from .probes import HttpsClient, ProbeError, Trust

NTP_OFFSET_WARN = 1.0  # seconds; certificates and etcd care about clock skew
MAX_WORKERS = 16
ARP_WORKERS = 32
VSPHERE_PORT = 443
MAX_PROBES_PER_RANGE = 256


def clean(value) -> str:
    """A server-supplied value made safe to print: no control characters, bounded length."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text[:120]


class Results:
    def __init__(self) -> None:
        self.findings: List[Finding] = []
        self.notes: List[str] = []

    def error(self, path: Path, found: str, fix: str) -> None:
        self.findings.append(Finding(ERROR, path, found, fix))

    def warn(self, path: Path, found: str, fix: str) -> None:
        self.findings.append(Finding(WARNING, path, found, fix))

    def note(self, text: str) -> None:
        self.notes.append("live: " + text)


def read_secret(ref: dict, config_dir: FsPath) -> str:
    """The value behind {file: ...} or {env: ...}. The offline checks already proved it exists."""
    if "env" in ref:
        return os.environ.get(ref["env"], "").strip()
    raw = ref["file"]
    target = FsPath(raw) if os.path.isabs(raw) else config_dir / raw
    return target.read_text(encoding="utf-8").strip()


def _secret_path(ref: dict) -> str:
    return f"file {ref['file']}" if "file" in ref else f"env {ref['env']}"


# -- Proxmox --------------------------------------------------------------------------

def check_proxmox(spec: dict, config_dir: FsPath, timeout: float, fingerprint: Optional[str] = None) -> Results:
    """The CAPMOX token and what CAPMOX needs. fingerprint: the API certificate's pin
    (default tlsFingerprint; on a bare site, the install record's: pending.proxmox_api)."""
    r = Results()
    px = spec["platform"]["proxmox"]
    base: Path = ("spec", "platform", "proxmox")
    creds = px["credentials"]
    secret = read_secret(creds["tokenSecret"], config_dir)
    client = HttpsClient(px["url"], Trust(fingerprint=fingerprint or px["tlsFingerprint"]), timeout)
    auth = {"Authorization": f"PVEAPIToken={creds['tokenID']}={secret}"}

    def get(path: str):
        resp = client.request("GET", "/api2/json" + path, auth)
        return resp.status, (resp.json().get("data") if resp.status == 200 else None)

    try:
        status, version = get("/version")
    except ProbeError as exc:
        r.error(base + ("url",), str(exc), "check the URL, the network path from this host, and tlsFingerprint")
        return r
    if status in (401, 403):
        r.error(base + ("credentials", "tokenID"), f"the API rejected token {creds['tokenID']} (HTTP {status})",
                f"check the token ID and the secret in {_secret_path(creds['tokenSecret'])}")
        return r
    if status != 200:
        r.error(base + ("url",), f"GET /api2/json/version answered HTTP {status}", "check that the URL is the Proxmox API")
        return r
    r.note(f"Proxmox VE {clean(version.get('version', '?')) if isinstance(version, dict) else '?'} at {client.where} "
           f"accepted token {creds['tokenID']}")

    try:
        status, nodes = get("/nodes")
        if status != 200:
            r.error(base + ("nodes",), f"GET /nodes answered HTTP {status}",
                    "give the token Sys.Audit on / so DCiaB can see the nodes")
            return r
        online = {n["node"]: n.get("status", "unknown") for n in nodes or []}
        for i, node in enumerate(px["nodes"]):
            if node not in online:
                r.error(base + ("nodes", i), f"{node}: not a node of this Proxmox cluster",
                        "use node names from the cluster: " + ", ".join(clean(n) for n in sorted(online)))
            elif online[node] != "online":
                r.warn(base + ("nodes", i), f"{node} is {online[node]}", "bring the node online before the seed step")
        tnode = px["templateNode"]
        if online.get(tnode) == "online":
            status, vms = get(f"/nodes/{quote(tnode)}/qemu")
            match = [v for v in vms or [] if int(v.get("vmid", -1)) == px["templateID"]]
            if status != 200:
                r.error(base + ("templateID",), f"GET /nodes/{tnode}/qemu answered HTTP {status}",
                        "give the token VM.Audit so DCiaB can see the template")
            elif not match:
                r.error(base + ("templateID",), f"{px['templateID']}: no VM with this ID on {tnode}",
                        "publish the node template there (dciab templates), or correct templateID and templateNode")
            elif not match[0].get("template"):
                r.error(base + ("templateID",), f"{px['templateID']} on {tnode} is a VM, not a template",
                        "convert it to a template (qm template) or use the template's ID")
        no_storage, no_bridge, bridges_seen, unreadable = [], [], set(), []
        for node in px["nodes"]:
            if online.get(node) != "online":
                continue
            status, stores = get(f"/nodes/{quote(node)}/storage")
            if status != 200:
                unreadable.append(f"/nodes/{node}/storage (HTTP {status})")
            elif px["storage"] not in {s.get("storage") for s in stores or []}:
                no_storage.append(node)
            status, nets = get(f"/nodes/{quote(node)}/network")
            bridges = {n.get("iface") for n in nets or [] if "bridge" in str(n.get("type", "")) and n.get("iface")}
            bridges_seen |= bridges
            if status != 200:
                unreadable.append(f"/nodes/{node}/network (HTTP {status})")
            elif px["bridge"] not in bridges:
                no_bridge.append(node)
        if unreadable:
            r.error(base + ("credentials", "tokenID"), "the token can't read " + ", ".join(unreadable),
                    "give the token Sys.Audit and Datastore.Audit so DCiaB can check storage and bridges")
        if no_storage:
            r.error(base + ("storage",), f"{px['storage']}: no such storage on " + ", ".join(no_storage),
                    "use a storage every node in spec.platform.proxmox.nodes has")
        if no_bridge:
            r.error(base + ("bridge",), f"{px['bridge']}: no such bridge on " + ", ".join(no_bridge),
                    "use a bridge every node has; bridges seen: " + (", ".join(clean(b) for b in sorted(bridges_seen)) or "(none)"))
        check_token_privileges(px, get, r, base)
    except ProbeError as exc:
        r.error(base + ("url",), str(exc), "check the network path to the Proxmox API")
    return r


def check_token_privileges(px: dict, get, r: Results, base: Path, want=None, token_id: Optional[str] = None,
                           what: str = "docs/PROXMOX.md") -> None:
    """The token has exactly what docs/PROXMOX.md lists: nothing missing, nothing that could
    change permissions or the hosts. want and token_id default to the CAPMOX token's."""
    from .proxmox_privs import compare, grants
    want = grants(px) if want is None else want
    token_id = token_id or px["credentials"]["tokenID"]
    user = token_id.split("!", 1)[0]
    if user == "root@pam":
        r.warn(base + ("credentials", "tokenID"), "the token belongs to root@pam",
               f"use a dedicated user with the privileges in {what} (pveum commands there)")
    have = {}
    for path in [g.path for g in want] + ["/"]:
        status, data = get(f"/access/permissions?path={quote(path, safe='')}")
        if status != 200 or not isinstance(data, dict):
            r.error(base + ("credentials", "tokenID"), f"can't read the token's privileges on {path} (HTTP {status})",
                    "the token must be able to read its own permissions (GET /access/permissions)")
            return
        have[path] = data.get(path) or {}
    missing, broad = compare(want, {k: v for k, v in have.items() if k != "/"}, root=have.get("/"))
    for line in missing:
        r.error(base + ("credentials", "tokenID"), f"the token lacks {line}",
                f"grant exactly the roles and ACLs in {what}, to both the user and the token (privsep=1)")
    for line in broad:
        r.warn(base + ("credentials", "tokenID"), f"the token can do more than DCiaB needs: {line}",
               f"remove those privileges; {what} lists exactly what is needed")
    if not missing:
        r.note(f"token {token_id} has every privilege {what} lists")


def check_template_import(spec: dict, config_dir: FsPath, timeout: float, fingerprint: Optional[str] = None) -> Results:
    """The token dciab templates logs in with (templateImport): accepted, and exactly the
    privileges docs/PROXMOX.md lists for the node template."""
    from .proxmox_privs import template_grants
    r = Results()
    px = spec["platform"]["proxmox"]
    base: Path = ("spec", "platform", "proxmox", "templateImport")
    creds = px["templateImport"]["credentials"]
    secret = read_secret(creds["tokenSecret"], config_dir)
    client = HttpsClient(px["url"], Trust(fingerprint=fingerprint or px["tlsFingerprint"]), timeout)
    auth = {"Authorization": f"PVEAPIToken={creds['tokenID']}={secret}"}

    def get(path: str):
        resp = client.request("GET", "/api2/json" + path, auth)
        return resp.status, (resp.json().get("data") if resp.status == 200 else None)

    try:
        status, _ = get("/version")
        if status in (401, 403):
            r.error(base + ("credentials", "tokenID"), f"the API rejected token {creds['tokenID']} (HTTP {status})",
                    f"check the token ID and the secret in {_secret_path(creds['tokenSecret'])}")
            return r
        check_token_privileges(px, get, r, base, want=template_grants(px), token_id=creds["tokenID"],
                               what="docs/PROXMOX.md (node template)")
    except ProbeError as exc:
        r.error(("spec", "platform", "proxmox", "url"), str(exc), "check the network path to the Proxmox API")
    return r


def proxmox_csi_blocks(spec: dict) -> List[Tuple[Path, dict]]:
    """(path, proxmoxCSI settings) for each place the config gives the Proxmox CSI plugin's."""
    block = spec["storage"].get("block")
    if not block:
        return []
    out = []
    if "proxmoxCSI" in block:
        out.append((("spec", "storage", "block", "proxmoxCSI"), block["proxmoxCSI"]))
    for tier, over in (block.get("overrides") or {}).items():
        if "proxmoxCSI" in over:
            out.append((("spec", "storage", "block", "overrides", tier, "proxmoxCSI"), over["proxmoxCSI"]))
    return out


def check_proxmox_csi(spec: dict, base: Path, csi: dict, config_dir: FsPath, timeout: float,
                      fingerprint: Optional[str] = None) -> Results:
    """The Proxmox CSI plugin's own token: accepted, its storage on every node and shared as the
    config says, and exactly the privileges docs/PROXMOX.md lists for it."""
    from .proxmox_privs import csi_grants
    r = Results()
    px = spec["platform"]["proxmox"]
    creds = csi["credentials"]
    secret = read_secret(creds["tokenSecret"], config_dir)
    client = HttpsClient(px["url"], Trust(fingerprint=fingerprint or px["tlsFingerprint"]), timeout)
    auth = {"Authorization": f"PVEAPIToken={creds['tokenID']}={secret}"}

    def get(path: str):
        resp = client.request("GET", "/api2/json" + path, auth)
        return resp.status, (resp.json().get("data") if resp.status == 200 else None)

    what = "docs/PROXMOX.md (Proxmox CSI plugin)"
    try:
        status, _ = get("/version")
        if status in (401, 403):
            r.error(base + ("credentials", "tokenID"), f"the API rejected token {creds['tokenID']} (HTTP {status})",
                    f"check the token ID and the secret in {_secret_path(creds['tokenSecret'])}")
            return r
        missing_on, unreadable = [], []
        for node in px["nodes"]:
            status, stores = get(f"/nodes/{quote(node)}/storage")
            if status != 200:
                unreadable.append(node)
            elif csi["storage"] not in {s.get("storage") for s in stores or []}:
                missing_on.append(node)
        if unreadable:
            r.warn(base + ("storage",), "the token can't list the storage on " + ", ".join(unreadable),
                   f"grant Datastore.Audit as {what} lists, so DCiaB can check the storage")
        if missing_on:
            r.error(base + ("storage",), f"{csi['storage']}: no such storage on " + ", ".join(missing_on),
                    "use a storage the nodes that run Kubernetes VMs have")
        # Whether the storage is shared decides the zone rule: the config says, Proxmox knows.
        status, conf = get(f"/storage/{quote(csi['storage'])}") if not missing_on else (None, None)
        if status is None:
            pass   # no such storage: reported above
        elif status == 200 and conf is not None:
            shared = bool(int(conf.get("shared") or 0))
            if shared != csi["shared"]:
                r.error(base + ("shared",), f"{str(csi['shared']).lower()}, but Proxmox has {csi['storage']} as "
                        + ("shared" if shared else "not shared (node-local)"),
                        f"set shared: {str(shared).lower()}, or fix the storage's shared flag in Proxmox")
        else:
            r.warn(base + ("shared",), f"the token can't read storage {csi['storage']} (HTTP {status})",
                   f"grant Datastore.Audit as {what} lists, so DCiaB can check shared")
        check_token_privileges(px, get, r, base, csi_grants(csi), creds["tokenID"], what)
    except ProbeError as exc:
        r.error(("spec", "platform", "proxmox", "url"), str(exc), "check the network path to the Proxmox API")
    return r


# -- vSphere ------------------------------------------------------------------------------

def check_vsphere(spec: dict, config_dir: FsPath, timeout: float) -> Results:
    r = Results()
    vs = spec["platform"]["vsphere"]
    base: Path = ("spec", "platform", "vsphere")
    creds = vs["credentials"]
    password = read_secret(creds["password"], config_dir)
    client = HttpsClient(f"https://{vs['server']}:{VSPHERE_PORT}", Trust(fingerprint=vs["thumbprint"]), timeout)
    basic = base64.b64encode(f"{creds['username']}:{password}".encode("utf-8")).decode("ascii")
    try:
        resp = client.request("POST", "/api/session", {"Authorization": f"Basic {basic}"})
    except ProbeError as exc:
        r.error(base + ("server",), str(exc), "check the server name, the network path from this host, and thumbprint")
        return r
    if resp.status in (401, 403):
        r.error(base + ("credentials", "username"), f"vCenter rejected {creds['username']} (HTTP {resp.status})",
                f"check the user name and the password in {_secret_path(creds['password'])}")
        return r
    if resp.status not in (200, 201):
        r.error(base + ("server",), f"POST /api/session answered HTTP {resp.status}",
                "check that the server is vCenter 7.0U2 or newer (the /api REST endpoints)")
        return r
    try:
        session = resp.json()
    except ProbeError as exc:
        r.error(base + ("server",), str(exc), "check that the server is vCenter")
        return r
    hdr = {"vmware-api-session-id": str(session)}
    r.note(f"vCenter {client.where} accepted {creds['username']}")

    def find(kind: str, name: str, **filters: str) -> Optional[list]:
        query = "names=" + quote(name, safe="") + "".join(f"&{k}={quote(v, safe='')}" for k, v in filters.items())
        resp = client.request("GET", f"/api/vcenter/{kind}?{query}", hdr)
        if resp.status != 200:
            r.error(base, f"GET /api/vcenter/{kind} answered HTTP {resp.status}",
                    f"give {creds['username']} read access to the inventory")
            return None
        return resp.json()

    try:
        dcs = find("datacenter", vs["datacenter"])
        if dcs is None:
            return r
        if not dcs:
            r.error(base + ("datacenter",), f"{vs['datacenter']}: no such datacenter", "use a datacenter name from vCenter")
            return r
        dc = dcs[0]["datacenter"]
        leaf = lambda p: p.rstrip("/").rsplit("/", 1)[-1]  # noqa: E731 - the REST filters match names only
        clusters = find("cluster", vs["cluster"], datacenters=dc)
        if clusters is not None and not clusters:
            r.error(base + ("cluster",), f"{vs['cluster']}: no cluster named {vs['cluster']} in {vs['datacenter']}",
                    "use the name of an existing cluster")
        wanted = [
            ("datastore", "datastore", vs["datastore"], {"datacenters": dc}),
            ("network", "network", vs["network"], {"datacenters": dc}),
            ("folder", "folder", leaf(vs["folder"]), {"datacenters": dc, "type": "VIRTUAL_MACHINE"}),
        ]
        if clusters:
            wanted.append(("resourcePool", "resource-pool", leaf(vs["resourcePool"]),
                           {"clusters": str(clusters[0].get("cluster", ""))}))
        for field, kind, name, extra in wanted:
            found = find(kind, name, **extra)
            if found is not None and not found:
                where = vs["cluster"] if field == "resourcePool" else vs["datacenter"]
                r.error(base + (field,), f"{vs[field]}: no {kind.replace('-', ' ')} named {name} in {where}",
                        f"create it, or use the name of an existing {kind.replace('-', ' ')}")
        found = find("vm", vs["template"], datacenters=dc)
        if found is not None and not found:
            r.warn(base + ("template",), f"{vs['template']}: not found in {vs['datacenter']}",
                   "build or import the node template; some vCenter versions leave templates out of this "
                   "list, so check in the vSphere client if you know it exists")
    except ProbeError as exc:
        r.error(base + ("server",), str(exc), "check the network path to vCenter")
    finally:
        try:
            client.request("DELETE", "/api/session", hdr)
        except ProbeError:
            pass
    return r


# -- Redfish ---------------------------------------------------------------------------------

def _redfish_trust(bmc: dict, config_dir: FsPath) -> Trust:
    if "tlsFingerprint" in bmc:
        return Trust(fingerprint=bmc["tlsFingerprint"])
    raw = bmc["caBundle"]["file"] if "file" in bmc["caBundle"] else None
    if raw is None:
        # {env: VAR}: the variable holds the PEM itself; write nothing to disk, load it from memory.
        return Trust(cadata=os.environ.get(bmc["caBundle"]["env"], ""))
    return Trust(cafile=str(FsPath(raw) if os.path.isabs(raw) else config_dir / raw))


def check_redfish(path: Path, host: dict, config_dir: FsPath, timeout: float) -> Results:
    r = Results()
    bmc = host["bmc"]
    url = bmc["address"].split("+", 1)[1]
    parts = urlsplit(url)
    system_path = parts.path.rstrip("/")
    creds = bmc["credentials"]
    password = read_secret(creds["password"], config_dir)
    basic = base64.b64encode(f"{creds['username']}:{password}".encode("utf-8")).decode("ascii")
    auth = {"Authorization": f"Basic {basic}"}
    client = HttpsClient(f"https://{parts.netloc}", _redfish_trust(bmc, config_dir), timeout)
    name = host["name"]
    try:
        root = client.request("GET", "/redfish/v1/", auth)  # some BMCs want credentials even here
        if root.status not in (200, 401, 403):
            r.error(path + ("address",), f"{name}: GET /redfish/v1/ answered HTTP {root.status}",
                    "check that the address is the BMC and Redfish is enabled")
            return r
        systems = client.request("GET", "/redfish/v1/Systems", auth)
        if systems.status in (401, 403):
            r.error(path + ("credentials", "username"),
                    f"{name}: the BMC rejected {creds['username']} (HTTP {systems.status})",
                    f"check the user name and the password in {_secret_path(creds['password'])}")
            return r
        if systems.status != 200:
            r.error(path + ("address",), f"{name}: GET /redfish/v1/Systems answered HTTP {systems.status}",
                    "check that Redfish is enabled on the BMC")
            return r
        members = [m.get("@odata.id", "").rstrip("/") for m in systems.json().get("Members", [])]
        if system_path not in members:
            r.error(path + ("address",), f"{name}: no system {system_path} on this BMC",
                    "use one of: " + (", ".join(clean(m) for m in members) or "(none listed)"))
            return r
        sys_resp = client.request("GET", system_path, auth)
        if sys_resp.status != 200:
            r.error(path + ("address",), f"{name}: GET {system_path} answered HTTP {sys_resp.status}",
                    "check the system path in the address")
            return r
        system = sys_resp.json()
        media = _virtual_media(client, auth, system)
        if media is None:
            r.error(path + ("address",), f"{name}: the BMC offers no virtual media",
                    "enable virtual media (it may need a BMC license); DCiaB boots installers through it")
        elif not media:
            r.error(path + ("address",), f"{name}: the BMC's virtual media takes no CD or DVD images",
                    "enable CD/DVD virtual media on the BMC")
        else:
            r.note(f"{name}: BMC {client.where} accepted {creds['username']}; virtual media {clean(media[0])}")
    except ProbeError as exc:
        r.error(path + ("address",), f"{name}: {exc}",
                "check the route from this host to the BMC network, the BMC address and its TLS trust")
    return r


def _virtual_media(client: HttpsClient, auth: dict, system: dict) -> Optional[List[str]]:
    """Virtual media slots that take CD/DVD images; None when the BMC has no virtual media."""
    link = (system.get("VirtualMedia") or {}).get("@odata.id")
    if not link:
        for mgr in (system.get("Links") or {}).get("ManagedBy", []):
            m = client.request("GET", mgr["@odata.id"], auth)
            if m.status == 200:
                link = (m.json().get("VirtualMedia") or {}).get("@odata.id")
                if link:
                    break
    if not link:
        return None
    coll = client.request("GET", link, auth)
    if coll.status != 200:
        return None
    slots = []
    for member in coll.json().get("Members", []):
        resp = client.request("GET", member["@odata.id"], auth)
        if resp.status != 200:
            continue
        types = resp.json().get("MediaTypes")
        if types is None or any(t in ("CD", "DVD") for t in types):
            slots.append(member["@odata.id"])
    return slots if coll.json().get("Members") else None


# -- NTP and DNS ---------------------------------------------------------------------------

def ntp_targets(spec: dict) -> List[Tuple[Path, str]]:
    """The NTP servers that must answer now. With time.servedBy dciab, the node-facing servers
    are the DNS service IPs, which DCiaB itself serves later, so only its upstream counts."""
    t = spec["time"]
    if t["servedBy"] == "dciab":
        return [(("spec", "time", "upstream", i), s) for i, s in enumerate(t["upstream"])]
    return [(("spec", "time", "servers", i), s) for i, s in enumerate(t["servers"])]


def check_ntp(path: Path, server: str, timeout: float) -> Results:
    r = Results()
    try:
        offset, stratum = probes.sntp_offset(server, timeout)
    except ProbeError as exc:
        r.error(path, str(exc), "check the server address and that UDP 123 is open from this host")
        return r
    if abs(offset) > NTP_OFFSET_WARN:
        r.warn(path, f"{server} is {offset:+.3f}s from this host's clock",
               "set this host's clock (chronyc makestep) or check the server; nodes take their time from it")
    r.note(f"NTP {server}: stratum {stratum}, offset {offset:+.3f}s")
    return r


def dns_names(spec: dict) -> List[str]:
    """Names the site's recursor will forward upstream: the config's hostnames outside network.domain."""
    domain = spec["network"]["domain"]
    names = []
    plat = spec["platform"]
    if plat["type"] == "vsphere":
        names.append(plat["vsphere"]["server"])
    else:
        names.append(urlsplit(plat["proxmox"]["url"]).hostname or "")
    # Backup targets: spec.backup's site and off-site copy, and any Longhorn backupTarget
    # (the old form, or an INFRA/workload tier's own). Each one is optional.
    targets = [(bk.site_target(spec) or {}).get("url"), (bk.offsite(spec) or {}).get("url")]
    for tier in ("mgmt", "infra", "workload"):
        block = tier_block(spec, tier)
        if block and block["provider"] == "longhorn":
            targets.append((block.get("settings") or {}).get("backupTarget"))
    names.extend(_target_host(t) for t in targets if t)
    names.extend(spec["time"].get("servers", []))
    names.extend(spec["time"].get("upstream", []))
    out = []
    for n in names:
        if not n or _is_ip(n) or n == domain or n.endswith("." + domain) or n in out:
            continue
        out.append(n)
    return out


def _target_host(url: str) -> str:
    """The host of s3://<bucket>@<endpoint>[:port][/path] or nfs://<server>:/<export>.
    Assumes the URL passed validate's S3/NFS patterns (live checks run only after them)."""
    scheme, rest = url.split("://", 1)
    if scheme == "nfs":
        return rest.split(":", 1)[0].split("/", 1)[0]       # the export path may hold "@"
    authority = rest.split("/", 1)[0]                        # so may an S3 path
    return authority.rsplit("@", 1)[-1].split(":", 1)[0]


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def check_dns(path: Path, server: str, names: List[str], timeout: float) -> Results:
    r = Results()
    queries = [(n, probes.TYPE_A) for n in names] or [(".", probes.TYPE_NS)]
    answered = []
    for name, qtype in queries:
        try:
            rcode, count = probes.dns_query(server, name, qtype, timeout)
        except ProbeError as exc:
            r.error(path, str(exc), "check the resolver address and that UDP 53 is open from this host")
            return r
        if rcode == "REFUSED":
            r.error(path, f"{server} refused a recursive query for {name}",
                    "allow recursion for this site's addresses on the resolver, or use another resolver")
            return r
        if rcode == "NXDOMAIN" or (rcode == "NOERROR" and qtype == probes.TYPE_A and count == 0):
            r.warn(path, f"{server} has no address for {name} ({rcode})",
                   "the site's DNS forwards to this resolver, so nodes won't resolve this name; check the name or resolver")
        elif rcode != "NOERROR":
            r.warn(path, f"{server} answered {rcode} for {name}", "check the resolver")
        else:
            answered.append(name)
    r.note(f"DNS {server} answered" + (f" for {', '.join(answered)}" if answered else ""))
    return r


# -- unused-address probes ------------------------------------------------------------------

def claimed_addresses(spec: dict) -> Tuple[List[Tuple[Path, str]], List[str]]:
    """Node-network addresses DCiaB will take that nothing may be using yet, and notes about ranges
    too large to probe whole (only their first MAX_PROBES_PER_RANGE addresses are probed)."""
    out: List[Tuple[Path, str]] = [(("spec", "bootstrap", "address"), spec["bootstrap"]["address"])]
    notes: List[str] = []
    for i, a in enumerate(spec["network"]["dnsServiceIPs"]):
        out.append((("spec", "network", "dnsServiceIPs", i), a))
    clusters = [(("spec", "clusters", n), spec["clusters"][n]) for n in ("mgmt", "infra")]
    clusters += [(("spec", "workloadClusters", i), c) for i, c in enumerate(spec.get("workloadClusters", []))]
    for cpath, c in clusters:
        if c.get("class") == "metal3":
            continue  # on a bare-metal network this host may not be on
        out.append((cpath + ("apiVIP",), c["apiVIP"]))
        lo, hi = nu.span_of_range(c["nodeIPRange"])
        if hi - lo + 1 > MAX_PROBES_PER_RANGE:
            notes.append(f"{c['nodeIPRange']}: only the first {MAX_PROBES_PER_RANGE} addresses were probed")
            hi = lo + MAX_PROBES_PER_RANGE - 1
        for v in range(lo, hi + 1):
            out.append((cpath + ("nodeIPRange",), str(ipaddress.IPv4Address(v))))
    return out, notes


def probe_interface(spec: dict) -> Optional[str]:
    """The interface on the node network: the bootstrap NIC, or its VLAN interface when tagged."""
    iface = probes.find_interface(spec["bootstrap"]["interface"])
    if iface and spec["bootstrap"].get("managementVLAN") == "tagged":
        sub = probes.find_interface(f"{iface}.{spec['network']['nodes']['vlan']}")
        if sub:
            return sub
    return iface


def check_addresses(spec: dict, timeout: float) -> Results:
    r = Results()
    want = spec["bootstrap"]["interface"]
    ipath: Path = ("spec", "bootstrap", "interface")
    iface = probe_interface(spec)
    if iface is None:
        r.note(f"unused-address probes skipped: the bootstrap interface {want} isn't on this host "
               "(run --live on the bootstrap host to probe the node network)")
        return r
    if not probes.interface_up(iface):
        r.warn(ipath, f"{iface} is down, so unused addresses were not probed",
               "connect the interface to the node network and rerun --live")
        return r
    method = probes.probe_method()
    if method is None:
        r.note("unused-address probes skipped: neither arping (iputils) nor ping is installed")
        return r
    word = "ARP" if method == probes.ARPING else "ping"
    gateway = spec["network"]["nodes"]["gateway"]
    # A known-present host first: if the gateway is silent, silence proves nothing about the rest.
    if probes.address_in_use(method, iface, gateway, timeout) is not True:
        r.warn(ipath, f"the node network gateway {gateway} doesn't answer {word} on {iface}, so unused "
               "addresses were not probed",
               "check that this interface is on the node network (and VLAN), and that the probe can run (root)")
        return r
    local_list = probes.local_ipv4_addresses()
    local = set(local_list or [])
    claimed, range_notes = claimed_addresses(spec)
    targets = [(p, a) for p, a in claimed if a not in local]
    if local_list is None:
        targets = [(p, a) for p, a in targets if p != ("spec", "bootstrap", "address")]
        r.note("couldn't list this host's addresses (`ip` failed), so the bootstrap address was not probed")
    with ThreadPoolExecutor(max_workers=ARP_WORKERS) as pool:
        results = list(pool.map(lambda t: probes.address_in_use(method, iface, t[1], timeout), targets))
    failed = 0
    for (path, addr), used in zip(targets, results):
        if used is None:
            failed += 1
        elif used:
            r.error(path, f"{addr} answers {word} on {iface}: it is in use",
                    "free the address, or choose addresses nothing on the node network uses")
    if failed:
        r.warn(ipath, f"{failed} of {len(targets)} address probes on {iface} failed to run",
               "run --live as root (arping needs CAP_NET_RAW) so every address is probed")
    how = ("arping -D (ARP duplicate address detection)" if method == probes.ARPING
           else "ping (ICMP; a host that drops ping looks unused, install iputils arping for a stronger check)")
    r.note(f"probed {len(targets)} node-network addresses on {iface} with {how}")
    for n in range_notes:
        r.note(n)
    return r


# -- entry ------------------------------------------------------------------------------------

def _secret_missing(ref: dict, config_dir: FsPath) -> bool:
    if "env" in ref:
        return not os.environ.get(ref["env"])
    raw = ref["file"]
    target = FsPath(raw) if os.path.isabs(raw) else config_dir / raw
    return not target.is_file() or target.stat().st_size == 0


def proxmox_pending(spec: dict, config_dir: FsPath, state) -> Tuple[Optional[str], Optional[str]]:
    """(fingerprint to trust the API by, None), or (None, why the Proxmox API checks are pending):
    the URL names a host spec.hypervisors installs, whose install record doesn't say post: done,
    and the token or the certificate's pin doesn't exist yet (#276)."""
    from .pending import held_hint, proxmox_api
    api = proxmox_api(spec, state)
    token_missing = _secret_missing(spec["platform"]["proxmox"]["credentials"]["tokenSecret"], config_dir)
    if api.pending and (token_missing or not api.fingerprint):
        what = " and ".join(w for w, missing in (("the CAPMOX token", token_missing),
                                                 ("the API certificate's pin", not api.fingerprint)) if missing)
        return None, (f"Proxmox API checks pending: {api.name} isn't set up by dciab hypervisors yet (its install "
                      f"record in {state}/installs/ doesn't say post: done), so {what} can't exist yet. Run "
                      f"--live again after dciab hypervisors{held_hint(api)}")
    return api.fingerprint, None


def tasks(spec: dict, config_dir: FsPath, timeout: float, state=None) -> List[Tuple[str, Callable[[], Results]]]:
    out: List[Tuple[str, Callable[[], Results]]] = []
    plat = spec["platform"]
    if plat["type"] == "proxmox":
        pin, why = proxmox_pending(spec, config_dir, state)
        if why:
            def note(why=why) -> Results:
                r = Results()
                r.note(why)
                return r
            out.append(("Proxmox API", note))
        elif not (pin or plat["proxmox"].get("tlsFingerprint")):
            from .pending import FINGERPRINT_PATH

            def unpinned() -> Results:
                r = Results()
                r.error(FINGERPRINT_PATH,
                        "not set, and no install record pins the Proxmox API's certificate, so the API can't be "
                        "trusted; the Proxmox API checks weren't run",
                        "set tlsFingerprint to the Proxmox API certificate's fingerprint (SHA-256), or run dciab "
                        "hypervisors again so its post pins it")
                return r
            out.append(("Proxmox API", unpinned))
        else:
            out.append(("Proxmox API", lambda: check_proxmox(spec, config_dir, timeout, pin)))
            for path, csi in proxmox_csi_blocks(spec):
                out.append((f"Proxmox CSI token {csi['credentials']['tokenID']}",
                            lambda p=path, c=csi: check_proxmox_csi(spec, p, c, config_dir, timeout, pin)))
            if plat["proxmox"].get("templateImport"):
                out.append(("node template token", lambda: check_template_import(spec, config_dir, timeout, pin)))
    else:
        out.append(("vSphere API", lambda: check_vsphere(spec, config_dir, timeout)))
    hosts: List[Tuple[Path, dict]] = []
    for i, h in enumerate(spec.get("hypervisors", {}).get("hosts", [])):
        hosts.append((("spec", "hypervisors", "hosts", i, "bmc"), h))
    if spec["baremetal"]["enabled"]:
        for i, h in enumerate(spec["baremetal"]["hosts"]):
            hosts.append((("spec", "baremetal", "hosts", i, "bmc"), h))
    for path, h in hosts:
        out.append((f"Redfish {h['name']}", lambda p=path, h=h: check_redfish(p, h, config_dir, timeout)))
    for path, server in ntp_targets(spec):
        out.append((f"NTP {server}", lambda p=path, s=server: check_ntp(p, s, timeout)))
    names = dns_names(spec)
    for i, server in enumerate(spec["network"]["upstreamResolvers"]):
        p = ("spec", "network", "upstreamResolvers", i)
        out.append((f"DNS {server}", lambda p=p, s=server: check_dns(p, s, names, timeout)))
    out.append(("address probes", lambda: check_addresses(spec, timeout)))
    return out


def run(doc: dict, config_path: FsPath, timeout: float, state=None) -> Tuple[List[Finding], List[str]]:
    """The live checks. state: the state folder (install records; default the step default)."""
    from .pending import default_state
    spec = doc["spec"]
    config_dir = config_path.resolve().parent
    jobs = tasks(spec, config_dir, timeout, state if state is not None else default_state(config_path))
    findings: List[Finding] = []
    notes: List[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in jobs]
        for name, fut in futures:
            try:
                res = fut.result()
            except Exception as exc:  # a bug or an unexpected answer must not hide the other checks
                notes.append(f"live: the {name} check could not finish ({type(exc).__name__}); rerun with a "
                             "working network path, or report it")
                findings.append(Finding(ERROR, ("spec",), f"the {name} live check failed unexpectedly",
                                        "see the note; rerun once the cause is fixed"))
                continue
            findings.extend(res.findings)
            notes.extend(res.notes)
    if not spec["network"]["upstreamResolvers"]:
        notes.append("live: no upstream resolvers to check (an isolated site)")
    return findings, notes
