"""Ctrl+wheel vertical zoom (and plain-wheel horizontal zoom) of the scope plot and spectrum."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from edu09.gui.plot import ScopePlot
from edu09.gui.spectrum import SpectrumPanel
from edu09.model import Settings

pytestmark = pytest.mark.gui

NO = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


def _shown(qtbot: Any, w: Any) -> Any:
    qtbot.addWidget(w)
    w.resize(800, 500)
    w.show()
    qtbot.waitExposed(w)
    return w


def _wheel(widget: Any, mods: Any, delta: int = 120, at: QPointF | None = None) -> None:
    vp = widget.viewport()
    pos = at if at is not None else QPointF(vp.width() / 2, vp.height() / 2)
    ev = QWheelEvent(
        pos,
        vp.mapToGlobal(pos),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        mods,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(vp, ev)


def _plot(qtbot: Any, **kw: Any) -> ScopePlot:
    return _shown(qtbot, ScopePlot(Settings().replace(volts_per_div=1.0, **kw)))


def test_plain_wheel_zooms_x_only(qtbot: Any) -> None:
    plot = _plot(qtbot)
    y0, x0 = plot.y_range(), plot.x_range()
    _wheel(plot.widget, NO)
    x1 = plot.x_range()
    assert x1[1] - x1[0] < x0[1] - x0[0]
    assert plot.y_range() == pytest.approx(y0)
    assert plot.y_zoom == pytest.approx(1.0)


def test_ctrl_wheel_zooms_y_only_around_pointer(qtbot: Any) -> None:
    plot = _plot(qtbot)
    x0 = plot.x_range()
    rect = plot.view_box.sceneBoundingRect()
    top = QPointF(plot.widget.mapFromScene(rect.center().x(), rect.top() + 0.5))
    _wheel(plot.widget, CTRL, 120, top)
    lo, hi = plot.y_range()
    assert plot.x_range() == pytest.approx(x0)
    assert plot.y_zoom > 1.2
    assert hi == pytest.approx(4.0, abs=0.2) and lo > -4.0  # top edge stays under the pointer
    assert "Y zoom" in plot.info_label.text()
    assert plot.settings.ch1.volts_per_div == 1.0  # display only


def test_ctrl_wheel_limits(qtbot: Any) -> None:
    plot = _plot(qtbot)
    for _ in range(60):
        _wheel(plot.widget, CTRL, 120)
    lo, hi = plot.y_range()
    assert hi - lo == pytest.approx(0.5, rel=1e-3)  # 0.5 div at 1 V/div
    for _ in range(100):
        _wheel(plot.widget, CTRL, -120)
    assert plot.y_range() == pytest.approx((-4.0, 4.0))  # never beyond the 8-div screen
    assert plot.y_zoom == pytest.approx(1.0)


def test_shift_wheel_pans_y(qtbot: Any) -> None:
    plot = _plot(qtbot)
    _wheel(plot.widget, CTRL, 240)
    lo, hi = plot.y_range()
    _wheel(plot.widget, SHIFT, -120)
    lo2, hi2 = plot.y_range()
    assert hi2 - lo2 == pytest.approx(hi - lo)
    assert lo2 < lo


def test_settings_change_resets_y_zoom_but_level_change_does_not(qtbot: Any) -> None:
    plot = _plot(qtbot)
    _wheel(plot.widget, CTRL, 240)
    zoomed = plot.y_range()
    plot.set_settings(plot.settings.replace(trigger_level_v=0.5))
    assert plot.y_range() == pytest.approx(zoomed)
    plot.set_settings(plot.settings.replace(volts_per_div=2.0))
    assert plot.y_range() == pytest.approx((-8.0, 8.0))
    _wheel(plot.widget, CTRL, 240)
    assert plot.y_zoom > 1
    plot.set_settings(plot.settings.replace(position_div=1.0))
    assert plot.y_range() == pytest.approx((-10.0, 6.0))
    assert plot.y_zoom == pytest.approx(1.0)


def test_reset_view_resets_both_axes(qtbot: Any) -> None:
    plot = _plot(qtbot)
    x0 = plot.x_range()
    _wheel(plot.widget, NO)
    _wheel(plot.widget, CTRL)
    assert plot.y_zoom > 1 and plot.x_range() != pytest.approx(x0)
    plot.reset_view()
    assert plot.y_zoom == pytest.approx(1.0)
    assert plot.x_range() == pytest.approx(x0)
    assert "Y zoom" not in plot.info_label.text()


def test_trigger_drag_and_cursors_work_when_zoomed(qtbot: Any) -> None:
    plot = _plot(qtbot, trigger_level_v=1.0)
    _wheel(plot.widget, CTRL, 240)
    lo, hi = plot.y_range()
    vb = plot.view_box
    # the trigger line sits at its data value; scene mapping follows the zoomed view
    y_scene = vb.mapViewToScene(QPointF(0.0, 1.0)).y()
    assert vb.mapSceneToView(QPointF(0.0, y_scene)).y() == pytest.approx(1.0)
    assert plot.level_line.value() == pytest.approx(1.0)
    got: list[float] = []
    plot.levelDragged.connect(got.append)
    plot.level_line.setValue(1.5)
    plot.level_line.sigPositionChangeFinished.emit(plot.level_line)
    assert got == [pytest.approx(1.5)]
    plot.set_cursor_positions(0.0, 1e-3, 0.5, 1.5)
    assert plot.cursor_readout().dv == pytest.approx(1.0)
    assert plot.level_line.bounds() == pytest.approx([-4.0, 4.0])


def test_spectrum_wheel_x_and_ctrl_wheel_y(qtbot: Any) -> None:
    panel = _shown(qtbot, SpectrumPanel())
    panel.autoscale_check.setChecked(False)
    panel.autoscale_check.setChecked(True)
    x0 = panel.item.viewRange()[0]
    y0 = panel.item.viewRange()[1]
    _wheel(panel.widget, NO)
    x1 = panel.item.viewRange()[0]
    assert x1[1] - x1[0] < x0[1] - x0[0]
    assert panel.item.viewRange()[1] == pytest.approx(y0)
    assert panel.autoscale_check.isChecked()
    _wheel(panel.widget, CTRL)
    y1 = panel.item.viewRange()[1]
    assert y1[1] - y1[0] < y0[1] - y0[0]
    assert panel.item.viewRange()[0] == pytest.approx(x1)
    assert not panel.autoscale_check.isChecked()
    panel.autoscale_check.setChecked(True)
    panel.reset_view()
    assert panel.autoscale_check.isChecked()
