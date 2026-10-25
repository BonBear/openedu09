"""Main window against the simulator: run/stop/single, plot, connection state, MCP, export."""

from __future__ import annotations

import re
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from gui_helpers import FakeMcp, make_window

from edu09.device.scope import Scope
from edu09.model import Settings, Timebase, VoltsPerDiv
from edu09.transport.sim import SimTransport

pytestmark = pytest.mark.gui

WAIT_MS = 10_000


def drain(qtbot: Any, win: Any) -> None:
    """Wait until every command queued on the adapter's command thread has run."""
    done: list[bool] = []
    win.adapter.call(lambda: None, on_result=lambda _r: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=WAIT_MS)


# -- acquisition ------------------------------------------------------------------------------


def test_run_stop_single_realtime_sim(qtbot: Any, rtwindow: Any) -> None:
    win, scope = rtwindow, rtwindow.scope
    c = win.controls
    assert c.run_button.isEnabled() and not c.stop_button.isEnabled()
    qtbot.mouseClick(c.run_button, qtbot_button())
    qtbot.waitUntil(lambda: scope.running and not c.run_button.isEnabled(), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: win.plot.redraws >= 3, timeout=WAIT_MS)
    assert "running" in win.state_label.text()
    qtbot.mouseClick(c.stop_button, qtbot_button())
    qtbot.waitUntil(lambda: not scope.running and c.run_button.isEnabled(), timeout=WAIT_MS)
    before = win.plot.waveform.seq
    qtbot.mouseClick(c.single_button, qtbot_button())
    qtbot.waitUntil(
        lambda: win.plot.waveform is not None and win.plot.waveform.seq > before, timeout=WAIT_MS
    )
    assert not scope.running
    wf = win.plot.waveform
    x, y = win.plot.curve.getData()
    assert len(x) == len(y) == 1024
    assert x[wf.trigger_index] == pytest.approx(0.0)
    # measurements of the 1 kHz 4 Vpp square
    qtbot.waitUntil(lambda: win.measurements.values["frequency"].text() != "—", timeout=WAIT_MS)
    freq = win.measurements.values["frequency"].text()
    assert re.fullmatch(r"(99\d(\.\d+)? Hz|1(\.\d+)? kHz)", freq), freq
    assert win.measurements.values["vpp"].text().endswith(" V")


def qtbot_button() -> Any:
    from PySide6.QtCore import Qt

    return Qt.MouseButton.LeftButton


def test_render_is_throttled(qtbot: Any, vscope: Scope) -> None:
    win = make_window(qtbot, vscope, render_interval_ms=200)
    try:
        win.run()
        qtbot.waitUntil(lambda: win.adapter.waveforms_received >= 40, timeout=WAIT_MS)
        win.stop()
        qtbot.waitUntil(lambda: not vscope.running, timeout=WAIT_MS)
        assert 1 <= win.plot.redraws < win.adapter.waveforms_received / 2
    finally:
        win.shutdown()


def test_clipped_indicator(qtbot: Any) -> None:
    scope = Scope.open(SimTransport.virtual("square:1khz:20vpp", seed=0))
    try:
        scope.configure(volts_per_div=0.5, timebase="1ms")
        win = make_window(qtbot, scope)
        with qtbot.waitSignal(win.rendered, timeout=WAIT_MS):
            win.single()
        assert win.plot.waveform.clipped
        assert win.plot.clipped_badge.isVisibleTo(win.plot)
        win.shutdown()
    finally:
        scope.close()


def test_single_timeout_is_reported(qtbot: Any) -> None:
    sim = SimTransport.virtual("sine:1khz:2vpp", seed=0)
    sim.device.never_trigger = True
    scope = Scope.open(sim)
    try:
        scope.configure(trigger_mode="normal", timebase="200us")
        win = make_window(qtbot, scope)
        win.adapter.single(0.2, on_error=lambda e: win.notify(f"Single failed: {e!r}"))
        qtbot.waitUntil(lambda: "Single failed" in win.log_view.toPlainText(), timeout=WAIT_MS)
        win.shutdown()
    finally:
        scope.close()


# -- connection state -------------------------------------------------------------------------


def test_disconnect_greys_out_and_reconnect_restores(qtbot: Any, window: Any) -> None:
    sim = window.scope._transport
    window.run()
    qtbot.waitUntil(lambda: window.scope.running, timeout=WAIT_MS)
    sim.unplug()
    qtbot.waitUntil(lambda: not window.controls.vdiv.isEnabled(), timeout=WAIT_MS)
    assert "Disconnected" in window.conn_label.text()
    assert "reconnecting" in window.conn_label.text()
    assert not window.controls.single_button.isEnabled()
    assert not window.run_action.isEnabled()
    sim.replug()
    qtbot.waitUntil(lambda: window.controls.vdiv.isEnabled(), timeout=WAIT_MS)
    assert "Connected" in window.conn_label.text()
    assert "FW 1.01" in window.fw_label.text()
    log = window.log_view.toPlainText()
    assert "Device disconnected" in log and "Device connected" in log


# -- cursors / plot geometry --------------------------------------------------------------------


def test_cursor_readout_in_panel(qtbot: Any, window: Any) -> None:
    assert not window.measurements.cursor_group.isVisibleTo(window)
    window.cursors_action.setChecked(True)
    assert window.measurements.cursor_group.isVisibleTo(window)
    window.plot.set_cursor_positions(-1e-3, 1.5e-3, -0.5, 1.5)
    vals = window.measurements.cursor_values
    assert vals["dt"].text() == "2.5 ms"
    assert vals["freq"].text() == "400 Hz"
    assert vals["dv"].text() == "2 V"
    assert vals["t1"].text() == "-1 ms"
    window.cursors_action.setChecked(False)
    assert not window.measurements.cursor_group.isVisibleTo(window)


def test_graticule_follows_settings(qtbot: Any, window: Any) -> None:
    window.scope.configure(volts_per_div=0.5, position_div=1.0, timebase="100us")
    qtbot.waitUntil(lambda: window.plot.settings.timebase is Timebase.US100, timeout=WAIT_MS)
    lo, hi = window.plot.item.viewRange()[1]
    assert (lo, hi) == pytest.approx((-2.5, 1.5))
    x0, x1 = window.plot.x_range()
    span200 = 1023 / 156_250
    assert x1 - x0 == pytest.approx(span200 / 2, rel=0.02)  # 2x display zoom of the frame


# -- MCP toggle -------------------------------------------------------------------------------


def test_mcp_toggle_starts_and_stops_server(qtbot: Any, fake_mcp: FakeMcp, vscope: Scope) -> None:
    win = make_window(qtbot, vscope, mcp_port=9123)
    try:
        assert win.mcp_action.isEnabled()
        win.mcp_action.setChecked(True)
        qtbot.waitUntil(lambda: win.mcp_url is not None, timeout=WAIT_MS)
        assert win.mcp_url == "http://127.0.0.1:9123/mcp"
        assert "MCP: http://127.0.0.1:9123/mcp" in win.owner_label.text()
        call = fake_mcp.calls[0]
        assert call["scope"] is vscope and call["host"] == "127.0.0.1" and call["port"] == 9123
        assert call["opts"].owner_kind == "gui"
        assert fake_mcp.threads[0] != threading.main_thread().name
        handle = fake_mcp.handles[0]
        win.mcp_action.setChecked(False)
        qtbot.waitUntil(lambda: handle.stopped == 1, timeout=WAIT_MS)
        assert win.mcp_url is None and "MCP:" not in win.owner_label.text()
        # on again, then closing the window stops it
        win.set_mcp_enabled(True)
        qtbot.waitUntil(lambda: win.mcp_url is not None, timeout=WAIT_MS)
        win.close()
        assert fake_mcp.handles[1].stopped == 1
    finally:
        win.shutdown()


def test_mcp_import_failure_disables_toggle(
    qtbot: Any, fake_mcp: FakeMcp, vscope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = make_window(qtbot, vscope)
    try:
        monkeypatch.setitem(sys.modules, "edu09.mcp_server.server", None)  # import -> ImportError
        win.mcp_action.setChecked(True)
        qtbot.waitUntil(lambda: not win.mcp_action.isEnabled(), timeout=WAIT_MS)
        assert not win.mcp_action.isChecked()
        assert "unavailable" in win.mcp_action.toolTip()
    finally:
        win.shutdown()


def test_mcp_unavailable_at_startup(
    qtbot: Any, vscope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "edu09.mcp_server.server", None)
    win = make_window(qtbot, vscope)
    try:
        assert not win.mcp_action.isEnabled()
        assert win.mcp_action.toolTip()
    finally:
        win.shutdown()


# -- export -----------------------------------------------------------------------------------


def test_export_csv(
    qtbot: Any, window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert window.export_csv(tmp_path / "none.csv") is None  # nothing acquired yet
    with qtbot.waitSignal(window.rendered, timeout=WAIT_MS):
        window.single()
    out = window.export_csv(tmp_path / "a.csv")
    assert out == tmp_path / "a.csv"
    lines = out.read_text().splitlines()
    assert lines[0].startswith("# edu09 waveform export")
    assert "t_s,volts,raw" in lines
    assert len(lines) - lines.index("t_s,volts,raw") - 1 == 1024
    # via the (patched) file dialog; ".csv" is appended
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "b"), ""))
    )
    assert window.export_csv() == tmp_path / "b.csv"
    assert (tmp_path / "b.csv").exists()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    assert window.export_csv() is None


# -- auto-set / calibration (T9 contracts, faked) ----------------------------------------------


@dataclass
class FakeAutoSetResult:
    settings: Settings
    rationale: list[str]
    waveform: Any = None


def test_autoset_runs_in_background(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, float, str]] = []

    def auto_set(scope: Scope, timeout_s: float = 5.0) -> FakeAutoSetResult:
        calls.append((scope, timeout_s, threading.current_thread().name))
        s = scope.configure(volts_per_div=1.0, timebase="500us")
        return FakeAutoSetResult(s, ["picked 1 V/div: Vpp 2 V", "picked 500 us/div"])

    mod = types.ModuleType("edu09.device.autoset")
    mod.auto_set = auto_set  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edu09.device.autoset", mod)
    window.auto_set()
    assert not window.controls.autoset_button.isEnabled()  # busy
    qtbot.waitUntil(lambda: window.last_autoset is not None, timeout=WAIT_MS)
    assert calls[0][0] is window.scope and calls[0][1] == 5.0
    assert calls[0][2] != threading.main_thread().name
    assert "Auto-set: picked 1 V/div" in window.log_view.toPlainText()
    assert window.controls.autoset_button.isEnabled()
    qtbot.waitUntil(
        lambda: window.controls.timebase.currentData() == Timebase.US500.value, timeout=WAIT_MS
    )
    assert window.controls.vdiv.currentData() == VoltsPerDiv.V1.value


def test_calibrate_needs_confirmation(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(window.scope, "calibrate", lambda **kw: calls.append(kw) or object())
    monkeypatch.setattr(window, "confirm_calibration", lambda: False)
    assert window.calibrate() is False
    drain(qtbot, window)
    assert calls == []
    monkeypatch.setattr(window, "confirm_calibration", lambda: True)
    assert window.calibrate() is True
    qtbot.waitUntil(lambda: "Calibration done" in window.log_view.toPlainText(), timeout=WAIT_MS)
    assert calls == [{"confirm": True, "input_disconnected": True, "save": True}]


def test_calibration_dialog_text_asks_to_disconnect() -> None:
    from edu09.gui.main_window import CALIBRATION_TEXT

    assert "DISCONNECT" in CALIBRATION_TEXT and "input" in CALIBRATION_TEXT.lower()


def test_despike_option_applies_to_ets_frames_only(qtbot: Any, window: Any) -> None:
    import numpy as np

    window.scope.configure(timebase="50us", trigger_mode="auto")
    qtbot.waitUntil(lambda: window.plot.settings.timebase is Timebase.US50, timeout=WAIT_MS)
    with qtbot.waitSignal(window.rendered, timeout=WAIT_MS):
        window.single()
    wf = window.plot.waveform
    assert wf.settings.timebase.is_ets
    _, y = window.plot.curve.getData()
    assert np.array_equal(y, wf.volts)  # off by default
    with qtbot.waitSignal(window.rendered, timeout=WAIT_MS):
        window.despike_action.setChecked(True)
    _, y2 = window.plot.curve.getData()
    from edu09 import analysis

    assert np.array_equal(y2, analysis.despike(wf.volts))
