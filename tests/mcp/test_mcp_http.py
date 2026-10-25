"""Streamable HTTP: in-thread server start/stop (GUI path) and owner.json URL publishing."""

from __future__ import annotations

import contextlib
import socket

import pytest
from mcp import Client
from mcp_helpers import ok, run_client

from edu09.device.scope import Scope
from edu09.mcp_server.server import (
    HttpServerHandle,
    ServerOptions,
    check_host,
    mcp_url,
    start_http_in_thread,
)


def test_http_smoke(mcp_scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str | None] = []
    monkeypatch.setattr(mcp_scope, "set_mcp_url", urls.append)
    handle = start_http_in_thread(mcp_scope, ServerOptions(), port=0)
    try:
        assert isinstance(handle, HttpServerHandle)
        assert handle.running
        assert handle.port > 0
        assert handle.url == f"http://127.0.0.1:{handle.port}/mcp"
        assert urls == [handle.url]

        async def fn(c: Client) -> list[str]:
            return [t.name for t in (await c.list_tools()).tools]

        names = run_client(handle.url, fn)
        assert "get_status" in names and "calibrate" not in names
        st = ok(handle.url, "get_status")
        assert st["connected"] is True and st["simulated"] is True
        assert ok(handle.url, "acquire", {"max_points": 32})["waveform_id"] == "w1"
    finally:
        handle.stop()
    assert not handle.running
    assert urls == [handle.url, None]
    handle.stop()  # idempotent
    assert urls == [handle.url, None]


def test_http_restart_on_same_scope(mcp_scope: Scope) -> None:
    """The GUI toggles the server: each start builds a fresh server."""
    for _ in range(2):
        with start_http_in_thread(mcp_scope, port=0) as handle:
            assert ok(handle.url, "get_status")["connected"] is True
        assert not handle.running


def test_http_port_in_use(mcp_scope: Scope) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(OSError, match="could not start"):
            start_http_in_thread(mcp_scope, port=port)


def test_remote_host_requires_allow_remote(mcp_scope: Scope) -> None:
    with pytest.raises(ValueError, match="allow_remote"):
        start_http_in_thread(mcp_scope, host="0.0.0.0", port=0)
    check_host("127.0.0.1", False)
    check_host("localhost", False)
    check_host("::1", False)
    check_host("0.0.0.0", True)
    with pytest.raises(ValueError):
        check_host("192.168.1.10", False)


def test_mcp_url_formatting() -> None:
    assert mcp_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/mcp"
    assert mcp_url("::1", 8765) == "http://[::1]:8765/mcp"


def _post(url: str, headers: dict[str, str]) -> int:
    import json
    import urllib.error
    import urllib.request

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return e.code


def test_http_dns_rebinding_protection_on_by_default(mcp_scope: Scope) -> None:
    """The SDK's loopback default (Host/Origin allow-list) must stay active (review)."""
    with start_http_in_thread(mcp_scope, port=0) as handle:
        assert _post(handle.url, {}) == 200
        assert _post(handle.url, {"origin": f"http://localhost:{handle.port}"}) == 200
        assert _post(handle.url, {"host": "evil.example:8765"}) == 421
        assert _post(handle.url, {"origin": "http://evil.example"}) == 403
        assert _post(handle.url, {"content-type": "text/plain"}) == 400


def test_http_stop_with_active_client(mcp_scope: Scope) -> None:
    """stop() while a client session is open must return promptly and free the port."""
    import time

    import anyio

    handle = start_http_in_thread(mcp_scope, port=0)
    port = handle.port
    stopped: list[float] = []

    async def main() -> None:
        async with Client(handle.url) as c:
            await c.list_tools()
            t0 = time.monotonic()
            await anyio.to_thread.run_sync(handle.stop)
            stopped.append(time.monotonic() - t0)

    with contextlib.suppress(BaseException):  # the client may see the dropped connection
        anyio.run(main)
    assert stopped and stopped[0] < 6.0
    assert not handle.running
    with socket.socket() as s:  # the port is free again
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
