"""T12: stdio <-> HTTP bridge to the device owner (architecture.md 3.4).

Every "owner" here is a sim Scope served by ``start_http_in_thread`` on a free loopback port,
plus a real ``OwnerLock`` in a per-test lock dir. Nothing touches the real device or the default
lock dir (``EDU09_RUNTIME_DIR`` points into tmp_path).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_helpers import run_client, text_of

import edu09.mcp_server.__main__ as cli
import edu09.mcp_server.bridge as bridge_mod
from edu09.device.lock import OWNER_FILENAME, OwnerInfo, OwnerLock
from edu09.device.scope import Scope
from edu09.errors import DeviceBusy
from edu09.mcp_server.bridge import (
    OWNER_GONE,
    Bridge,
    OwnerUnreachable,
    check_owner_url,
    probe,
    resolve_owner,
)
from edu09.mcp_server.server import HttpServerHandle, ServerOptions, start_http_in_thread
from edu09.transport.sim import SimTransport


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "runtime"
    d.mkdir()
    monkeypatch.setenv("EDU09_RUNTIME_DIR", str(d))
    return d


@dataclass
class Owner:
    scope: Scope
    handle: HttpServerHandle
    lock: OwnerLock

    @property
    def url(self) -> str:
        return self.handle.url

    def info(self) -> OwnerInfo:
        return self.lock.info()

    def restart_http(self) -> None:
        self.handle.stop()
        self.handle = start_http_in_thread(self.scope, ServerOptions(owner_kind="gui"), port=0)
        self.lock.set_mcp_url(self.handle.url)


@pytest.fixture
def owner(lock_dir: Path, tmp_path: Path) -> Iterator[Owner]:
    """A fake GUI: owns the (tmp) lock, serves a sim Scope over HTTP, URL in owner.json."""
    scope = Scope.open(SimTransport.virtual(seed=3), reconnect_interval_s=0.01)
    lock = OwnerLock("gui", lock_dir=lock_dir)
    lock.acquire()
    opts = ServerOptions(owner_kind="gui", export_dir=tmp_path / "exports")
    handle = start_http_in_thread(scope, opts, port=0)
    lock.set_mcp_url(handle.url)
    o = Owner(scope, handle, lock)
    try:
        yield o
    finally:
        o.handle.stop()
        lock.release()
        scope.close()


def _bridge(owner: Owner, lock_dir: Path) -> Bridge:
    return Bridge(owner.url, owner.info(), lock_dir=lock_dir, info=probe(owner.url))


def _payload(res: Any) -> Any:
    assert not res.is_error, text_of(res)
    return json.loads(text_of(res))


# -- URL policy ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/mcp",
        "http://127.3.4.5:1/mcp",
        "http://[::1]:8765/mcp",
        "http://localhost:8765/mcp",
        "https://LOCALHOST/mcp",
    ],
)
def test_loopback_urls_accepted(url: str) -> None:
    assert check_owner_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:8765/mcp",
        "http://0.0.0.0:8765/mcp",
        "http://evil.example.com/mcp",
        "http://127.0.0.1.evil.example.com/mcp",
        "http://localhost.evil.example.com/mcp",
        "http://user@127.0.0.1:8765/mcp",
        "file:///etc/passwd",
        "ftp://127.0.0.1/mcp",
        "http:///mcp",
        "http://127.0.0.1:99999/mcp",
        "not a url",
    ],
)
def test_non_loopback_urls_refused(url: str) -> None:
    with pytest.raises(ValueError):
        check_owner_url(url)
    with pytest.raises(ValueError):
        Bridge(url)
    with pytest.raises(ValueError):
        probe(url)


# -- owner resolution ----------------------------------------------------------------------


def test_resolve_owner_prefers_given_url(lock_dir: Path) -> None:
    o = OwnerInfo(pid=1, kind="gui", mcp_url="http://127.0.0.1:1/mcp")
    assert resolve_owner(o, lock_dir, wait_s=0) is o


def test_resolve_owner_waits_for_url(lock_dir: Path) -> None:
    """DeviceBusy without owner info (owner.json mid-replace) / URL published a bit later."""
    lock = OwnerLock("gui", lock_dir=lock_dir)
    lock.acquire()
    try:
        timer = threading.Timer(0.15, lock.set_mcp_url, args=("http://127.0.0.1:9/mcp",))
        timer.start()
        t0 = time.monotonic()
        got = resolve_owner(None, lock_dir, wait_s=3.0)
        timer.join()
        assert got is not None and got.mcp_url == "http://127.0.0.1:9/mcp"
        assert time.monotonic() - t0 < 2.0
    finally:
        lock.release()


def test_resolve_owner_without_url_returns_last_seen(lock_dir: Path) -> None:
    assert resolve_owner(None, lock_dir, wait_s=0.1) is None
    lock = OwnerLock("mcp", lock_dir=lock_dir)
    lock.acquire()
    try:
        got = resolve_owner(None, lock_dir, wait_s=0.1)
        assert got is not None and got.kind == "mcp" and got.mcp_url is None
    finally:
        lock.release()


def test_probe_unreachable(owner: Owner) -> None:
    url = owner.url
    owner.handle.stop()
    with pytest.raises(OwnerUnreachable, match="cannot reach"):
        probe(url, timeout_s=5.0)


# -- forwarding (in-process stdio side) ----------------------------------------------------


def test_bridge_forwards_tools_and_resources(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)
    assert br.info.name == "edu09" and br.info.instructions

    async def fn(c: Client) -> dict[str, Any]:
        out: dict[str, Any] = {}
        out["instructions"] = c.instructions
        out["tools"] = [t.name for t in (await c.list_tools()).tools]
        out["acq"] = _payload(await c.call_tool("acquire", {"max_points": 16}))
        out["cfg_err"] = await c.call_tool("configure", {"volts_per_div": 123})
        out["unknown"] = await c.call_tool("no_such_tool", {})
        out["resources"] = [str(r.uri) for r in (await c.list_resources()).resources]
        tpl = (await c.list_resource_templates()).resource_templates
        out["templates"] = [t.uri_template for t in tpl]
        rr = await c.read_resource(f"edu09://waveforms/{out['acq']['waveform_id']}.csv")
        out["csv"] = rr.contents[0].text  # type: ignore[union-attr]
        with pytest.raises(MCPError, match="NotFound"):
            await c.read_resource("edu09://waveforms/w999")
        return out

    out = run_client(br.build_server(), fn)
    assert out["instructions"] == br.info.instructions
    assert {"get_status", "acquire", "configure", "auto_set"} <= set(out["tools"])
    assert "calibrate" not in out["tools"]
    assert out["acq"]["waveform_id"] == "w1"
    assert out["cfg_err"].is_error and text_of(out["cfg_err"]).startswith("ValueError:")
    assert text_of(out["unknown"]).startswith("NotFound:")
    assert "edu09://status" in out["resources"]
    assert "edu09://waveforms/{waveform_id}" in out["templates"]
    assert out["csv"].startswith("# edu09 waveform export")
    # Shared state: the waveform acquired through the bridge lives in the owner's scope.
    assert [w.waveform_id for w in owner.scope.store.snapshot()] == ["w1"]


def test_owner_gone_mid_session(owner: Owner, lock_dir: Path) -> None:
    """GUI closed: HTTP server stops, lock released (owner.json removed)."""
    br = _bridge(owner, lock_dir)

    async def fn(c: Client) -> tuple[Any, Any, str]:
        before = await c.call_tool("get_status", {})
        owner.handle.stop()
        owner.lock.release()
        assert not (lock_dir / OWNER_FILENAME).exists()
        after = await c.call_tool("get_status", {})
        with pytest.raises(MCPError) as ei:
            await c.list_tools(cache_mode="bypass")
        return before, after, ei.value.message

    before, after, list_err = run_client(br.build_server(), fn)
    assert _payload(before)["connected"] is True
    assert after.is_error
    msg = text_of(after)
    assert msg.startswith(f"{OWNER_GONE}:") and "restart edu09-mcp" in msg
    assert "pid" in msg  # names the owner
    assert list_err.startswith(f"{OWNER_GONE}:")


def test_owner_stops_mcp_server_keeps_device(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)
    owner.handle.stop()
    owner.lock.set_mcp_url(None)  # what the GUI's MCP toggle does

    async def fn(c: Client) -> Any:
        return await c.call_tool("get_status", {})

    res = run_client(br.build_server(), fn)
    assert res.is_error
    assert text_of(res).startswith(f"{OWNER_GONE}:") and "stopped its MCP server" in text_of(res)


def test_bridge_follows_owner_restart_on_new_port(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)
    old = owner.url
    owner.restart_http()
    assert owner.url != old

    async def fn(c: Client) -> Any:
        return await c.call_tool("get_status", {})

    assert _payload(run_client(br.build_server(), fn))["connected"] is True
    assert br.url == owner.url


def test_new_owner_without_url(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)
    owner.handle.stop()
    owner.lock.release()
    other = OwnerLock("mcp", lock_dir=lock_dir)
    other.acquire()
    try:
        # Same pid in this test process: fake a different owner in owner.json.
        doc = json.loads((lock_dir / OWNER_FILENAME).read_text())
        doc["pid"] = (doc["pid"] or 0) + 1
        (lock_dir / OWNER_FILENAME).write_text(json.dumps(doc))

        async def fn(c: Client) -> Any:
            return await c.call_tool("get_status", {})

        res = run_client(br.build_server(), fn)
    finally:
        other.release()
    assert res.is_error and text_of(res).startswith("DeviceBusy:")
    assert "mcp process" in text_of(res)


def test_planted_remote_url_mid_session_refused(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)
    owner.lock.set_mcp_url("http://203.0.113.7:8765/mcp")

    async def fn(c: Client) -> Any:
        return await c.call_tool("get_status", {})

    res = run_client(br.build_server(), fn)
    assert res.is_error and "loopback" in text_of(res)
    assert br.url == owner.url  # never switched to the planted URL


def test_follow_owner_disabled(owner: Owner, lock_dir: Path) -> None:
    br = Bridge(owner.url, owner.info(), lock_dir=lock_dir, follow_owner=False)
    owner.lock.set_mcp_url("http://203.0.113.7:8765/mcp")
    res = run_client(br.build_server(), lambda c: c.call_tool("get_status", {}))
    assert _payload(res)["connected"] is True


def test_concurrent_calls_through_bridge(owner: Owner, lock_dir: Path) -> None:
    br = _bridge(owner, lock_dir)

    async def fn(c: Client) -> list[Any]:
        results: list[Any] = []

        async def one() -> None:
            results.append(await c.call_tool("get_status", {}))

        async with anyio.create_task_group() as tg:
            for _ in range(5):
                tg.start_soon(one)
        return results

    results = run_client(br.build_server(), fn)
    assert len(results) == 5 and all(not r.is_error for r in results)


# -- CLI integration (in-process; Scope.open patched, never touches hardware) ---------------


def _busy_open(owner_info: OwnerInfo | None) -> Any:
    def fake_open(*a: Any, **kw: Any) -> Scope:
        raise DeviceBusy("EDU09 is in use", owner=owner_info)

    return fake_open


def test_cli_starts_bridge(
    owner: Owner, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[Bridge] = []
    monkeypatch.setattr(Scope, "open", _busy_open(owner.info()))
    monkeypatch.setattr(Bridge, "run_stdio", lambda self: started.append(self))
    assert cli.main(["--allow-calibration"]) == 0
    assert len(started) == 1 and started[0].url == owner.url
    assert started[0].info.instructions
    cap = capsys.readouterr()
    assert cap.out == ""


def test_cli_no_bridge_flag(
    owner: Owner, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Scope, "open", _busy_open(owner.info()))
    monkeypatch.setattr(Bridge, "run_stdio", lambda self: pytest.fail("bridge started"))
    assert cli.main(["--no-bridge"]) == cli.EXIT_BUSY
    err = capsys.readouterr().err
    assert owner.url in err and "--no-bridge" in err


def test_cli_owner_without_url(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "OWNER_URL_WAIT_S", 0.1)
    monkeypatch.setattr(Scope, "open", _busy_open(OwnerInfo(pid=7, kind="mcp")))
    assert cli.main([]) == cli.EXIT_BUSY
    cap = capsys.readouterr()
    assert cap.out == "" and "Close the other program" in cap.err


def test_cli_refuses_planted_remote_url(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = OwnerInfo(pid=7, kind="gui", mcp_url="http://203.0.113.7:8765/mcp")
    monkeypatch.setattr(Scope, "open", _busy_open(planted))
    monkeypatch.setattr(bridge_mod, "probe", lambda *a, **k: pytest.fail("probed"))
    assert cli.main([]) == cli.EXIT_BUSY
    assert "loopback" in capsys.readouterr().err


def test_cli_owner_vanished_falls_back_to_direct_open(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeviceBusy, but the owner exits before the probe: the next open succeeds."""
    info = owner.info()
    owner.handle.stop()
    calls: list[dict[str, Any]] = []
    served: list[Scope] = []
    real_open = Scope.open

    def fake_open(*a: Any, **kw: Any) -> Scope:
        calls.append(kw)
        if len(calls) == 1:
            raise DeviceBusy("EDU09 is in use", owner=info)
        return real_open(SimTransport.virtual(seed=1))

    monkeypatch.setattr(Scope, "open", fake_open)
    monkeypatch.setattr(
        "edu09.mcp_server.server.run_stdio", lambda scope, opts=None: served.append(scope)
    )
    assert cli.main([]) == 0
    assert len(calls) == 2 and len(served) == 1


def test_cli_owner_unreachable_gives_up(
    owner: Owner, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = owner.info()
    owner.handle.stop()
    monkeypatch.setattr(Scope, "open", _busy_open(info))
    assert cli.main([]) == cli.EXIT_BUSY
    err = capsys.readouterr().err
    assert "does not answer" in err and info.mcp_url is not None and info.mcp_url in err


# -- stdio end-to-end (subprocess) ---------------------------------------------------------

# Runs edu09-mcp in a subprocess with Scope.open patched: a "hardware" open takes the real
# OwnerLock (from EDU09_RUNTIME_DIR) and then uses a simulator instead of HID. So DeviceBusy and
# owner.json are genuine, and the real device is never touched.
_FAKE_HW = """
import sys
from edu09.device.lock import OwnerLock
from edu09.device.scope import Scope
from edu09.transport.sim import SimTransport

_open = Scope.__dict__["open"].__func__

def fake_open(cls, transport=None, **kw):
    if transport is None:
        lock = OwnerLock(kw.get("owner_kind", "other"))
        lock.acquire()
        lock.release()
        transport = SimTransport.virtual(seed=1)
    kw.pop("owner_kind", None)
    kw.pop("mcp_url", None)
    return _open(cls, transport, **kw)

Scope.open = classmethod(fake_open)
from edu09.mcp_server.__main__ import main
sys.exit(main(sys.argv[1:]))
"""


def _stdio_params(lock_dir: Path, *args: str) -> StdioServerParameters:
    env = dict(os.environ)
    env["EDU09_RUNTIME_DIR"] = str(lock_dir)
    return StdioServerParameters(
        command=sys.executable, args=["-I", "-c", _FAKE_HW, *args], env=env
    )


@pytest.mark.slow
def test_stdio_bridge_end_to_end(owner: Owner, lock_dir: Path) -> None:
    async def fn(c: Client) -> tuple[list[str], Any, Any, Any]:
        names = [t.name for t in (await c.list_tools()).tools]
        st = await c.call_tool("get_status", {})
        acq = await c.call_tool("acquire", {"max_points": 32})
        owner.handle.stop()
        gone = await c.call_tool("get_status", {})
        return names, st, acq, gone

    names, st, acq, gone = run_client(_stdio_params(lock_dir), fn)
    assert "acquire" in names
    status = _payload(st)
    assert status["owner"]["kind"] == "gui"  # served by the owner, not by the subprocess
    wid = _payload(acq)["waveform_id"]
    assert [w.waveform_id for w in owner.scope.store.snapshot()] == [wid]
    assert gone.is_error and text_of(gone).startswith(f"{OWNER_GONE}:")


@pytest.mark.slow
def test_stdio_falls_back_to_direct_when_free(lock_dir: Path) -> None:
    """No owner: the same fake-hardware subprocess opens the device itself."""

    async def fn(c: Client) -> Any:
        return await c.call_tool("get_status", {})

    st = _payload(run_client(_stdio_params(lock_dir), fn))
    assert st["connected"] is True and st["owner"]["kind"] == "mcp"
