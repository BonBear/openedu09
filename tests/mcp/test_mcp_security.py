"""HTTP Host/Origin checks (DNS-rebinding protection), ``--allow-remote``, ``--allowed-host``."""

from __future__ import annotations

import http.client
import json
import logging
from typing import Any

import pytest

import edu09.mcp_server.__main__ as cli
from edu09.device.scope import Scope
from edu09.errors import DeviceNotFound
from edu09.mcp_server.server import check_host, start_http_in_thread, transport_security

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


def test_loopback_defaults() -> None:
    sec = transport_security("127.0.0.1", 8765)
    assert sec.enable_dns_rebinding_protection
    assert sec.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert sec.allowed_origins == [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    # Other loopback binds (the SDK would disable the protection for these).
    assert transport_security("127.0.0.2", 8765).allowed_hosts[-1] == "127.0.0.2:*"
    assert transport_security("::1", 1).allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_remote_bind_is_restricted() -> None:
    sec = transport_security(
        "0.0.0.0", 8765, ["scope.lan", "10.0.0.5:9000", "fe80::1", "[::2]", "box:*"]
    )
    assert sec.enable_dns_rebinding_protection
    assert sec.allowed_hosts == [
        "127.0.0.1:8765",
        "localhost:8765",
        "[::1]:8765",
        "scope.lan:8765",
        "10.0.0.5:9000",
        "[fe80::1]:8765",
        "[::2]:8765",
        "box:*",
    ]
    # full-list comparisons (no substring/membership checks on URLs; CodeQL
    # py/incomplete-url-substring-sanitization); the wildcard bind host itself is not listed
    assert sec.allowed_origins == [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://[::1]:8765",
        "http://scope.lan:8765",
        "http://10.0.0.5:9000",
        "http://[fe80::1]:8765",
        "http://[::2]:8765",
        "http://box:*",
    ]
    sec = transport_security("192.168.1.10", 8765)
    assert sec.allowed_hosts == [
        "127.0.0.1:8765",
        "localhost:8765",
        "[::1]:8765",
        "192.168.1.10:8765",
    ]
    assert transport_security("192.168.1.10", 0).allowed_hosts[-1] == "192.168.1.10:*"


@pytest.mark.parametrize(
    "entry", ["", "http://x", "a:b", "x:0", "x:70000", "u@x", "x/y", "[::1", "[::1]x", "a b"]
)
def test_bad_allowed_host(entry: str) -> None:
    with pytest.raises(ValueError):
        transport_security("127.0.0.1", 8765, [entry])


def test_remote_warning_is_prominent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="edu09.mcp_server.server"):
        check_host("0.0.0.0", True)
    assert "WITHOUT AUTHENTICATION" in caplog.text


def _post(port: int, host: str | None = None, origin: str | None = None) -> int:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Host": host if host is not None else f"127.0.0.1:{port}",
    }
    if origin is not None:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/mcp", body=json.dumps(_INIT), headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


def test_http_server_enforces_host_header(mcp_scope: Scope) -> None:
    with start_http_in_thread(mcp_scope, port=0, allowed_hosts=["scope.lan"]) as h:
        port = h.port
        assert _post(port) == 200
        assert _post(port, host=f"localhost:{port}") == 200
        assert _post(port, host=f"scope.lan:{port}") == 200  # port=0 -> any port
        assert _post(port, host=f"evil.example.com:{port}") == 421
        assert _post(port, origin="http://evil.example.com") == 403
        assert _post(port, origin=f"http://scope.lan:{port}") == 200


def test_start_http_rejects_bad_allowed_host(mcp_scope: Scope) -> None:
    with pytest.raises(ValueError, match="allowed host"):
        start_http_in_thread(mcp_scope, port=0, allowed_hosts=["http://x"])


def test_cli_allowed_host(capsys: pytest.CaptureFixture[str]) -> None:
    a = cli.build_parser().parse_args(["--allowed-host", "a", "--allowed-host", "b:1"])
    assert a.allowed_host == ["a", "b:1"] and a.no_bridge is False
    assert cli.main(["--http", "--allowed-host", "http://x", "--simulate"]) == cli.EXIT_BUSY
    assert "--allowed-host" in capsys.readouterr().err


def test_cli_passes_allowed_hosts(monkeypatch: pytest.MonkeyPatch, mcp_scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def fake_run_http(scope: Scope, opts: Any, host: str, port: int, **kw: Any) -> None:
        seen.update(kw, host=host, port=port)

    monkeypatch.setattr(Scope, "open", lambda *a, **k: mcp_scope)
    monkeypatch.setattr("edu09.mcp_server.server.run_http", fake_run_http)
    argv = ["--http", "--host", "0.0.0.0", "--allow-remote", "--allowed-host", "scope.lan"]
    assert cli.main(argv) == 0
    assert seen == {
        "host": "0.0.0.0",
        "port": 8765,
        "allow_remote": True,
        "allowed_hosts": ["scope.lan"],
    }


def test_cli_remote_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(Scope, "open", lambda *a, **k: (_ for _ in ()).throw(DeviceNotFound("x")))
    with caplog.at_level(logging.WARNING):
        assert cli.main(["--http", "--host", "0.0.0.0", "--allow-remote"]) == cli.EXIT_DEVICE_ERROR
    assert "WITHOUT AUTHENTICATION" in caplog.text
