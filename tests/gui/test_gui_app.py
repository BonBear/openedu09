"""edu09-gui entry point: argument parsing, simulator launch, DeviceBusy handling."""

from __future__ import annotations

import signal
from typing import Any

import pytest

from edu09.device.lock import OwnerInfo
from edu09.errors import DeviceBusy
from edu09.gui import app

pytestmark = pytest.mark.gui


def test_parser_and_transport() -> None:
    args = app.build_parser().parse_args(
        ["--simulate", "square:1khz:4vpp", "--mcp-http", "--port", "9000"]
    )
    assert args.simulate == "square:1khz:4vpp" and args.mcp_http and args.port == 9000
    assert app.make_transport(None) is None
    t = app.make_transport("square:1khz:4vpp")
    assert t.info.kind == "sim"
    with pytest.raises(ValueError):
        app.make_transport("bogus:1khz")


def test_console_script_points_at_main() -> None:
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["edu09-gui"] == "edu09.gui.app:main"


def test_main_launches_simulator_and_exits_cleanly(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    rc = app.main(["--simulate", "sine:1khz:2vpp", "--quit-after", "0.4"])
    assert rc == 0


def test_main_reports_device_busy(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = OwnerInfo(pid=4242, kind="mcp", mcp_url="http://127.0.0.1:8765/mcp")
    shown: list[str] = []

    def busy(*_a: Any, **_k: Any) -> Any:
        raise DeviceBusy("device busy", owner=owner)

    monkeypatch.setattr(app.Scope, "open", busy)
    monkeypatch.setattr(app.QMessageBox, "critical", lambda _p, _t, text: shown.append(text))
    assert app.main(["--no-run"]) == 2
    assert "4242" in shown[0] and "mcp" in shown[0]
    assert shown[0].find(owner.mcp_url) >= 0  # find(), not `in`: CodeQL URL-substring rule


def test_describe_busy_without_owner() -> None:
    assert "another program" in app.describe_busy(DeviceBusy("locked"))
