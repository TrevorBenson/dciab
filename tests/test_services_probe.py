"""dciab services readiness probes against local fakes: a UDP DNS responder, an SNTP
responder and an HTTP server, plus timeouts and malformed answers (#215)."""

import http.server
import socket
import struct
import threading
import time

import pytest

from dciab_services import cli, probe


class UDPServer:
    """A one-thread UDP responder on 127.0.0.1: handler(request) -> reply bytes or None (stay silent)."""

    def __init__(self, handler):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.handler = handler
        self.requests = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests.append(data)
            reply = self.handler(data)
            if reply is not None:
                self.sock.sendto(reply, addr)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def question(query: bytes):
    """(id, the question section bytes, qtype) of a DNS query."""
    qid = struct.unpack("!H", query[:2])[0]
    off = 12
    while query[off]:
        off += 1 + query[off]
    off += 1
    qtype = struct.unpack("!H", query[off:off + 2])[0]
    return qid, query[12:off + 4], qtype


def dns_reply(query: bytes, records, rcode=0, qid=None):
    """An answer to query: records is a list of (type, rdata bytes); names use a pointer to
    the question (offset 12)."""
    rid, q, _ = question(query)
    head = struct.pack("!6H", rid if qid is None else qid, 0x8180 | rcode, 1, len(records), 0, 0)
    body = b"".join(b"\xc0\x0c" + struct.pack("!HHIH", t, 1, 300, len(r)) + r for t, r in records)
    return head + q + body


def a(ip):
    return (1, socket.inet_aton(ip))


def ptr(name):
    return (12, probe._qname(name))


class Zone:
    """A tiny authoritative server: {name: [records]}; unknown names are refused."""

    def __init__(self, names):
        self.names = names

    def __call__(self, query):
        qid, q, qtype = question(query)
        name, off = probe._read_name(query, 12)
        recs = [r for r in self.names.get(name, []) if r[0] == qtype]
        if name not in self.names:
            return dns_reply(query, [], rcode=5)   # REFUSED
        return dns_reply(query, recs)


def test_dns_forward_and_reverse():
    zone = Zone({"bootstrap.dc3.example.com": [a("10.30.110.5")],
                 "5.110.30.10.in-addr.arpa": [ptr("bootstrap.dc3.example.com")]})
    with UDPServer(zone) as srv:
        assert probe.dns("127.0.0.1", "bootstrap.dc3.example.com", "A", port=srv.port) == ["10.30.110.5"]
        assert probe.dns("127.0.0.1", probe.reverse_name("10.30.110.5"), "PTR",
                         port=srv.port) == ["bootstrap.dc3.example.com"]
        # a name outside the zones is refused: an empty answer, not a failure to answer
        assert probe.dns("127.0.0.1", "example.org", "A", port=srv.port) == []
        # the query is a standard recursive query for one question
        flags, qd = struct.unpack("!HH", srv.requests[0][2:6])
        assert (flags, qd) == (0x0100, 1)


def test_dns_several_answers_and_other_types_skipped():
    zone = Zone({"registry.dc3.example.com": [a("10.30.110.5"), a("10.30.110.6"), ptr("x.example")]})
    with UDPServer(zone) as srv:
        assert probe.dns("127.0.0.1", "registry.dc3.example.com", "A", port=srv.port) == ["10.30.110.5",
                                                                                         "10.30.110.6"]


def test_dns_silence_is_none():
    with UDPServer(lambda q: None) as srv:
        start = time.monotonic()
        assert probe.dns("127.0.0.1", "x.example", "A", timeout=0.3, port=srv.port) is None
        assert time.monotonic() - start < 2


def test_dns_nothing_listening_is_none():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe.dns("127.0.0.1", "x.example", "A", timeout=0.3, port=port) is None


@pytest.mark.parametrize("reply", [
    lambda q: b"\x00\x01",                                         # shorter than a header
    lambda q: dns_reply(q, [a("10.0.0.1")], qid=(question(q)[0] + 1) & 0xFFFF),  # another query's id
    lambda q: dns_reply(q, [a("10.0.0.1")])[:-3],                  # truncated answer
    lambda q: dns_reply(q, [(1, b"\x0a")])[:-1] + b"",             # rdata cut short
    lambda q: q[:2] + b"\x81\x80\x00\x00\x00\x01\x00\x00\x00\x00" + b"\xc0\x0c" + b"\x00" * 10,  # pointer loop
])
def test_dns_malformed_answer_is_none(reply):
    with UDPServer(reply) as srv:
        assert probe.dns("127.0.0.1", "bootstrap.dc3.example.com", "A", timeout=0.5, port=srv.port) in (None, [])


def test_dns_pointer_loop_is_none_not_a_hang():
    def loop(q):
        qid = q[:2]
        # header with one answer whose name points at itself (offset 12)
        return qid + b"\x81\x80\x00\x00\x00\x01\x00\x00\x00\x00" + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 1, 4) + b"\0" * 4
    with UDPServer(loop) as srv:
        assert probe.dns("127.0.0.1", "x.example", "A", timeout=0.5, port=srv.port) is None


def sntp(stratum=2, mode=4, length=48):
    def reply(query):
        assert query[0] == 0x23   # client, version 4
        return (bytes([0x20 | mode, stratum]) + b"\0" * 46)[:length]
    return reply


def test_ntp_stratum():
    with UDPServer(sntp(stratum=3)) as srv:
        assert probe.ntp("127.0.0.1", port=srv.port) == 3
    with UDPServer(sntp(stratum=16)) as srv:   # unsynchronised: reported as is
        assert probe.ntp("127.0.0.1", port=srv.port) == 16


@pytest.mark.parametrize("server", [sntp(mode=3), sntp(length=20), lambda q: None])
def test_ntp_not_a_server_answer_is_none(server):
    with UDPServer(server) as srv:
        assert probe.ntp("127.0.0.1", timeout=0.3, port=srv.port) is None


class Handler(http.server.BaseHTTPRequestHandler):
    """/healthz -> 200 "ok"; /isos/test.iso honours Range; /slow sleeps; anything else 404."""
    body = b"0123456789"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, b"ok")
        elif self.path == "/slow":
            time.sleep(1.5)
            self._send(200, b"late")
        elif self.path == "/error":
            self._send(500, b"no")
        elif self.path == "/isos/test.iso":
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start, end = (int(x) for x in rng[6:].split("-"))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.body)}")
                part = self.body[start:end + 1]
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                self.wfile.write(part)
            else:
                self._send(200, self.body)
        else:
            self._send(404, b"")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # the timeout test hangs up on /slow on purpose


@pytest.fixture
def web():
    srv = QuietServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http(web, monkeypatch):
    # a proxy from the environment must not be used: the probe goes straight to the host
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert probe.http(f"{web}/healthz")
    assert not probe.http(f"{web}/missing")
    assert not probe.http(f"{web}/error")
    assert not probe.http(f"{web}/slow", timeout=0.3)


def test_http_nothing_listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not probe.http(f"http://127.0.0.1:{port}/healthz", timeout=0.3)


def test_fake_web_honours_range(web):
    """The fake serves ranges like Caddy does (BMCs read virtual-media ISOs in ranges)."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"{web}/isos/test.iso", headers={"Range": "bytes=2-5"})
    with opener.open(req, timeout=2) as r:
        assert r.status == 206 and r.read() == b"2345"


def test_backoff_and_wait():
    delays = probe.backoff(start=0.5, cap=5.0)
    assert [next(delays) for _ in range(6)] == [0.5, 1, 2, 4, 5, 5]
    assert not probe.wait(time.monotonic() - 1, probe.backoff())
    start = time.monotonic()
    assert probe.wait(start + 0.05, probe.backoff())   # cut short by the deadline, still True
    assert time.monotonic() - start < 0.5


def test_check_reports_each_failure(monkeypatch):
    """cli.check names every failing probe, and retries until the deadline."""
    doc = {"spec": {"network": {"domain": "dc3.example.com", "dnsServiceIPs": ["10.0.0.53", "10.0.0.54"],
                                "nodes": {"cidr": "10.0.0.0/24"}},
                    "bootstrap": {"address": "10.0.0.5", "interface": "eth0"},
                    "time": {"servedBy": "site"}, "hypervisors": {"hosts": []}}}
    calls = []

    def fake_dns(ip, name, qtype, **kw):
        calls.append((ip, name, qtype))
        if ip == "10.0.0.54":
            return None
        return ["bootstrap.dc3.example.com"] if qtype == "PTR" else ["10.0.0.5"]

    monkeypatch.setattr(probe, "dns", fake_dns)
    monkeypatch.setattr(probe, "http", lambda url, **kw: False)
    monkeypatch.setattr(probe, "wait", lambda deadline, delays: False)
    failed = cli.check(doc, timeout=0)
    assert any("10.0.0.54" in f and "bootstrap.dc3.example.com" in f for f in failed)
    assert any(f.startswith("HTTP on 10.0.0.5") for f in failed)
    assert not any("10.0.0.53" in f for f in failed)
    monkeypatch.setattr(probe, "dns", lambda ip, name, qtype, **kw:
                        ["bootstrap.dc3.example.com"] if qtype == "PTR" else ["10.0.0.5"])
    monkeypatch.setattr(probe, "http", lambda url, **kw: True)
    assert cli.check(doc, timeout=0) == []
