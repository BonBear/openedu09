"""Deterministic loopback / wildcard checks for IP literals (no DNS, no Python-version drift).

``ipaddress``' ``is_loopback`` / ``is_unspecified`` changed between CPython patch releases for
IPv4-mapped IPv6 addresses (``::ffff:127.0.0.1`` is loopback on newer 3.12.x/3.13, not on older
ones). The MCP server and bridge must not depend on that, so these helpers accept only the
canonical forms and fail closed for every IPv6 transition/embedding form (IPv4-mapped,
IPv4-compatible ``::127.0.0.1``, 6to4, Teredo, NAT64, ...) and for scoped addresses
(``::1%eth0``).
"""

from __future__ import annotations

import ipaddress
from typing import Final

__all__ = ["ip_literal", "is_loopback_ip", "is_unspecified_ip"]

_V4_LOOPBACK: Final = ipaddress.IPv4Network("127.0.0.0/8")
_V6_LOOPBACK: Final = ipaddress.IPv6Address("::1")
_V4_ANY: Final = ipaddress.IPv4Address("0.0.0.0")
_V6_ANY: Final = ipaddress.IPv6Address("::")


def ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` (optionally ``[bracketed]``) parsed as a strict IP literal, else ``None``.

    Only dotted-quad IPv4 and standard IPv6 text forms parse (``ipaddress`` rejects ``127.1``,
    decimal/hex/octal forms).
    """
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None


def is_loopback_ip(host: str) -> bool:
    """True only for an IPv4 literal in ``127.0.0.0/8`` or exactly ``::1`` (no scope id)."""
    ip = ip_literal(host)
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _V4_LOOPBACK
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.scope_id is None and ip == _V6_LOOPBACK
    return False


def is_unspecified_ip(host: str) -> bool:
    """True only for exactly ``0.0.0.0`` or ``::`` (not ``::ffff:0.0.0.0``, no scope id)."""
    ip = ip_literal(host)
    if isinstance(ip, ipaddress.IPv4Address):
        return ip == _V4_ANY
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.scope_id is None and ip == _V6_ANY
    return False
