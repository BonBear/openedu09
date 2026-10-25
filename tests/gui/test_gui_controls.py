"""Controls -> scope.configure, and external (MCP) settings changes -> controls without loops."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from edu09.gui.controls import ControlPanel
from edu09.model import Coupling, Settings, Timebase, TriggerEdge, TriggerMode, VoltsPerDiv

pytestmark = pytest.mark.gui

WAIT_MS = 10_000


def pick(box: Any, data: Any) -> None:
    """Select like a user would (``activated`` is only emitted for user choices)."""
    idx = box.findData(data.value)
    assert idx >= 0
    box.setCurrentIndex(idx)
    box.activated.emit(idx)


def test_set_settings_does_not_emit(qtbot: Any) -> None:
    panel = ControlPanel()
    qtbot.addWidget(panel)
    s = Settings().replace(
        volts_per_div=0.5,
        coupling="ac",
        position_div=1.5,
        timebase="50us",
        trigger_edge="falling",
        trigger_level_v=0.3,
    )
    with qtbot.assertNotEmitted(panel.settingsRequested):
        panel.set_settings(s)
    assert panel.vdiv.currentData() == VoltsPerDiv.V0_5.value
    assert panel.coupling.currentData() == Coupling.AC.value
    assert panel.position.value() == pytest.approx(1.5)
    assert panel.timebase.currentData() == Timebase.US50.value
    assert panel.trigger_edge.currentData() == TriggerEdge.FALLING.value
    assert panel.trigger_level.value() == pytest.approx(0.3)
    lo, hi = s.level_range_v()
    assert panel.trigger_level.minimum() == pytest.approx(lo)
    assert panel.trigger_level.maximum() == pytest.approx(hi)
    # ETS timebase: free run is not selectable
    model = panel.trigger_mode.model()
    assert not model.item(panel.trigger_mode.findData(TriggerMode.NONE.value)).isEnabled()


def test_user_edits_emit_flat_partials(qtbot: Any) -> None:
    panel = ControlPanel()
    qtbot.addWidget(panel)
    got: list[dict[str, Any]] = []
    panel.settingsRequested.connect(got.append)
    pick(panel.vdiv, VoltsPerDiv.V0_5)
    pick(panel.coupling, Coupling.GND)
    pick(panel.timebase, Timebase.US500)
    pick(panel.trigger_mode, TriggerMode.NORMAL)
    pick(panel.trigger_edge, TriggerEdge.FALLING)
    panel.position.setValue(-1.25)
    panel.trigger_level.setValue(1.0)
    panel.request_level(1e6)  # clamped to the screen window
    assert got == [
        {"volts_per_div": 0.5},
        {"coupling": "gnd"},
        {"timebase": 500e-6},
        {"trigger_mode": "normal"},
        {"trigger_edge": "falling"},
        {"position_div": -1.25},
        {"trigger_level_v": 1.0},
        {"trigger_level_v": Settings().level_range_v()[1]},
    ]


def test_disconnected_and_running_states(qtbot: Any) -> None:
    panel = ControlPanel()
    qtbot.addWidget(panel)
    panel.set_running(True)
    assert not panel.run_button.isEnabled() and panel.stop_button.isEnabled()
    panel.set_connected(False)
    assert not panel.vdiv.isEnabled()
    assert not panel.run_button.isEnabled() and not panel.single_button.isEnabled()
    assert not panel.stop_button.isEnabled()
    panel.set_connected(True)
    panel.set_running(False)
    assert panel.vdiv.isEnabled() and panel.run_button.isEnabled()
    assert not panel.stop_button.isEnabled()
    panel.set_busy(True)
    assert not panel.autoset_button.isEnabled() and not panel.timebase.isEnabled()


def test_controls_configure_the_scope(qtbot: Any, window: Any) -> None:
    scope = window.scope
    c = window.controls
    pick(c.vdiv, VoltsPerDiv.V0_5)
    qtbot.waitUntil(lambda: scope.settings.ch1.volts_per_div is VoltsPerDiv.V0_5, timeout=WAIT_MS)
    pick(c.timebase, Timebase.MS1)
    qtbot.waitUntil(lambda: scope.settings.timebase is Timebase.MS1, timeout=WAIT_MS)
    c.trigger_level.setValue(0.5)
    qtbot.waitUntil(lambda: scope.settings.trigger.level_v == pytest.approx(0.5), timeout=WAIT_MS)
    # the plot follows the applied settings (graticule/level marker)
    qtbot.waitUntil(lambda: window.plot.settings.timebase is Timebase.MS1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: window.plot.level_line.value() == pytest.approx(0.5), timeout=WAIT_MS)


def test_plot_level_drag_configures_trigger_level(qtbot: Any, window: Any) -> None:
    window.plot.levelDragged.emit(1.25)
    qtbot.waitUntil(
        lambda: window.scope.settings.trigger.level_v == pytest.approx(1.25), timeout=WAIT_MS
    )


def test_external_mcp_change_updates_controls_without_feedback(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[dict[str, Any]] = []
    window.controls.settingsRequested.connect(requested.append)
    gui_configures: list[dict[str, Any]] = []
    orig = window.adapter.configure

    def spy(**kw: Any) -> Any:
        gui_configures.append(kw)
        return orig(**kw)

    monkeypatch.setattr(window.adapter, "configure", spy)
    scope = window.scope
    t = threading.Thread(
        target=lambda: scope.configure(
            source="mcp", timebase="50us", volts_per_div=1.0, trigger_level_v=0.75
        )
    )
    t.start()
    t.join(5)
    qtbot.waitUntil(
        lambda: window.controls.timebase.currentData() == Timebase.US50.value, timeout=WAIT_MS
    )
    assert window.controls.vdiv.currentData() == VoltsPerDiv.V1.value
    assert window.controls.trigger_level.value() == pytest.approx(0.75)
    qtbot.waitUntil(lambda: "MCP" in window.statusBar().currentMessage(), timeout=WAIT_MS)
    assert "Settings changed by MCP" in window.log_view.toPlainText()
    # barrier: everything the GUI might have queued on its command thread has run
    done: list[bool] = []
    window.adapter.call(lambda: None, on_result=lambda _r: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=WAIT_MS)
    assert requested == [] and gui_configures == []
    assert scope.settings.timebase is Timebase.US50


def test_invalid_request_resyncs_controls(qtbot: Any, window: Any) -> None:
    # 50 us/div is ETS: free run is coerced (warning), not an error; force a real error instead
    window.controls.settingsRequested.emit({"trigger_level_v": 1e6})
    qtbot.waitUntil(lambda: "configure failed" in window.log_view.toPlainText(), timeout=WAIT_MS)
    assert window.controls.trigger_level.value() == pytest.approx(
        window.scope.settings.trigger.level_v
    )
