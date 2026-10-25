"""Shared helpers for the GUI tests (fakes for the T10 MCP server contract, window factory)."""

from __future__ import annotations

import types
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
    """Stands in for ``edu09.mcp_server.server`` (T10 contract)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    handles: list[FakeHandle] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    #: if set, ``start_http_in_thread`` blocks until it is set (bounded) before returning
    start_gate: Any = None

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("edu09.mcp_server.server")

        @dataclass
        class ServerOptions:
            allow_calibration: bool = False
            owner_kind: str = "mcp"

        def start_http_in_thread(
            scope: Any, opts: Any, host: str = "127.0.0.1", port: int = 8765
        ) -> FakeHandle:
            import threading

            self.threads.append(threading.current_thread().name)
            if self.start_gate is not None:
                self.start_gate.wait(WAIT_MS / 1000)
            self.calls.append({"scope": scope, "opts": opts, "host": host, "port": port})
            h = FakeHandle(f"http://{host}:{port}/mcp")
            self.handles.append(h)
            return h

        mod.ServerOptions = ServerOptions  # type: ignore[attr-defined]
        mod.start_http_in_thread = start_http_in_thread  # type: ignore[attr-defined]
        return mod


def make_window(qtbot: Any, scope: Scope, **kw: Any) -> Any:
    from edu09.gui.main_window import MainWindow
    from edu09.gui.qt_bridge import ScopeQtAdapter

    adapter = ScopeQtAdapter(scope)
    win = MainWindow(adapter, **kw)
    qtbot.addWidget(win)
    return win
