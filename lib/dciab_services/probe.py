"""Minimal DNS, NTP and HTTP clients for the readiness check (standard library only)."""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import urllib.request
from typing import Iterator, List, Optional, Tuple

TYPES = {"A": 1, "PTR": 12}


def backoff(start: float = 0.5, cap: float = 5.0) -> Iterator[float]:
    """Delays between readiness retries: 0.5, 1, 2, 4, 5, 5, ... seconds."""
    delay = start
    while True:
        yield delay
        delay = min(delay * 2, cap)


def wait(deadline: float, delays: Iterator[float]) -> bool:
    """Sleep for the next delay, never past deadline (time.monotonic()); False when it has passed.

    A sleep cut short by the deadline returns True on purpose: callers make one last check at
    the deadline, so a timeout overruns by at most one round of probes (seconds)."""
    import time
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    time.sleep(min(next(delays), left))
    return True


def reverse_name(ip: str) -> str:
    return ipaddress.ip_address(ip).reverse_pointer


def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"


def _read_name(msg: bytes, off: int) -> Tuple[str, int]:
    labels, end = [], None
    while True:
        n = msg[off]
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | msg[off + 1]
            continue
        if n == 0:
            off += 1
            break
        labels.append(msg[off + 1:off + 1 + n].decode())
        off += 1 + n
    return ".".join(labels), (end if end is not None else off)


def parse_answers(msg: bytes, qtype: int) -> List[str]:
    """The answers of one type in a DNS response: addresses for A, names for PTR."""
    _, flags, qd, an, _, _ = struct.unpack("!6H", msg[:12])
    if flags & 0xF:  # rcode
        return []
    off = 12
    for _ in range(qd):
        _, off = _read_name(msg, off)
        off += 4
    out = []
    for _ in range(an):
        _, off = _read_name(msg, off)
        rtype, _, _, rdlen = struct.unpack("!HHIH", msg[off:off + 10])
        off += 10
        if rtype == qtype == 1:
            out.append(socket.inet_ntoa(msg[off:off + 4]))
        elif rtype == qtype == 12:
            out.append(_read_name(msg, off)[0])
        off += rdlen
    return out


def dns(server: str, name: str, qtype: str, timeout: float = 2.0) -> Optional[List[str]]:
    """Answers for name from server over UDP, or None when the server doesn't answer."""
    qid = struct.unpack("!H", os.urandom(2))[0]
    query = struct.pack("!6H", qid, 0x0100, 1, 0, 0, 0) + _qname(name) + struct.pack("!HH", TYPES[qtype], 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(query, (server, 53))
            msg, _ = s.recvfrom(4096)
        except OSError:
            return None
    if len(msg) < 12 or struct.unpack("!H", msg[:2])[0] != qid:
        return None
    return parse_answers(msg, TYPES[qtype])


def ntp(server: str, timeout: float = 2.0) -> Optional[int]:
    """The stratum a server answers with (16 = unsynchronised), or None without an answer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(b"\x23" + b"\0" * 47, (server, 123))  # LI 0, version 4, mode 3 (client)
            msg, _ = s.recvfrom(512)
        except OSError:
            return None
    if len(msg) < 48 or msg[0] & 0x7 != 4:  # mode 4: server
        return None
    return msg[1]


def http(url: str, timeout: float = 2.0) -> bool:
    try:
        # Straight to the bootstrap host: never through a proxy from the environment.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False
