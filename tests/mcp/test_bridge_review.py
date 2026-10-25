"""Review (wave 6): bridge hardening against a hostile or misconfigured environment.

* owner URLs that only *look* local (``127.1``, decimal/hex/octal IPv4, ``localhost.``, IDN,
  IPv4-mapped IPv6, ``0.0.0.0``, user info) are refused;
* environment proxies (``HTTP_PROXY`` ...) are ignored: forwarded requests must reach the
  loopback owner directly, never a proxy;
* an owner that answers with an off-origin redirect is not followed.

Only loopback sockets owned by this test and sim scopes; never the real device or lock dir.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from edu09.device.scope import Scope
from edu09.mcp_server.bridge import Bridge, OwnerUnreachable, check_owner_url, probe
from edu09.mcp_server.server import start_http_in_thread


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1/mcp",  # inet_aton short form
        "http://2130706433/mcp",  # decimal
        "http://0x7f000001/mcp",  # hex
        "http://0177.0.0.1/mcp",  # octal
        "http://0.0.0.0:8765/mcp",  # reaches local servers on Linux, but not a loopback literal
        "http://[::]:8765/mcp",
        "http://[::ffff:127.0.0.1]:8765/mcp",  # IPv4-mapped (refused: fail closed)
        "http://localhost./mcp",  # trailing dot = absolute DNS name
        "http://localhost%2e/mcp",
        "http://ｌｏｃａｌｈｏｓｔ/mcp",  # fullwidth
        "http://xn--localhost/mcp",
        "http://127.0.0.1\\@evil.example.com/mcp",
        "http://evil.example.com#@127.0.0.1/mcp",
        "http://127.0.0.1%2F@evil.example.com/mcp",
        "http://127.0.0.1:8765@evil.example.com/mcp",
        "http://:pw@127.0.0.1:8765/mcp",
        "http://127.0.0.1./mcp",
        "//127.0.0.1/mcp",
    ],
)
def test_lookalike_loopback_urls_refused(url: str) -> None:
    with pytest.raises(ValueError):
        check_owner_url(url)


class _Recorder:
    """A loopback TCP listener that records (and drops) every connection."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits: list[bytes] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1.0)
                try:
                    self.hits.append(conn.recv(4096))
                except OSError:
                    self.hits.append(b"")

    def close(self) -> None:
        self._stop.set()
        self._t.join(5)
        self.sock.close()


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    r = _Recorder()
    try:
        yield r
    finally:
        r.close()


@pytest.mark.parametrize("var", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
def test_environment_proxy_is_ignored(
    mcp_scope: Scope, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """httpx would send loopback traffic to an environment proxy (no NO_PROXY default)."""
    for v in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(var, f"http://127.0.0.1:{recorder.port}")
    with start_http_in_thread(mcp_scope, port=0) as h:
        info = probe(h.url, timeout_s=10.0)
        assert info.name == "edu09"
        br = Bridge(h.url, follow_owner=False, info=info)
        from mcp_helpers import run_client, text_of

        res: Any = run_client(br.build_server(), lambda c: c.call_tool("get_status", {}))
        assert not res.is_error, text_of(res)
    assert recorder.hits == []


def test_off_origin_redirect_is_not_followed(recorder: _Recorder) -> None:
    """A (malicious) owner answering 307 to another host/port: the request is not re-sent."""
    target = f"http://127.0.0.1:{recorder.port}/mcp"

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.send_response(307)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST  # noqa: N815

        def log_message(self, *a: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(OwnerUnreachable):
            probe(f"http://127.0.0.1:{srv.server_address[1]}/mcp", timeout_s=10.0)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert recorder.hits == []
