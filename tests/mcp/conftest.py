"""Fixtures for the MCP server tests: a sim-backed Scope and a server built on it.

Never opens the real device: every Scope here runs on ``SimTransport.virtual``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from edu09.device.scope import Scope
from edu09.mcp_server import bridge
from edu09.mcp_server.server import DEFAULT_PORT, FastMCP, ServerOptions, build_server
from edu09.transport.sim import SimTransport


@pytest.fixture
def mcp_scope() -> Iterator[Scope]:
    scope = Scope.open(SimTransport.virtual(seed=0), reconnect_interval_s=0.01)
    try:
        yield scope
    finally:
        scope.close()


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    d = tmp_path / "exports"
    d.mkdir()
    return d


@pytest.fixture
def server(mcp_scope: Scope, export_dir: Path) -> FastMCP:
    return build_server(mcp_scope, ServerOptions(export_dir=export_dir))


@pytest.fixture(autouse=True)
def _never_reach_the_real_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: the developer's GUI may be serving MCP on the default port and own the real
    device. No test may connect the bridge to it (a CLI test that hit the real
    http://127.0.0.1:8765/mcp passed or failed depending on whether the GUI was running).
    Tests use port=0 servers."""
    real = bridge._client

    def guarded(url: str) -> Any:
        if urlsplit(url).port == DEFAULT_PORT:
            pytest.fail(f"test tried to connect the bridge to the default MCP port: {url}")
        return real(url)

    monkeypatch.setattr(bridge, "_client", guarded)
