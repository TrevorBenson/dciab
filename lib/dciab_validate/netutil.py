"""Small IPv4 helpers. Ranges are inclusive (first, last) integer pairs."""

from __future__ import annotations

import ipaddress
from typing import Tuple

Span = Tuple[int, int]


class NetError(ValueError):
    pass


def ip(text: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        raise NetError(f"{text} is not a valid IPv4 address") from None


def net(text: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(text, strict=True)
    except ValueError as exc:
        if "host bits set" in str(exc):
            fixed = ipaddress.IPv4Network(text, strict=False)
            raise NetError(f"{text} has host bits set; the network address is {fixed}") from None
        raise NetError(f"{text} is not a valid IPv4 network") from None


def span_of_range(text: str) -> Span:
    first, _, last = text.partition("-")
    a, b = int(ip(first)), int(ip(last))
    if a > b:
        raise NetError(f"{text} starts after it ends")
    return a, b


def span_of_ip(text: str) -> Span:
    v = int(ip(text))
    return v, v


def span_of_net(n: ipaddress.IPv4Network) -> Span:
    return int(n.network_address), int(n.broadcast_address)


def overlaps(a: Span, b: Span) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def within(inner: Span, outer: Span) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def size(s: Span) -> int:
    return s[1] - s[0] + 1


def usable_hosts(n: ipaddress.IPv4Network) -> Span:
    """The span of addresses a host may use (excludes network and broadcast for /30 and larger)."""
    lo, hi = span_of_net(n)
    if n.prefixlen <= 30:
        return lo + 1, hi - 1
    return lo, hi
