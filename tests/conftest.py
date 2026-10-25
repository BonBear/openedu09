"""Shared pytest configuration: --hardware option, marker handling, user-dir isolation, sim
scope fixture."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from typing import Any

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help="run tests marked 'hardware' (needs a real EDU09 attached)",
    )


def _hardware_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--hardware")) or os.environ.get("EDU09_HW") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_hw = pytest.mark.skip(reason="hardware test: pass --hardware or set EDU09_HW=1")
    skip_gui = pytest.mark.skip(reason="PySide6 is not installed")
    have_qt = importlib.util.find_spec("PySide6") is not None
    hw_on = _hardware_enabled(config)
    for item in items:
        if "hardware" in item.keywords and not hw_on:
            item.add_marker(skip_hw)
        if "gui" in item.keywords and not have_qt:
            item.add_marker(skip_gui)


@pytest.fixture(autouse=True)
def _isolate_user_dirs(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety net: no test may write the real user config/data/cache dirs (calibration.json,
    exports) or use the real runtime dir (device.lock). Hardware tests keep the real runtime
    dir so they still see a GUI/MCP process that owns the device."""
    import platformdirs

    base = tmp_path_factory.getbasetemp() / "user-dirs"
    for name in ("user_config_dir", "user_data_dir", "user_cache_dir", "user_runtime_dir"):
        if name == "user_runtime_dir" and request.node.get_closest_marker("hardware"):
            continue

        def fake(appname: str | None = None, *a: Any, _n: str = name, **k: Any) -> str:
            return str(base / _n / (appname or ""))

        monkeypatch.setattr(platformdirs, name, fake)
    if not request.node.get_closest_marker("hardware"):
        monkeypatch.delenv("EDU09_RUNTIME_DIR", raising=False)


@pytest.fixture
def sim_scope() -> Iterator[Any]:
    """A connected ``Scope`` on a deterministic ``SimTransport.virtual(seed=0)`` (1 kHz, 2 Vpp
    sine). Deadlines run on the simulator's VirtualClock; closed after the test."""
    from edu09.device.scope import Scope
    from edu09.transport.sim import SimTransport

    scope = Scope.open(SimTransport.virtual(seed=0), reconnect_interval_s=0.01)
    try:
        yield scope
    finally:
        scope.close()
