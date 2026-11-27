"""Low-level network probes for the live checks, standard library only.

Every probe takes a timeout and raises ProbeError with a message that is safe to print:
no secret ever goes into a message, a log line or a command's argv.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import shutil
import socket
import ssl
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit


class ProbeError(Exception):
    pass


MAX_BODY = 4 << 20  # bytes; no API answer DCiaB reads is near this


# -- HTTPS --------------------------------------------------------------------

@dataclass(frozen=True)
class Trust:
    """How to trust a server certificate: a pinned fingerprint, a CA file, or the system CAs."""

    fingerprint: Optional[str] = None
    cafile: Optional[str] = None
    cadata: Optional[str] = None

    def context(self) -> ssl.SSLContext:
        if self.fingerprint:
            # The pin replaces chain and name checks; it is compared before any request is sent.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context(cafile=self.cafile, cadata=self.cadata or None)


def fingerprint_matches(der: bytes, want: str) -> bool:
    hexpin = want.replace(":", "").lower()
    algo = hashlib.sha1 if len(hexpin) == 40 else hashlib.sha256
    return algo(der).hexdigest() == hexpin


def format_fingerprint(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


@dataclass
class Response:
    status: int
    body: bytes
    headers: Dict[str, str]
    reason: str = ""        # the status line's text: Proxmox VE puts its error message there

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProbeError(f"HTTP {self.status} with a body that is not JSON") from None


class HttpsClient:
    """One HTTPS server. Each request opens a connection, checks the certificate pin, then sends."""

    def __init__(self, base_url: str, trust: Trust, timeout: float):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ProbeError(f"{base_url} is not an https:// URL")
        self.host = parts.hostname
        self.port = parts.port or 443
        self.trust = trust
        self.timeout = timeout

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}"

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None) -> Response:
        try:
            ctx = self.trust.context()
        except (OSError, ssl.SSLError, ValueError):
            raise ProbeError(f"{self.where}: cannot load the CA bundle given to trust this server") from None
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        try:
            try:
                conn.connect()
            except ssl.SSLCertVerificationError as exc:
                raise ProbeError(f"{self.where}: the TLS certificate is not trusted ({exc.verify_message})") from None
            except ssl.SSLError as exc:
                raise ProbeError(f"{self.where}: TLS failed ({exc.reason or exc})") from None
            except socket.timeout:
                raise ProbeError(f"{self.where}: no answer within {self.timeout:g}s") from None
            except OSError as exc:
                raise ProbeError(f"{self.where}: cannot connect ({exc.strerror or exc})") from None
            if self.trust.fingerprint:
                der = conn.sock.getpeercert(binary_form=True) or b""
                if not fingerprint_matches(der, self.trust.fingerprint):
                    raise ProbeError(f"{self.where}: the certificate fingerprint is {format_fingerprint(der)}, "
                                     "not the pinned one")
            all_headers = {"Accept": "application/json", "User-Agent": "dciab-validate"}
            all_headers.update(headers or {})
            if body is not None:
                all_headers.setdefault("Content-Type", "application/json")
            try:
                conn.request(method, path, body=body, headers=all_headers)
                resp = conn.getresponse()
                data = resp.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise ProbeError(f"{self.where}: the answer is larger than {MAX_BODY // (1 << 20)} MiB")
            except socket.timeout:
                raise ProbeError(f"{self.where}: no answer within {self.timeout:g}s") from None
            except (OSError, http.client.HTTPException) as exc:
                raise ProbeError(f"{self.where}: the request failed ({type(exc).__name__})") from None
            return Response(resp.status, data, {k.lower(): v for k, v in resp.getheaders()}, resp.reason or "")
        finally:
            conn.close()


# -- NTP -------------------------------------------------------------------------

NTP_EPOCH = 2208988800  # seconds from 1900-01-01 to 1970-01-01
NTP_PORT = 123


def _to_ntp(t: float) -> bytes:
    sec = int(t) + NTP_EPOCH
    frac = int((t - int(t)) * (1 << 32))
    return struct.pack("!II", sec & 0xFFFFFFFF, frac & 0xFFFFFFFF)


def _from_ntp(data: bytes) -> float:
    sec, frac = struct.unpack("!II", data)
    return sec - NTP_EPOCH + frac / (1 << 32)


def sntp_offset(server: str, timeout: float) -> Tuple[float, int]:
    """Ask one NTP server for the time. Returns (offset of the server from this host's clock, stratum)."""
    addr = _resolve(server, NTP_PORT, socket.SOCK_DGRAM)
    with socket.socket(addr[0], socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        packet = b"\x1b" + 39 * b"\0" + _to_ntp(t1)  # LI 0, version 3, mode 3 (client)
        try:
            s.connect(addr[4])  # the kernel then drops datagrams from any other address
            s.send(packet)
            data = s.recv(512)
        except socket.timeout:
            raise ProbeError(f"{server}: no NTP answer within {timeout:g}s") from None
        except OSError as exc:
            raise ProbeError(f"{server}: {exc.strerror or exc}") from None
        t4 = time.time()
    if len(data) < 48:
        raise ProbeError(f"{server}: a short NTP answer ({len(data)} bytes)")
    leap, mode, stratum = data[0] >> 6, data[0] & 0x7, data[1]
    if mode != 4:
        raise ProbeError(f"{server}: not an NTP server answer (mode {mode})")
    if data[24:32] != packet[40:48]:
        raise ProbeError(f"{server}: the NTP answer doesn't match the query")
    if stratum == 0:
        raise ProbeError(f"{server}: the server refused to serve time (kiss-o'-death {data[12:16]!r})")
    if stratum >= 16 or leap == 3:
        raise ProbeError(f"{server}: the server is not synchronized (stratum {stratum}, leap indicator {leap})")
    t2, t3 = _from_ntp(data[32:40]), _from_ntp(data[40:48])
    return ((t2 - t1) + (t3 - t4)) / 2, stratum


# -- DNS ----------------------------------------------------------------------------

RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
TYPE_A, TYPE_NS = 1, 2
DNS_PORT = 53


def build_query(name: str, qtype: int, qid: int) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    labels = [p for p in name.rstrip(".").split(".") if p]
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in labels) + b"\0"
    return header + qname + struct.pack("!HH", qtype, 1)


def dns_query(server: str, name: str, qtype: int, timeout: float) -> Tuple[str, int]:
    """Send one recursive query over UDP. Returns (rcode name, number of answers of the asked type)."""
    qid = random.randint(0, 0xFFFF)
    query = build_query(name, qtype, qid)
    addr = _resolve(server, DNS_PORT, socket.SOCK_DGRAM)
    with socket.socket(addr[0], socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(addr[4])
            s.send(query)
            deadline = time.monotonic() + timeout
            while True:
                data = s.recv(4096)
                if len(data) >= 12 and struct.unpack("!H", data[:2])[0] == qid:
                    break
                if time.monotonic() > deadline:
                    raise socket.timeout()
        except socket.timeout:
            raise ProbeError(f"{server}: no DNS answer within {timeout:g}s") from None
        except OSError as exc:
            raise ProbeError(f"{server}: {exc.strerror or exc}") from None
    _, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    if not flags & 0x8000:
        raise ProbeError(f"{server}: the answer is not a DNS response")
    rcode = flags & 0xF
    if rcode == 0 and not flags & 0x0080:
        raise ProbeError(f"{server}: answers without recursion (RA flag not set); it is not a recursive resolver")
    try:
        count = count_answers(data, qdcount, ancount, qtype)
    except (IndexError, struct.error):
        raise ProbeError(f"{server}: a malformed DNS answer") from None
    return RCODES.get(rcode, f"RCODE{rcode}"), count


def _skip_name(data: bytes, off: int) -> int:
    while True:
        n = data[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:  # a compression pointer ends the name
            return off + 2
        off += 1 + n


def count_answers(data: bytes, qdcount: int, ancount: int, qtype: int) -> int:
    """Answer records of the asked type (a CNAME alone doesn't count as an address)."""
    off = 12
    for _ in range(qdcount):
        off = _skip_name(data, off) + 4
    found = 0
    for _ in range(ancount):
        off = _skip_name(data, off)
        rtype, _, _, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10 + rdlen
        if off > len(data):
            raise IndexError
        found += rtype == qtype
    return found


def _resolve(host: str, port: int, socktype: int):
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socktype)
    except socket.gaierror as exc:
        raise ProbeError(f"{host}: cannot resolve the name ({exc.strerror})") from None
    if not infos:
        raise ProbeError(f"{host}: cannot resolve the name")
    return infos[0]


# -- interfaces and address probes --------------------------------------------------------

SYS_NET = "/sys/class/net"


def find_interface(name_or_mac: str, sys_net: str = SYS_NET) -> Optional[str]:
    """The local interface with this name or MAC, or None."""
    if not os.path.isdir(sys_net):
        return None
    if ":" not in name_or_mac:
        return name_or_mac if os.path.exists(os.path.join(sys_net, name_or_mac)) else None
    want = name_or_mac.lower()
    for iface in sorted(os.listdir(sys_net)):
        try:
            with open(os.path.join(sys_net, iface, "address"), encoding="ascii") as fh:
                if fh.read().strip().lower() == want:
                    return iface
        except OSError:
            continue
    return None


def interface_up(iface: str, sys_net: str = SYS_NET) -> bool:
    try:
        with open(os.path.join(sys_net, iface, "operstate"), encoding="ascii") as fh:
            return fh.read().strip() in ("up", "unknown")
    except OSError:
        return True  # can't tell; the gateway probe decides


def run_cmd(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run a command without a shell. Returns (exit code, stdout+stderr); 124 on timeout."""
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, str(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def local_ipv4_addresses() -> Optional[List[str]]:
    """IPv4 addresses on this host's interfaces (from `ip`), or None when they can't be read."""
    if not shutil.which("ip"):
        return None
    code, out = run_cmd(["ip", "-o", "-4", "addr", "show"], 5)
    if code != 0:
        return None
    found = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            found.append(parts[parts.index("inet") + 1].split("/", 1)[0])
    return found


ARPING = "arping"
PING = "ping"


def probe_method() -> Optional[str]:
    """iputils arping when present (duplicate address detection), else ping, else None."""
    if shutil.which("arping"):
        code, out = run_cmd(["arping", "-V"], 5)
        if "iputils" in out:
            return ARPING
    if shutil.which("ping"):
        return PING
    return None


def address_in_use(method: str, iface: str, address: str, timeout: float) -> Optional[bool]:
    """True if something answers for the address, False if nothing does, None if the probe failed."""
    wait = str(max(1, int(round(timeout))))
    if method == ARPING:
        # -D: duplicate address detection; exit 0 = no reply, 1 = a reply.
        code, _ = run_cmd(["arping", "-D", "-q", "-c", "2", "-w", wait, "-I", iface, address], timeout + 2)
        return {0: False, 1: True}.get(code)
    code, _ = run_cmd(["ping", "-n", "-q", "-c", "1", "-W", wait, "-I", iface, address], timeout + 2)
    return {0: True, 1: False}.get(code)
