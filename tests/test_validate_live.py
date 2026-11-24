"""dciab validate --live, against local fake servers. Nothing here touches a real network."""

import base64
import copy
import hashlib
import json
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ROOT, errors, load_example, warnings
from dciab_validate import live, probes
from dciab_validate.probes import ProbeError

TOKEN = "not-a-real-token"      # what conftest's Site puts in DCIAB_PVE_TOKEN
FILE_SECRET = "not-a-real-secret"  # what conftest's Site writes into every secret file


# --- a self-signed certificate ------------------------------------------------------------

@pytest.fixture(scope="session")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make a test certificate")
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "key.pem", d / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(crt),
         "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
         "-addext", "extendedKeyUsage=serverAuth"],
        check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    sha256 = hashlib.sha256(der).hexdigest().upper()
    sha1 = hashlib.sha1(der).hexdigest().upper()
    pair = lambda h: ":".join(h[i:i + 2] for i in range(0, len(h), 2))  # noqa: E731
    return {"key": key, "crt": crt, "sha256": pair(sha256), "sha1": pair(sha1)}


WRONG_PIN = ":".join(["AB"] * 32)


# --- a fake HTTPS server: routes map (method, path) to (status, json) ------------------------------

class FakeHTTPS:
    def __init__(self, cert, routes, auth=None):
        self.routes = routes
        self.auth = auth          # header value every path except the open ones must carry
        self.open_paths = {"/redfish/v1/", "/api/session"}
        self.seen = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                fake.seen.append((self.command, self.path, dict(self.headers)))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                key = (self.command, self.path)
                if key not in fake.routes:
                    return self._send(404, {"error": "not found"})
                if fake.auth and self.path not in fake.open_paths:
                    got = self.headers.get("Authorization") or self.headers.get("vmware-api-session-id")
                    if got not in fake.auth:
                        return self._send(401, {"error": "unauthorized"})
                status, body = fake.routes[key]
                if callable(body):
                    status, body = body(self)
                self._send(status, body)

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert["crt"]), str(cert["key"]))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers():
    made = []

    def make(cert, routes, auth=None):
        s = FakeHTTPS(cert, routes, auth)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


# --- Proxmox -----------------------------------------------------------------------------------

def pve_routes(template=1, storage="local-lvm", bridge="vmbr0", status3="online"):
    routes = {
        ("GET", "/api2/json/version"): (200, {"data": {"version": "9.1.1"}}),
        ("GET", "/api2/json/nodes"): (200, {"data": [
            {"node": "pve01", "status": "online"}, {"node": "pve02", "status": "online"},
            {"node": "pve03", "status": status3}]}),
        ("GET", "/api2/json/nodes/pve01/qemu"): (200, {"data": [
            {"vmid": 9010, "template": template}, {"vmid": 100}]}),
    }
    for n in ("pve01", "pve02", "pve03"):
        routes[("GET", f"/api2/json/nodes/{n}/storage")] = (200, {"data": [{"storage": "local"}, {"storage": storage}]})
        routes[("GET", f"/api2/json/nodes/{n}/network")] = (200, {"data": [
            {"iface": "eno1", "type": "eth"}, {"iface": bridge, "type": "bridge"}]})
    add_permissions(routes, load_example("proxmox-bare")["spec"]["platform"]["proxmox"])
    return routes


def add_permissions(routes, px, drop=None, extra_root=None):
    """GET /access/permissions?path=... answers: exactly docs/PROXMOX.md's grants."""
    from urllib.parse import quote

    from dciab_validate.proxmox_privs import grants
    for g in grants(px):
        # As the API answers: each held privilege maps to the propagate flag (0 when the grant
        # does not propagate), so a 0 must still count as held.
        privs = {p: int(g.propagate) for p in g.privileges if p != drop}
        routes[("GET", f"/api2/json/access/permissions?path={quote(g.path, safe='')}")] = (200, {"data": {g.path: privs}})
    routes[("GET", "/api2/json/access/permissions?path=%2F")] = (200, {"data": {"/": extra_root or {}}})


PVE_AUTH = {f"PVEAPIToken=dciab@pve!capmox={TOKEN}"}


def proxmox_spec(port, pin):
    spec = copy.deepcopy(load_example("proxmox-bare")["spec"])
    # The live Proxmox checks run against a three-node cluster (pve_routes); the example
    # lists only pve01 because its hosts aren't clustered.
    spec["platform"]["proxmox"]["nodes"] = ["pve01", "pve02", "pve03"]
    spec["platform"]["proxmox"]["url"] = f"https://127.0.0.1:{port}"
    spec["platform"]["proxmox"]["tlsFingerprint"] = pin
    return spec


def test_proxmox_all_good(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN + "\n")
    srv = servers(cert, pve_routes(), PVE_AUTH)
    r = live.check_proxmox(proxmox_spec(srv.port, cert["sha256"]), tmp_path, 3)
    assert r.findings == []
    assert any("Proxmox VE 9.1.1" in n and "dciab@pve!capmox" in n for n in r.notes)


def test_proxmox_sha1_pin_works(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    srv = servers(cert, pve_routes(), PVE_AUTH)
    assert live.check_proxmox(proxmox_spec(srv.port, cert["sha1"]), tmp_path, 3).findings == []


def test_proxmox_wrong_pin_sends_no_token(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    srv = servers(cert, pve_routes(), PVE_AUTH)
    r = live.check_proxmox(proxmox_spec(srv.port, WRONG_PIN), tmp_path, 3)
    [f] = r.findings
    assert f.path == ("spec", "platform", "proxmox", "url")
    assert "not the pinned one" in f.found and cert["sha256"] in f.found
    assert srv.seen == []  # the token never left this host


def test_proxmox_bad_token(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", "wrong-token-SECRET")
    srv = servers(cert, pve_routes(), PVE_AUTH)
    r = live.check_proxmox(proxmox_spec(srv.port, cert["sha256"]), tmp_path, 3)
    [f] = r.findings
    assert f.path[-2:] == ("credentials", "tokenID") and "HTTP 401" in f.found
    assert "SECRET" not in f.found + f.fix and "env DCIAB_PVE_TOKEN" in f.fix


def test_proxmox_missing_things(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    srv = servers(cert, pve_routes(template=0, storage="ceph", bridge="vmbr1", status3="offline"), PVE_AUTH)
    spec = proxmox_spec(srv.port, cert["sha256"])
    spec["platform"]["proxmox"]["nodes"].append("pve09")
    r = live.check_proxmox(spec, tmp_path, 3)
    by = {f.path[-1]: f for f in r.findings}
    assert "not a template" in by["templateID"].found
    assert "local-lvm: no such storage" in by["storage"].found
    assert "vmbr0: no such bridge" in by["bridge"].found and "vmbr1" in by["bridge"].fix
    assert "not a node" in by[3].found
    assert [f for f in r.findings if f.severity == "warning" and "offline" in f.found]


def test_proxmox_unreadable_storage_is_an_error(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    routes = pve_routes()
    routes[("GET", "/api2/json/nodes/pve02/storage")] = (403, {"data": None})
    srv = servers(cert, routes, PVE_AUTH)
    [f] = live.check_proxmox(proxmox_spec(srv.port, cert["sha256"]), tmp_path, 3).findings
    assert "can't read /nodes/pve02/storage (HTTP 403)" in f.found


def test_proxmox_template_id_missing(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    srv = servers(cert, pve_routes(), PVE_AUTH)
    spec = proxmox_spec(srv.port, cert["sha256"])
    spec["platform"]["proxmox"]["templateID"] = 9999
    [f] = live.check_proxmox(spec, tmp_path, 3).findings
    assert "9999: no VM with this ID on pve01" in f.found


def test_proxmox_unreachable(cert, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    [f] = live.check_proxmox(proxmox_spec(port, cert["sha256"]), tmp_path, 1).findings
    assert "cannot connect" in f.found


# --- vSphere -----------------------------------------------------------------------------------

SESSION = "sess-123"


def vsphere_routes(template=True, datastore=True):
    q = "&datacenters=datacenter-1"
    return {
        ("POST", "/api/session"): (0, lambda h: (201, SESSION) if h.headers.get("Authorization") ==
                                   "Basic " + base64.b64encode(f"dciab@vsphere.local:{FILE_SECRET}".encode()).decode()
                                   else (401, {"error": "bad"})),
        ("DELETE", "/api/session"): (204, None),
        ("GET", "/api/vcenter/datacenter?names=DC1"): (200, [{"datacenter": "datacenter-1", "name": "DC1"}]),
        ("GET", "/api/vcenter/cluster?names=Cluster1" + q): (200, [{"cluster": "domain-c1"}]),
        ("GET", "/api/vcenter/resource-pool?names=dciab&clusters=domain-c1"): (200, [{"resource_pool": "rp-1"}]),
        ("GET", "/api/vcenter/datastore?names=vsanDatastore" + q): (200, [{"datastore": "ds-1"}] if datastore else []),
        ("GET", "/api/vcenter/network?names=dciab-nodes-110" + q): (200, [{"network": "net-1"}]),
        ("GET", "/api/vcenter/folder?names=dciab" + q + "&type=VIRTUAL_MACHINE"): (200, [{"folder": "group-v1"}]),
        ("GET", "/api/vcenter/vm?names=rocky10-kube-v1.36.6" + q): (200, [{"vm": "vm-1"}] if template else []),
    }


@pytest.fixture
def vsphere_spec(site, monkeypatch, cert):
    spec = copy.deepcopy(load_example("vsphere")["spec"])
    vs = spec["platform"]["vsphere"]
    vs.update({"server": "localhost", "thumbprint": cert["sha256"], "datacenter": "DC1", "cluster": "Cluster1",
               "datastore": "vsanDatastore", "folder": "/DC1/vm/dciab", "resourcePool": "/DC1/host/Cluster1/Resources/dciab",
               "network": "dciab-nodes-110", "template": "rocky10-kube-v1.36.6"})
    vs["credentials"] = {"username": "dciab@vsphere.local", "password": {"file": "./secrets/vsphere.pw"}}
    return spec


def test_vsphere_all_good(cert, servers, site, vsphere_spec, monkeypatch):
    srv = servers(cert, vsphere_routes(), {SESSION})
    monkeypatch.setattr(live, "VSPHERE_PORT", srv.port)
    r = live.check_vsphere(vsphere_spec, site.dir, 3)
    assert r.findings == [], r.findings
    assert ("DELETE", "/api/session") in [(m, p) for m, p, _ in srv.seen]


def test_vsphere_missing_datastore_and_template(cert, servers, site, vsphere_spec, monkeypatch):
    srv = servers(cert, vsphere_routes(template=False, datastore=False), {SESSION})
    monkeypatch.setattr(live, "VSPHERE_PORT", srv.port)
    r = live.check_vsphere(vsphere_spec, site.dir, 3)
    assert [f.path[-1] for f in r.findings if f.severity == "error"] == ["datastore"]
    assert [f.path[-1] for f in r.findings if f.severity == "warning"] == ["template"]


def test_vsphere_bad_password(cert, servers, site, vsphere_spec, monkeypatch):
    (site.dir / "secrets" / "vsphere.pw").write_text("wrong-SECRET\n")
    srv = servers(cert, vsphere_routes(), {SESSION})
    monkeypatch.setattr(live, "VSPHERE_PORT", srv.port)
    [f] = live.check_vsphere(vsphere_spec, site.dir, 3).findings
    assert "rejected dciab@vsphere.local" in f.found and "SECRET" not in f.found + f.fix


def test_vsphere_unknown_datacenter(cert, servers, site, vsphere_spec, monkeypatch):
    vsphere_spec["platform"]["vsphere"]["datacenter"] = "DC9"
    routes = vsphere_routes()
    routes[("GET", "/api/vcenter/datacenter?names=DC9")] = (200, [])
    srv = servers(cert, routes, {SESSION})
    monkeypatch.setattr(live, "VSPHERE_PORT", srv.port)
    [f] = live.check_vsphere(vsphere_spec, site.dir, 3).findings
    assert f.path[-1] == "datacenter"


# --- Redfish ------------------------------------------------------------------------------------

BMC_AUTH = {"Basic " + base64.b64encode(f"dciab:{FILE_SECRET}".encode()).decode()}
SYS = "/redfish/v1/Systems/System.Embedded.1"


def redfish_routes(media="system", types=("CD", "USBStick")):
    routes = {
        ("GET", "/redfish/v1/"): (200, {"RedfishVersion": "1.17.0"}),
        ("GET", "/redfish/v1/Systems"): (200, {"Members": [{"@odata.id": SYS}]}),
        ("GET", "/redfish/v1/Managers/iDRAC.Embedded.1"): (200, {"VirtualMedia": {"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1/VirtualMedia"}}),
        ("GET", "/redfish/v1/Managers/iDRAC.Embedded.1/VirtualMedia"): (200, {"Members": [{"@odata.id": "/vm/1"}]}),
        ("GET", SYS + "/VirtualMedia"): (200, {"Members": [{"@odata.id": "/vm/1"}]}),
        ("GET", "/vm/1"): (200, {"MediaTypes": list(types)}),
    }
    system = {"Links": {"ManagedBy": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"}]}}
    if media == "system":
        system["VirtualMedia"] = {"@odata.id": SYS + "/VirtualMedia"}
    elif media == "none":
        system["Links"] = {}
    routes[("GET", SYS)] = (200, system)
    return routes


def bmc_host(port, cert, trust="pin"):
    h = {"name": "pve01", "bmc": {"address": f"redfish-virtualmedia+https://127.0.0.1:{port}{SYS}",
                                  "credentials": {"username": "dciab", "password": {"file": "./secrets/bmc/pve01"}}}}
    if trust == "pin":
        h["bmc"]["tlsFingerprint"] = cert["sha256"]
    else:
        h["bmc"]["caBundle"] = {"file": str(cert["crt"])}
    return h


P = ("spec", "hypervisors", "hosts", 0, "bmc")


@pytest.mark.parametrize("media", ["system", "manager"])
def test_redfish_all_good(cert, servers, site, media):
    srv = servers(cert, redfish_routes(media), BMC_AUTH)
    r = live.check_redfish(P, bmc_host(srv.port, cert), site.dir, 3)
    assert r.findings == [] and any("virtual media /vm/1" in n for n in r.notes)


def test_redfish_root_that_needs_credentials(cert, servers, site):
    srv = servers(cert, redfish_routes(), BMC_AUTH)
    srv.open_paths = set()
    assert live.check_redfish(P, bmc_host(srv.port, cert), site.dir, 3).findings == []


def test_redfish_ca_bundle(cert, servers, site):
    srv = servers(cert, redfish_routes(), BMC_AUTH)
    assert live.check_redfish(P, bmc_host(srv.port, cert, trust="ca"), site.dir, 3).findings == []


def test_redfish_untrusted_certificate(cert, servers, site, tmp_path):
    other = tmp_path / "other-ca.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=other",
                    "-keyout", str(tmp_path / "k"), "-out", str(other)], check=True, capture_output=True)
    srv = servers(cert, redfish_routes(), BMC_AUTH)
    h = bmc_host(srv.port, cert, trust="ca")
    h["bmc"]["caBundle"]["file"] = str(other)
    [f] = live.check_redfish(P, h, site.dir, 3).findings
    assert "not trusted" in f.found and srv.seen == []


def test_redfish_bad_password(cert, servers, site):
    (site.dir / "secrets" / "bmc" / "pve01").write_text("wrong-SECRET\n")
    srv = servers(cert, redfish_routes(), BMC_AUTH)
    [f] = live.check_redfish(P, bmc_host(srv.port, cert), site.dir, 3).findings
    assert f.path == P + ("credentials", "username") and "SECRET" not in f.found + f.fix


def test_redfish_wrong_system(cert, servers, site):
    srv = servers(cert, redfish_routes(), BMC_AUTH)
    h = bmc_host(srv.port, cert)
    h["bmc"]["address"] = h["bmc"]["address"].replace("System.Embedded.1", "System.9")
    [f] = live.check_redfish(P, h, site.dir, 3).findings
    assert "no system /redfish/v1/Systems/System.9" in f.found and SYS in f.fix


def test_redfish_no_virtual_media(cert, servers, site):
    srv = servers(cert, redfish_routes("none"), BMC_AUTH)
    [f] = live.check_redfish(P, bmc_host(srv.port, cert), site.dir, 3).findings
    assert "no virtual media" in f.found


def test_redfish_virtual_media_without_cd(cert, servers, site):
    srv = servers(cert, redfish_routes(types=("USBStick",)), BMC_AUTH)
    [f] = live.check_redfish(P, bmc_host(srv.port, cert), site.dir, 3).findings
    assert "no CD or DVD" in f.found


# --- NTP and DNS fakes ------------------------------------------------------------------------------

class FakeUDP:
    def __init__(self, reply):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            out = self.reply(data)
            if out is not None:
                self.sock.sendto(out, addr)

    def close(self):
        self.sock.close()


@pytest.fixture
def udp():
    made = []

    def make(reply):
        s = FakeUDP(reply)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def ntp_reply(skew=0.0, stratum=2, mode=4, leap=0):
    def reply(data):
        now = time.time() + skew
        ts = probes._to_ntp(now)
        return bytes([(leap << 6) | 0x20 | mode, stratum, 6, 0xEC]) + b"\0" * 8 + b"LOCL" + ts + data[40:48] + ts + ts
    return reply


def test_ntp_offset_reported(udp, monkeypatch):
    srv = udp(ntp_reply(skew=0.0))
    monkeypatch.setattr(probes, "NTP_PORT", srv.port)
    r = live.check_ntp(("spec", "network", "ntpServers", 0), "127.0.0.1", 2)
    assert r.findings == [] and any("stratum 2" in n and "offset" in n for n in r.notes)


def test_ntp_big_offset_is_a_warning(udp, monkeypatch):
    srv = udp(ntp_reply(skew=30.0))
    monkeypatch.setattr(probes, "NTP_PORT", srv.port)
    [f] = live.check_ntp(("spec", "network", "ntpServers", 0), "127.0.0.1", 2).findings
    assert f.severity == "warning" and "+30." in f.found


@pytest.mark.parametrize("kwargs,text", [({"stratum": 0}, "kiss-o'-death"), ({"stratum": 16}, "not synchronized"),
                                         ({"mode": 3}, "mode 3"), ({"leap": 3}, "leap indicator 3")])
def test_ntp_bad_answers(udp, monkeypatch, kwargs, text):
    srv = udp(ntp_reply(**kwargs))
    monkeypatch.setattr(probes, "NTP_PORT", srv.port)
    with pytest.raises(ProbeError, match=text):
        probes.sntp_offset("127.0.0.1", 2)


def test_ntp_unreachable_is_an_error(udp, monkeypatch):
    srv = udp(lambda data: None)
    monkeypatch.setattr(probes, "NTP_PORT", srv.port)
    [f] = live.check_ntp(("spec", "network", "ntpServers", 0), "127.0.0.1", 0.3).findings
    assert f.severity == "error" and "no NTP answer" in f.found


def test_ntp_targets_follow_who_serves_time():
    bare = load_example("proxmox-bare")["spec"]
    assert live.ntp_targets(bare) == []  # isolated: upstream is empty, the servers are DCiaB's own
    bare["time"]["upstream"] = ["ntp.example.org"]
    assert live.ntp_targets(bare) == [(("spec", "time", "upstream", 0), "ntp.example.org")]
    plain = load_example("vsphere")["spec"]
    assert [p[1] for p in live.ntp_targets(plain)] == plain["time"]["servers"]


def dns_reply(rcode=0, answers=(1,), ra=True):
    """Answers with one record per entry in `answers` (its type: 1 = A, 5 = CNAME)."""
    def reply(data):
        qid, question = data[:2], data[12:]
        flags = 0x8100 | (0x80 if ra else 0) | rcode
        records = b""
        for rtype in answers:
            rdata = b"\x0a\x00\x00\x01" if rtype == 1 else b"\x02vc\xc0\x0c"
            records += b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 300, len(rdata)) + rdata
        return qid + struct.pack("!HHHHH", flags, 1, len(answers), 0, 0) + question + records
    return reply


def test_dns_query_packet():
    q = probes.build_query("vc.example.net", probes.TYPE_A, 0x1234)
    assert q[:12] == bytes.fromhex("123401000001000000000000")
    assert q[12:] == b"\x02vc\x07example\x03net\x00\x00\x01\x00\x01"


def test_dns_answers(udp, monkeypatch):
    srv = udp(dns_reply())
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    r = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", ["vc.example.net"], 2)
    assert r.findings == [] and any("vc.example.net" in n for n in r.notes)


def test_dns_nxdomain_is_a_warning(udp, monkeypatch):
    srv = udp(dns_reply(rcode=3, answers=()))
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    [f] = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", ["vc.example.net"], 2).findings
    assert f.severity == "warning" and "NXDOMAIN" in f.found


def test_dns_cname_only_is_a_warning(udp, monkeypatch):
    srv = udp(dns_reply(answers=(5,)))
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    [f] = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", ["vc.example.net"], 2).findings
    assert f.severity == "warning" and "no address for vc.example.net" in f.found


def test_dns_cname_then_a_counts(udp, monkeypatch):
    srv = udp(dns_reply(answers=(5, 1)))
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    assert probes.dns_query("127.0.0.1", "vc.example.net", probes.TYPE_A, 2) == ("NOERROR", 1)


def test_dns_without_recursion_is_an_error(udp, monkeypatch):
    srv = udp(dns_reply(ra=False))
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    [f] = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", ["vc.example.net"], 2).findings
    assert f.severity == "error" and "without recursion" in f.found


def test_dns_refused_is_an_error(udp, monkeypatch):
    srv = udp(dns_reply(rcode=5, answers=()))
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    [f] = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", [], 2).findings
    assert f.severity == "error" and "refused" in f.found


def test_dns_silent_is_an_error(udp, monkeypatch):
    srv = udp(lambda data: None)
    monkeypatch.setattr(probes, "DNS_PORT", srv.port)
    [f] = live.check_dns(("spec", "network", "upstreamResolvers", 0), "127.0.0.1", [], 0.3).findings
    assert "no DNS answer" in f.found


def test_dns_names_skip_the_site_domain_and_addresses():
    spec = load_example("vsphere")["spec"]
    names = live.dns_names(spec)
    assert spec["platform"]["vsphere"]["server"] in names or spec["platform"]["vsphere"]["server"].endswith(
        "." + spec["network"]["domain"])
    assert all(not n.endswith("." + spec["network"]["domain"]) for n in names)
    assert not any(n[0].isdigit() and n.replace(".", "").isdigit() for n in names)


# --- unused-address probes --------------------------------------------------------------------------

def test_address_probes_skipped_without_the_interface(monkeypatch):
    monkeypatch.setattr(probes, "find_interface", lambda want: None)
    r = live.check_addresses(load_example("proxmox")["spec"], 1)
    assert r.findings == [] and any("skipped" in n for n in r.notes)


def test_find_interface_by_name_and_mac(tmp_path):
    (tmp_path / "eno1").mkdir()
    (tmp_path / "eno1" / "address").write_text("B8:CA:3A:00:00:05\n")
    assert probes.find_interface("eno1", str(tmp_path)) == "eno1"
    assert probes.find_interface("b8:ca:3a:00:00:05", str(tmp_path)) == "eno1"
    assert probes.find_interface("eno2", str(tmp_path)) is None
    assert probes.find_interface("b8:ca:3a:00:00:06", str(tmp_path)) is None


@pytest.fixture
def fake_cmds(monkeypatch):
    """Fake arping/ping/ip: addresses in `used` answer, everything else stays quiet."""
    state = {"used": set(), "gateway_up": True, "calls": [], "tools": {"arping", "ping", "ip"}, "local": "10.30.110.5",
             "arping_version": "arping from iputils 20240905"}

    def which(name):
        return f"/usr/bin/{name}" if name in state["tools"] else None

    def run_cmd(argv, timeout):
        state["calls"].append(list(argv))
        if argv[0] == "ip":
            return 0, f"2: eno1    inet {state['local']}/24 brd 10.30.110.255 scope global eno1\n"
        if argv[:2] == ["arping", "-V"]:
            return 0, state["arping_version"]
        addr = argv[-1]
        if addr == "10.30.110.1" and state["gateway_up"]:
            return (1 if argv[0] == "arping" else 0), ""
        if argv[0] == "arping":
            return (1 if addr in state["used"] else 0), ""
        return (0 if addr in state["used"] else 1), ""

    monkeypatch.setattr(probes.shutil, "which", which)
    monkeypatch.setattr(probes, "run_cmd", run_cmd)
    monkeypatch.setattr(probes, "find_interface", lambda want: "eno1" if ":" in want else state.get("sub"))
    monkeypatch.setattr(probes, "interface_up", lambda iface: True)
    return state


def test_arping_finds_a_conflict(fake_cmds):
    spec = load_example("proxmox-bare")["spec"]
    fake_cmds["used"] = {"10.30.110.25", "10.30.110.11"}
    r = live.check_addresses(spec, 1)
    paths = sorted(".".join(map(str, f.path)) for f in r.findings)
    assert paths == ["spec.clusters.infra.apiVIP", "spec.clusters.mgmt.nodeIPRange"]
    assert all("answers ARP on eno1" in f.found for f in r.findings)
    assert any("arping -D" in n for n in r.notes)
    probed = [c[-1] for c in fake_cmds["calls"] if c[0] == "arping" and c[1] == "-D"]
    assert "10.30.110.5" not in probed  # the bootstrap address is already on this host
    assert "10.30.110.53" in probed and "10.30.110.20" in probed and "10.30.110.49" in probed
    assert all("-I" in c and "eno1" in c for c in fake_cmds["calls"] if c[0] == "arping" and c[1] == "-D")


def test_probes_use_the_vlan_interface_when_tagged(fake_cmds):
    fake_cmds["sub"] = "eno1.110"
    spec = load_example("proxmox-bare")["spec"]
    spec["bootstrap"]["managementVLAN"] = "tagged"
    r = live.check_addresses(spec, 1)
    assert r.findings == [] and any("on eno1.110" in n for n in r.notes)


def test_silent_gateway_skips_the_probes(fake_cmds):
    fake_cmds["gateway_up"] = False
    r = live.check_addresses(load_example("proxmox-bare")["spec"], 1)
    [f] = r.findings
    assert f.severity == "warning" and "gateway 10.30.110.1 doesn't answer ARP" in f.found
    assert len([c for c in fake_cmds["calls"] if c[0] == "arping" and c[1] == "-D"]) == 1


def test_unknown_local_addresses_skip_the_bootstrap_address(fake_cmds):
    fake_cmds["tools"] = {"arping", "ping"}
    r = live.check_addresses(load_example("proxmox-bare")["spec"], 1)
    probed = [c[-1] for c in fake_cmds["calls"] if c[0] == "arping" and c[1] == "-D"]
    assert "10.30.110.5" not in probed and any("bootstrap address was not probed" in n for n in r.notes)


def test_large_ranges_are_capped(monkeypatch):
    monkeypatch.setattr(live, "MAX_PROBES_PER_RANGE", 4)
    spec = load_example("proxmox-bare")["spec"]
    claimed, notes = live.claimed_addresses(spec)
    assert len([p for p, _ in claimed if p == ("spec", "clusters", "infra", "nodeIPRange")]) == 4
    assert any("only the first 4" in n for n in notes)


def test_ping_fallback_without_iputils_arping(fake_cmds):
    fake_cmds["arping_version"] = "ARPing 2.21, by Thomas Habets"
    fake_cmds["used"] = {"10.30.110.10"}
    r = live.check_addresses(load_example("proxmox-bare")["spec"], 1)
    [f] = r.findings
    assert f.path == ("spec", "clusters", "mgmt", "apiVIP") and "answers ping" in f.found
    assert any("ping (ICMP" in n for n in r.notes)


def test_no_probe_tool(fake_cmds):
    fake_cmds["tools"] = {"ip"}
    r = live.check_addresses(load_example("proxmox-bare")["spec"], 1)
    assert r.findings == [] and any("neither arping" in n for n in r.notes)


def test_arping_without_permission_is_counted(fake_cmds, monkeypatch):
    real = probes.run_cmd
    monkeypatch.setattr(probes, "run_cmd", lambda argv, timeout: (2, "permission")
                        if argv[:2] == ["arping", "-D"] and argv[-1] != "10.30.110.1" else real(argv, timeout))
    r = live.check_addresses(load_example("proxmox-bare")["spec"], 1)
    [f] = r.findings
    assert f.severity == "warning" and "failed to run" in f.found and "root" in f.fix


def test_claimed_addresses_skip_metal3_clusters():
    spec = load_example("metal3-cluster")["spec"]
    paths = {p for p, _ in live.claimed_addresses(spec)[0]}
    assert not any(p[0:2] == ("spec", "workloadClusters") and
                   spec["workloadClusters"][p[2]]["class"] == "metal3" for p in paths)


# --- the CLI ---------------------------------------------------------------------------------------

def test_cli_without_live_says_so(site):
    _, report = site.validate(load_example("proxmox"))
    assert any("were not run" in n and "--live" in n for n in report["notes"])


def _validate_live(site, doc, *extra):
    import yaml
    from dciab_validate import cli
    path = site.dir / "dciab.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    code = cli.main([str(path), "--root", str(Path(__file__).resolve().parent.parent), "--output", "json",
                     "--live", "--timeout", "3", *extra])
    out = site.capsys.readouterr().out
    return code, json.loads(out), out


def test_cli_live_end_to_end(cert, servers, site, monkeypatch):
    monkeypatch.setattr(probes, "find_interface", lambda want: None)
    pve = servers(cert, pve_routes(), PVE_AUTH)
    doc = copy.deepcopy(load_example("proxmox-bare"))
    doc["spec"]["platform"]["proxmox"].update(url=f"https://127.0.0.1:{pve.port}", tlsFingerprint=cert["sha256"])
    for h in doc["spec"]["hypervisors"]["hosts"]:
        bmc = servers(cert, redfish_routes(), BMC_AUTH)
        h["bmc"]["address"] = f"redfish-virtualmedia+https://127.0.0.1:{bmc.port}{SYS}"
        h["bmc"].pop("caBundle", None)
        h["bmc"]["tlsFingerprint"] = cert["sha256"]
    code, report, out = _validate_live(site, doc)
    assert code == 0, report
    # Only the example's own acknowledgements (tests/test_validate.py EXPECTED_WARNINGS).
    assert errors(report) == [] and {w["path"] for w in warnings(report)} == {
        "spec.failureDomains.notHighlyAvailable", "spec.time.serveLocalClock",
        "spec.clusters.mgmt", "spec.clusters.infra"}
    notes = report["notes"]
    assert not any("were not run" in n for n in notes)
    assert sum("virtual media" in n for n in notes) == 3
    assert any("unused-address probes skipped" in n for n in notes)
    assert TOKEN not in out and FILE_SECRET not in out


def test_cli_live_reports_problems_with_lines(cert, servers, site, monkeypatch):
    monkeypatch.setattr(probes, "find_interface", lambda want: None)
    pve = servers(cert, pve_routes(bridge="vmbr9"), PVE_AUTH)
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["platform"]["proxmox"].update(url=f"https://127.0.0.1:{pve.port}", tlsFingerprint=cert["sha256"])
    doc["spec"]["network"]["upstreamResolvers"] = []
    monkeypatch.setattr(live, "ntp_targets", lambda spec: [])
    code, report, _ = _validate_live(site, doc)
    assert code == 1
    [f] = errors(report, "spec.platform.proxmox.bridge")
    assert f["line"] is not None


def test_cli_live_waits_for_offline_errors(site, monkeypatch):
    called = []
    monkeypatch.setattr(live, "run", lambda *a: called.append(a) or ([], []))
    doc = load_example("proxmox")
    doc["spec"]["bundleVersion"] = "9.9.9"
    code, report, _ = _validate_live(site, doc)
    assert code == 1 and called == []
    assert any("live checks run once the errors above are fixed" in n for n in report["notes"])


def test_cli_live_survives_a_crashing_check(site, monkeypatch):
    def boom(*a):
        raise RuntimeError("secret-ish details")
    monkeypatch.setattr(live, "check_proxmox", boom)
    monkeypatch.setattr(live, "check_redfish", lambda *a: live.Results())
    monkeypatch.setattr(live, "check_addresses", lambda *a: live.Results())
    monkeypatch.setattr(live, "ntp_targets", lambda spec: [])
    doc = load_example("proxmox")
    doc["spec"]["network"]["upstreamResolvers"] = []
    code, report, out = _validate_live(site, doc)
    assert code == 1 and "secret-ish" not in out
    assert any("Proxmox API check could not finish (RuntimeError)" in n for n in report["notes"])


def test_cli_rejects_a_bad_timeout(site):
    with pytest.raises(SystemExit):
        _validate_live(site, load_example("proxmox"), "--timeout", "0")


def test_server_values_are_cleaned_before_printing():
    assert live.clean("9.1\x1b[2J\n") == "9.1?[2J?"
    assert len(live.clean("x" * 500)) == 120


def test_proxmox_token_privileges(cert, servers, monkeypatch, tmp_path):
    monkeypatch.setenv("DCIAB_PVE_TOKEN", TOKEN)
    px_example = load_example("proxmox-bare")["spec"]["platform"]["proxmox"]
    routes = pve_routes()
    add_permissions(routes, px_example, drop="VM.Clone", extra_root={"Permissions.Modify": 1})
    srv = servers(cert, routes, PVE_AUTH)
    spec = proxmox_spec(srv.port, cert["sha256"])
    r = live.check_proxmox(spec, tmp_path, 5)
    found = " | ".join(f.found for f in r.findings)
    assert "the token lacks /vms: VM.Clone" in found
    assert "can do more than DCiaB needs: /: Permissions.Modify" in found


def test_pveum_commands_match_the_grants():
    from dciab_validate import proxmox_privs
    px = {"storage": "ceph-vm", "bridge": "vmbr0", "vlanTag": 110}
    cmds = proxmox_privs.pveum_commands(px, "capmox@pve", "dciab")
    assert "pveum user token add capmox@pve dciab --privsep 1" in cmds
    assert any(c.startswith("pveum acl modify /sdn/zones/localnetwork/vmbr0/110 ") for c in cmds)
    assert any("/storage/local " in c and "DCiaBBootstrapISO" in c for c in cmds)
    one = {"storage": "local", "bridge": "vmbr1"}
    assert [g.path for g in proxmox_privs.grants(one)] == ["/nodes", "/vms", "/storage/local",
                                                            "/sdn/zones/localnetwork/vmbr1"]


def test_proxmox_doc_matches_the_grants():
    from dciab_validate.proxmox_privs import grants
    doc = (ROOT / "docs" / "PROXMOX.md").read_text()
    px = {"storage": "<storage>", "bridge": "<bridge>", "vlanTag": "<vlanTag>"}
    for g in grants(px):
        row = f"| `{g.role}` | `{g.path}` | {'yes' if g.propagate else 'no'} | " + \
              ", ".join(f"`{p}`" for p in g.privileges)
        assert row in doc, f"docs/PROXMOX.md is out of date for {g.role}"


def test_compare_counts_non_propagated_privileges_as_held():
    # The API reports a held privilege as 0 when its ACL does not propagate (issue #110).
    from dciab_validate.proxmox_privs import Grant, compare
    want = [Grant("R", "/storage/x", False, ("Datastore.Audit",), "")]
    have = {"/storage/x": {"Datastore.Audit": 0, "Sys.Modify": 0}}
    assert compare(want, have) == ([], ["/storage/x: Sys.Modify"])
    assert compare(want, {"/storage/x": {}})[0] == ["/storage/x: Datastore.Audit"]
