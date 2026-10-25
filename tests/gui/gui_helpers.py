"""Shared helpers for the GUI tests (fake MCP server start, window factory)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from edu09.device.scope import Scope

WAIT_MS = 10_000


@dataclass
class FakeHandle:
    url: str
    stopped: int = 0

    def stop(self) -> None:
        self.stopped += 1


@dataclass
class FakeMcp:
    """Replaces ``edu09.mcp_server.server.start_http_in_thread`` (no socket is opened)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    handles: list[FakeHandle] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    #: if set, ``start_http_in_thread`` blocks until it is set (bounded) before returning
    start_gate: Any = None
    #: if set, ``start_http_in_thread`` raises it (after recording the call)
    error: BaseException | None = None

    def start_http_in_thread(
        self, scope: Any, opts: Any, host: str = "127.0.0.1", port: int = 8765
    ) -> FakeHandle:
        import threading

        self.threads.append(threading.current_thread().name)
        if self.start_gate is not None:
            self.start_gate.wait(WAIT_MS / 1000)
        self.calls.append({"scope": scope, "opts": opts, "host": host, "port": port})
        if self.error is not None:
            raise self.error
        h = FakeHandle(f"http://{host}:{port}/mcp")
        self.handles.append(h)
        return h

    def install(self, monkeypatch: Any) -> FakeMcp:
        from edu09.mcp_server import server

        monkeypatch.setattr(server, "start_http_in_thread", self.start_http_in_thread)
        return self


def make_window(qtbot: Any, scope: Scope, **kw: Any) -> Any:
    from edu09.gui.main_window import MainWindow
    from edu09.gui.qt_bridge import ScopeQtAdapter

    adapter = ScopeQtAdapter(scope)
    win = MainWindow(adapter, **kw)
    qtbot.addWidget(win)
    return win
