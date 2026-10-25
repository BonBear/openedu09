"""``edu09-gui --record FILE``: the transport is wrapped in a ``RecordingTransport`` (simulator
and a never-opened ``HidTransport`` only; the real device is never touched)."""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from edu09.gui import app
from edu09.transport.hid_transport import HidTransport
from edu09.transport.replay import RecordingTransport, load_records

pytestmark = pytest.mark.gui


def test_make_transport_record(tmp_path: Path) -> None:
    rec = tmp_path / "r.jsonl"
    hw = app.make_transport(None, str(rec))
    assert isinstance(hw, RecordingTransport) and isinstance(hw._inner, HidTransport)
    assert hw.info.kind == "hid"  # Scope.open still treats it as hardware
    sim = app.make_transport("sine:1khz:2vpp", str(rec))
    assert isinstance(sim, RecordingTransport) and sim.info.kind == "sim"
    assert app.make_transport(None) is None
    assert app.build_parser().parse_args(["--record", "x.jsonl"]).record == "x.jsonl"


def test_main_records_simulated_session(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    rec = tmp_path / "logs" / "gui.jsonl"
    rc = app.main(["--simulate", "sine:1khz:2vpp", "--record", str(rec), "--quit-after", "0.4"])
    assert rc == 0
    records = load_records(rec)
    assert {r.dir for r in records} == {"in", "out"}


def test_main_record_hardware_passes_wrapper_to_scope_open(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Any] = []

    def fake_open(transport: Any = None, **_kw: Any) -> Any:
        seen.append(transport)
        raise app.DeviceNotFound("no device (test)")

    monkeypatch.setattr(app.Scope, "open", fake_open)
    monkeypatch.setattr(
        app.QMessageBox, "warning", lambda *_a: app.QMessageBox.StandardButton.Cancel
    )
    assert app.main(["--no-run", "--record", str(tmp_path / "r.jsonl")]) == 2
    (t,) = seen
    assert isinstance(t, RecordingTransport) and t.info.kind == "hid"


def test_main_record_unwritable_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as ei:
        app.main(["--simulate", "sine:1khz:2vpp", "--record", str(tmp_path)])
    assert ei.value.code == 2
