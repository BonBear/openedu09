"""Loopback/wildcard checks are deterministic across Python patch releases (CI fix).

CPython changed ``ipaddress`` properties of IPv4-mapped IPv6 addresses in a 3.12.x patch release
(``IPv6Address("::ffff:127.0.0.1").is_loopback`` became True). The server and the bridge must
give the same answer on every version, so the tests below also run with ``ipaddress`` patched to
the newer (and an even more permissive) behaviour and expect identical results.

Pure functions only; no sockets, never the real device.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import pytest

from edu09.mcp_server.bridge import check_owner_url
from edu09.mcp_server.netaddr import is_loopback_ip, is_unspecified_ip
from edu09.mcp_server.server import _is_loopback, check_host, client_url, transport_security

LOOPBACK = ["127.0.0.1", "127.255.255.254", "127.0.0.2", "::1", "[::1]", "0:0:0:0:0:0:0:1"]

NOT_LOOPBACK = [
    "::ffff:127.0.0.1",  # IPv4-mapped
    "[::ffff:127.0.0.1]",
    "::ffff:7f00:1",  # IPv4-mapped, hex spelling
    "0:0:0:0:0:ffff:127.0.0.1",
    "::127.0.0.1",  # IPv4-compatible (deprecated)
    "2002:7f00:1::",  # 6to4 of 127.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo (client 127.0.0.1)
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "::1%1",  # scoped
    "::1%eth0",
    "::",
    "0.0.0.0",
    "128.0.0.1",
    "126.255.255.255",
    "10.0.0.1",
    "fe80::1",
    "127.1",
    "2130706433",
    "localhost.",
    "",
    "[",
    "[]",
]


@pytest.fixture(params=["native", "new_semantics"])
def ipaddress_semantics(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """``native`` = this interpreter; ``new_semantics`` = IPv6 properties forced to follow any
    embedded IPv4 address (mapped/compatible/6to4/teredo), i.e. newer-or-looser than any CPython."""
    if request.param == "new_semantics":
        orig_loop = ipaddress.IPv6Address.is_loopback
        orig_unspec = ipaddress.IPv6Address.is_unspecified

        def embedded(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
            if ip.ipv4_mapped is not None:
                return ip.ipv4_mapped
            if ip.sixtofour is not None:
                return ip.sixtofour
            if ip.teredo is not None:
                return ip.teredo[1]
            if int(ip) >> 32 == 0 and int(ip) > 1:  # IPv4-compatible ::a.b.c.d
                return ipaddress.IPv4Address(int(ip))
            return None

        def loop(self: ipaddress.IPv6Address) -> bool:
            v4 = embedded(self)
            return v4.is_loopback if v4 is not None else bool(orig_loop.__get__(self))

        def unspec(self: ipaddress.IPv6Address) -> bool:
            v4 = embedded(self)
            return v4.is_unspecified if v4 is not None else bool(orig_unspec.__get__(self))

        monkeypatch.setattr(ipaddress.IPv6Address, "is_loopback", property(loop))
        monkeypatch.setattr(ipaddress.IPv6Address, "is_unspecified", property(unspec))
        assert ipaddress.IPv6Address("::ffff:127.0.0.1").is_loopback  # patch is active
    yield request.param


@pytest.mark.usefixtures("ipaddress_semantics")
@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_literals(host: str) -> None:
    assert is_loopback_ip(host)
    assert _is_loopback(host)
    check_host(host, allow_remote=False)


@pytest.mark.usefixtures("ipaddress_semantics")
@pytest.mark.parametrize("host", NOT_LOOPBACK)
def test_embedded_and_lookalike_forms_are_not_loopback(host: str) -> None:
    assert not is_loopback_ip(host)
    assert not _is_loopback(host)
    with pytest.raises(ValueError):
        check_host(host, allow_remote=False)


@pytest.mark.usefixtures("ipaddress_semantics")
@pytest.mark.parametrize(
    "host",
    [
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::127.0.0.1",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",
        "64:ff9b::7f00:1",
        "::1%25eth0",  # percent-encoded zone id
    ],
)
def test_bridge_refuses_ipv6_transition_forms(host: str) -> None:
    with pytest.raises(ValueError):
        check_owner_url(f"http://[{host}]:8765/mcp")


@pytest.mark.usefixtures("ipaddress_semantics")
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.9", "[::1]", "localhost", "LOCALHOST"])
def test_bridge_accepts_loopback(host: str) -> None:
    url = f"http://{host}:8765/mcp"
    assert check_owner_url(url) == url


@pytest.mark.usefixtures("ipaddress_semantics")
def test_unspecified_is_exact() -> None:
    assert is_unspecified_ip("0.0.0.0")
    assert is_unspecified_ip("::")
    assert is_unspecified_ip("[::]")
    for host in ("::ffff:0.0.0.0", "::ffff:0:0", "0.0.0.1", "::1", "localhost", "", "::%1"):
        assert not is_unspecified_ip(host), host


@pytest.mark.usefixtures("ipaddress_semantics")
def test_client_url_and_transport_security_are_deterministic() -> None:
    assert client_url("0.0.0.0", 8000) == "http://127.0.0.1:8000/mcp"
    assert client_url("::", 8000) == "http://[::1]:8000/mcp"
    # an IPv4-mapped wildcard is no wildcard here: published as given (and the bridge refuses it)
    assert client_url("::ffff:0.0.0.0", 8000) == "http://[::ffff:0.0.0.0]:8000/mcp"
    # an IPv4-mapped bind address is treated as remote: loopback names pinned to the port
    sec = transport_security("::ffff:127.0.0.1", 8765)
    assert sec.allowed_hosts == [
        "127.0.0.1:8765",
        "localhost:8765",
        "[::1]:8765",
        "[::ffff:127.0.0.1]:8765",
    ]
    sec = transport_security("::ffff:0.0.0.0", 8765)
    assert sec.allowed_hosts == [
        "127.0.0.1:8765",
        "localhost:8765",
        "[::1]:8765",
        "[::ffff:0.0.0.0]:8765",
    ]
