"""Wave-6 review fixes for the bridge and the MCP CLI:

* ``owner.json`` gone -> ``OwnerGone`` (the old URL is never reused);
* forwarded tool calls have an overall time limit; oversized JSON responses are refused;
* wildcard HTTP binds publish a loopback URL (so the bridge can follow it);
* ``--allowed-host`` / ``--allow-remote`` without ``--http`` warn;
* ``--record FILE`` wraps the transport in a ``RecordingTransport``.

Only loopback sockets owned by the tests and sim scopes; never the real device, the default lock
dir or the default MCP port.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_helpers import run_client, text_of

import edu09.mcp_server.__main__ as cli
import edu09.mcp_server.bridge as bridge_mod
from edu09.device.scope import Scope
from edu09.mcp_server.bridge import OWNER_GONE, Bridge, OwnerUnreachable, probe
from edu09.mcp_server.server import client_url, start_http_in_thread
from edu09.transport.hid_transport import HidTransport
from edu09.transport.replay import RecordingTransport, load_records
from edu09.transport.sim import SimTransport


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "runtime"
    d.mkdir()
    monkeypatch.setenv("EDU09_RUNTIME_DIR", str(d))
    return d


class _Listener:
    """Loopback TCP listener that records connections and never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.hits = 0
        self._conns: list[socket.socket] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            self._conns.append(conn)  # keep it open, never answer

    def close(self) -> None:
        self._stop.set()
        self._t.join(5)
        for c in self._conns:
            c.close()
        self.sock.close()


@pytest.fixture
def silent() -> Iterator[_Listener]:
    lst = _Listener()
    try:
        yield lst
    finally:
        lst.close()


# -- owner.json gone -------------------------------------------------------------------------


def test_missing_owner_json_is_owner_gone_and_old_url_unused(
    silent: _Listener, lock_dir: Path
) -> None:
    """The owner exited (owner.json removed): something else may listen on its old port now."""
    br = Bridge(silent.url, lock_dir=lock_dir)

    async def fn(c: Client) -> tuple[Any, str]:
        res = await c.call_tool("get_status", {})
        with pytest.raises(MCPError) as ei:
            await c.list_tools(cache_mode="bypass")
        return res, ei.value.message

    t0 = time.monotonic()
    res, list_err = run_client(br.build_server(), fn)
    assert time.monotonic() - t0 < 5.0
    assert res.is_error and text_of(res).startswith(f"{OWNER_GONE}:")
    assert "exited" in text_of(res)
    assert list_err.startswith(f"{OWNER_GONE}:")
    assert silent.hits == 0  # the old URL was never contacted


# -- time and size limits ----------------------------------------------------------------------


def test_tool_call_has_overall_time_limit(silent: _Listener) -> None:
    """An owner that accepts but never answers: the per-read timeout (300 s) alone would hang;
    the overall limit ends the call."""
    assert bridge_mod.TOOL_CALL_TIMEOUT_S == 330.0
    br = Bridge(silent.url, follow_owner=False, tool_timeout_s=0.5)
    t0 = time.monotonic()
    res = run_client(br.build_server(), lambda c: c.call_tool("get_status", {}))
    assert time.monotonic() - t0 < 5.0
    assert res.is_error and text_of(res).startswith(f"{OWNER_GONE}:")
    assert "no answer within 0.5 s" in text_of(res)
    assert silent.hits >= 1


class _HugeJson(BaseHTTPRequestHandler):
    """Answers every POST with a (huge) application/json body, chunked, no Content-Length."""

    total = 16 * 1024 * 1024
    sent: list[int] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = b" " * 65536
        sent = 0
        try:
            while sent < self.total:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                sent += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            type(self).sent.append(sent)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def huge_owner() -> Iterator[str]:
    _HugeJson.sent = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HugeJson)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    finally:
        srv.shutdown()
        srv.server_close()


def test_huge_json_response_is_refused(huge_owner: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge_mod.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    monkeypatch.setattr(bridge_mod, "MAX_RESPONSE_BYTES", 1024 * 1024)
    with pytest.raises(OwnerUnreachable) as ei:
        probe(huge_owner, timeout_s=10.0)
    assert "larger than 1048576 bytes" in str(ei.value)

    br = Bridge(huge_owner, follow_owner=False)

    async def fn(c: Client) -> str:
        try:
            res = await c.call_tool("get_status", {})
        except MCPError as exc:
            return exc.message
        assert res.is_error
        return text_of(res)

    msg = run_client(br.build_server(), fn)
    assert "larger than 1048576 bytes" in msg


class _Body(httpx2.AsyncByteStream):
    """An unread response body (like the network transport's), in 10-byte chunks."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> Any:
        for i in range(0, len(self._data), 10):
            yield self._data[i : i + 10]


def _capped_get(content_type: str, body: bytes, limit: int = 100, **headers: str) -> bytes:
    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": content_type, **headers}, stream=_Body(body)
        )

    async def main() -> bytes:
        transport = bridge_mod._CappedTransport(httpx2.MockTransport(handler), limit)
        async with httpx2.AsyncClient(transport=transport) as http:
            r = await http.get("http://127.0.0.1/x")
            return r.content

    return anyio.run(main)


def test_capped_transport_limits_json_only() -> None:
    assert _capped_get("application/json", b"1" * 100) == b"1" * 100
    with pytest.raises(bridge_mod.ResponseTooLarge):
        _capped_get("application/json", b"1" * 101)
    # SSE passes (the SDK caps each event instead)
    assert len(_capped_get("text/event-stream", b"x" * 1000)) == 1000
    with pytest.raises(bridge_mod.ResponseTooLarge, match="encoded"):
        _capped_get("application/json", b"\x1f\x8b", **{"content-encoding": "gzip"})


# -- wildcard binds publish a loopback URL --------------------------------------------------


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8000/mcp"),
        ("::", "http://[::1]:8000/mcp"),
        ("[::]", "http://[::1]:8000/mcp"),
        ("127.0.0.1", "http://127.0.0.1:8000/mcp"),
        ("::1", "http://[::1]:8000/mcp"),
        ("localhost", "http://localhost:8000/mcp"),
        ("192.168.1.5", "http://192.168.1.5:8000/mcp"),
    ],
)
def test_client_url(host: str, url: str) -> None:
    assert client_url(host, 8000) == url


def test_wildcard_bind_publishes_loopback_url(
    mcp_scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[str | None] = []
    monkeypatch.setattr(mcp_scope, "set_mcp_url", published.append)
    with start_http_in_thread(mcp_scope, host="0.0.0.0", port=0, allow_remote=True) as h:
        assert h.url == f"http://127.0.0.1:{h.port}/mcp"
        assert published == [h.url]
        br = Bridge(h.url, follow_owner=False)  # the bridge accepts and reaches it
        res = run_client(br.build_server(), lambda c: c.call_tool("get_status", {}))
        assert not res.is_error, text_of(res)
    assert published[-1] is None


def test_cli_http_wildcard_passes_loopback_url_to_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_open(*a: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise cli.DeviceNotFound("no device (test)")

    monkeypatch.setattr(Scope, "open", fake_open)
    rc = cli.main(["--http", "--host", "0.0.0.0", "--allow-remote", "--port", "9999"])
    assert rc == cli.EXIT_DEVICE_ERROR
    assert seen["mcp_url"] == "http://127.0.0.1:9999/mcp"


# -- CLI flags --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--allowed-host", "scope.lan"], "--allowed-host"),
        (["--allow-remote"], "--allow-remote"),
    ],
)
def test_http_only_flags_without_http_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    named: str,
) -> None:
    import edu09.mcp_server.server as srv

    monkeypatch.setattr(srv, "run_stdio", lambda *_a, **_k: None)
    assert cli.main([*flags, "--simulate"]) == 0
    cap = capsys.readouterr()
    assert cap.out == ""  # stdout belongs to the MCP protocol
    assert "warning" in cap.err and named in cap.err and "--http" in cap.err


def test_no_warning_with_http_flags_used_correctly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import edu09.mcp_server.server as srv

    monkeypatch.setattr(srv, "run_stdio", lambda *_a, **_k: None)
    assert cli.main(["--simulate"]) == 0
    assert "warning" not in capsys.readouterr().err


def test_cli_record_simulated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import edu09.mcp_server.server as srv

    rec = tmp_path / "logs" / "rec.jsonl"
    kinds: list[str] = []

    def fake_stdio(scope: Scope, _opts: Any) -> None:
        kinds.append(scope.status.transport_kind)
        scope.acquire(trigger_mode="none")

    monkeypatch.setattr(srv, "run_stdio", fake_stdio)
    assert cli.main(["--simulate", "--record", str(rec)]) == 0
    assert kinds == ["sim"]
    records = load_records(rec)
    dirs = {r.dir for r in records}
    assert dirs == {"in", "out"} and len(records) > 16
    n = len(records)
    assert cli.main(["--simulate", "--record", str(rec)]) == 0  # appends
    assert len(load_records(rec)) > n
    assert all("t" in json.loads(line) for line in rec.read_text().splitlines())


def test_cli_record_hardware_wraps_hid_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hardware mode: the transport handed to Scope.open is a RecordingTransport around a
    (never opened) HidTransport whose info still says "hid"."""
    seen: list[Any] = []

    def fake_open(transport: Any = None, **_kw: Any) -> Any:
        seen.append(transport)
        raise cli.DeviceNotFound("no device (test)")

    monkeypatch.setattr(Scope, "open", fake_open)
    assert cli.main(["--no-bridge", "--record", str(tmp_path / "r.jsonl")]) == 1
    (t,) = seen
    assert isinstance(t, RecordingTransport)
    assert isinstance(t._inner, HidTransport)
    assert t.info.kind == "hid"
    seen.clear()
    assert cli.main(["--no-bridge"]) == 1
    assert seen == [None]  # without --record: Scope.open's default transport


def test_cli_record_unwritable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(Scope, "open", lambda *a, **k: opened.append(a))
    assert cli.main(["--simulate", "--record", str(tmp_path)]) == cli.EXIT_BUSY  # a directory
    assert "--record" in capsys.readouterr().err
    assert opened == []


def test_recording_transport_keeps_hardware_detection(tmp_path: Path) -> None:
    """Scope.open decides "hardware" from ``transport.info.kind``: through the wrapper a HID
    transport still takes the owner lock (and would use the calibration file)."""
    sim = SimTransport.virtual(seed=0)
    from edu09.transport.base import TransportInfo

    sim.info = TransportInfo(kind="hid", path="fake-hid")
    rec = RecordingTransport(sim, tmp_path / "r.jsonl")
    scope = Scope.open(rec, lock_dir=tmp_path / "lock", reconnect_interval_s=0.01)
    try:
        assert scope.owner_lock is not None
        assert scope.status.transport_kind == "hid" and not scope.status.simulated
        assert scope.calibration.source == "typical"  # HW fallback without a calibration file
    finally:
        scope.close()
    assert len(load_records(tmp_path / "r.jsonl")) > 0
