"""GUI test fixtures: offscreen Qt, simulator-backed Scope, adapter and main window.

Never opens the real device: every Scope here uses a ``SimTransport``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_MISSING = [m for m in ("PySide6", "pyqtgraph", "pytestqt") if importlib.util.find_spec(m) is None]
if _MISSING:  # the [gui] extra / pytest-qt is not installed: skip the whole directory
    collect_ignore_glob = ["test_*.py"]

from gui_helpers import FakeMcp, make_window  # noqa: E402

from edu09.device.scope import Scope  # noqa: E402
from edu09.transport.sim import SimTransport  # noqa: E402

WAIT_MS = 10_000


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> FakeMcp:
    fake = FakeMcp()
    monkeypatch.setitem(sys.modules, "edu09.mcp_server.server", fake.module())
    return fake


def _open(transport: SimTransport) -> Scope:
    return Scope.open(transport, reconnect_interval_s=0.01)


@pytest.fixture
def vscope() -> Iterator[Scope]:
    """Scope on a deterministic virtual-clock simulator (1 kHz 2 Vpp sine)."""
    scope = _open(SimTransport.virtual(seed=0))
    try:
        yield scope
    finally:
        scope.close()


@pytest.fixture
def rtscope() -> Iterator[Scope]:
    """Scope on the real-time simulator (as ``edu09-gui --simulate``), fast timebase."""
    scope = _open(SimTransport("square:1khz:4vpp", seed=0))
    scope.configure(timebase="200us", volts_per_div=1.0)
    try:
        yield scope
    finally:
        scope.close()


@pytest.fixture
def window(qtbot: Any, vscope: Scope) -> Iterator[Any]:
    win = make_window(qtbot, vscope)
    try:
        yield win
    finally:
        win.shutdown()


@pytest.fixture
def rtwindow(qtbot: Any, rtscope: Scope) -> Iterator[Any]:
    win = make_window(qtbot, rtscope)
    try:
        yield win
    finally:
        win.shutdown()
