"""GUI review additions: threading, real MCP server in the GUI, drag mapping, shutdown."""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest
from gui_helpers import make_window

from edu09.device.scope import Scope
from edu09.model import Settings

pytestmark = pytest.mark.gui

WAIT_MS = 10_000


def _drain(qtbot: Any, win: Any) -> None:
    done: list[bool] = []
    win.adapter.call(lambda: None, on_result=lambda _r: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=WAIT_MS)


def test_no_blocking_scope_call_on_gui_thread(
    qtbot: Any, vscope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every blocking Scope entry point driven from the widgets runs off the GUI thread."""
    main = threading.current_thread()
    seen: dict[str, threading.Thread] = {}

    def wrap(name: str) -> None:
        orig = getattr(vscope, name)

        def spy(*a: Any, **kw: Any) -> Any:
            seen[name] = threading.current_thread()
            return orig(*a, **kw)

        monkeypatch.setattr(vscope, name, spy)

    for n in ("configure", "set_settings", "run", "stop", "acquire"):
        wrap(n)
    win = make_window(qtbot, vscope)
    try:
        win.controls.vdiv.setCurrentIndex(1)
        win.controls.vdiv.activated.emit(1)
        win.controls.position.setValue(1.0)
        win.controls.runRequested.emit()
        win.controls.stopRequested.emit()
        win.auto_set()  # background job: set_settings + acquire on the adapter's pool
        qtbot.waitUntil(lambda: not win._busy, timeout=WAIT_MS * 3)
        _drain(qtbot, win)
        assert {"configure", "run", "stop", "set_settings", "acquire"} <= set(seen)
        for name, thread in seen.items():
            assert thread is not main, name
    finally:
        win.shutdown()


def test_drag_maps_volts_including_position(qtbot: Any, vscope: Scope) -> None:
    """The plot's y axis is volts: a dragged level is the trigger level (V), also with position."""
    vscope.configure(volts_per_div=1.0, position_div=1.0, trigger_mode="normal")
    win = make_window(qtbot, vscope)
    try:
        lo, hi = vscope.settings.level_range_v()
        assert (lo, hi) == (-5.0, 3.0)
        win.plot.level_line.setValue(1.0)
        win.plot.levelDragged.emit(1.0)
        _drain(qtbot, win)
        assert vscope.settings.trigger.level_v == pytest.approx(1.0, abs=0.05)
        win.plot.levelDragged.emit(100.0)  # off screen: clamped, never an error
        _drain(qtbot, win)
        assert vscope.settings.trigger.level_v <= hi + 1e-9
    finally:
        win.shutdown()


def test_set_settings_does_not_echo_requests(qtbot: Any, window: Any) -> None:
    requested: list[dict[str, Any]] = []
    window.controls.settingsRequested.connect(requested.append)
    s: Settings = window.scope.settings.replace(timebase="1ms", trigger_level_v=0.5)
    window.controls.set_settings(s)
    window.plot.set_settings(s)
    assert requested == []


def test_real_mcp_server_in_gui_reflects_agent_changes(qtbot: Any, vscope: Scope) -> None:
    """GUI + real in-process MCP HTTP server: an agent's configure shows up in the controls."""
    import anyio
    from mcp import Client

    win = make_window(qtbot, vscope, mcp_port=0)
    try:
        win.set_mcp_enabled(True)
        qtbot.waitUntil(lambda: win.mcp_url is not None, timeout=WAIT_MS)
        url = win.mcp_url
        port = win._mcp_handle.port

        async def agent() -> str:
            async with Client(url) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert "calibrate" not in names  # the GUI never exposes calibration over MCP
                res = await c.call_tool("configure", {"volts_per_div": 0.5, "timebase": "5ms"})
                return str(res.is_error)

        done: list[str] = []
        t = threading.Thread(target=lambda: done.append(anyio.run(agent)))
        t.start()
        qtbot.waitUntil(lambda: bool(done), timeout=WAIT_MS)
        t.join()
        assert done == ["False"]
        qtbot.waitUntil(lambda: win.controls.vdiv.currentData() == 0.5, timeout=WAIT_MS)
        assert win.controls.timebase.currentData() == pytest.approx(0.005)
        assert "changed by MCP" in win.statusBar().currentMessage()
        win.close()
        assert not win._mcp_live
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    finally:
        win.shutdown()
