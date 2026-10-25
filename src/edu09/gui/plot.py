"""Waveform display: pyqtgraph plot with graticule, trigger markers, cursors and measurements.

Screen geometry: 8 vertical divisions (the 8-bit ADC span, ``model.SCREEN_DIVISIONS``) with one
graticule line per V/div, anchored to the screen (not to 0 V) so a fractional position still
shows 8 whole divisions. The time axis is relative to the trigger point
(``Waveform.t()``, trigger at t = 0) with a graticule line per time/div. One 1024-sample frame
spans ``TimebaseEntry.divisions_per_frame`` divisions; the 100 us and 10 us/div rows reuse the
hardware setting of the next slower timebase and are shown as a 2x display zoom of that frame
(``sw_zoom``), around the trigger point, exactly as the vendor software did.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from edu09 import analysis, units
from edu09.gui.fmt import format_si, timebase_text, vdiv_text
from edu09.gui.zoomview import WheelViewBox
from edu09.model import SCREEN_DIVISIONS, Settings, Timebase, TriggerMode, Waveform

__all__ = [
    "PANEL_MEASUREMENTS",
    "CursorReadout",
    "MeasurementPanel",
    "ScopePlot",
    "cursor_readout",
    "is_dark",
    "view_x_range",
    "y_graticule",
]

FRAME_SAMPLES = 1024
#: vertical zoom limits (display only): the whole 8-div screen window down to this many divisions
MIN_VISIBLE_DIVISIONS = 0.5
_ZOOM_EPS = 1e-9
PANEL_MEASUREMENTS: tuple[str, ...] = ("vpp", "frequency", "period", "duty", "mean", "vrms")
_LABELS = {
    "vpp": "Vpp",
    "frequency": "Freq",
    "period": "Period",
    "duty": "Duty",
    "mean": "Mean",
    "vrms": "Vrms",
}


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CursorReadout:
    t1: float
    t2: float
    v1: float
    v2: float

    @property
    def dt(self) -> float:
        """t2 - t1 (seconds)."""
        return self.t2 - self.t1

    @property
    def frequency(self) -> float | None:
        """1 / |dt| (Hz), None when the time cursors coincide."""
        return 1.0 / abs(self.dt) if self.dt != 0 else None

    @property
    def dv(self) -> float:
        """v2 - v1 (volts)."""
        return self.v2 - self.v1


def cursor_readout(t1: float, t2: float, v1: float, v2: float) -> CursorReadout:
    return CursorReadout(float(t1), float(t2), float(v1), float(v2))


def view_x_range(
    tb: Timebase,
    n: int = FRAME_SAMPLES,
    trigger_index: int | None = None,
    dt: float | None = None,
    *,
    zoomed: bool = True,
) -> tuple[float, float]:
    """Visible time window (s, relative to the trigger) for a frame at ``tb``.

    The whole frame for normal rows; the central ``1/sw_zoom`` around the trigger for the
    software-zoomed rows (100 us and 10 us/div). ``zoomed=False`` gives the whole frame.
    """
    if trigger_index is None:
        trigger_index = units.trigger_index(tb)
    if dt is None:
        dt = units.sample_interval(tb)
    zoom = max(int(units.timebase_entry(tb).sw_zoom), 1) if zoomed else 1
    first = -trigger_index * dt
    last = (n - 1 - trigger_index) * dt
    return first / zoom, last / zoom


def y_graticule(s: Settings) -> list[float]:
    """The 9 horizontal graticule line voltages (screen bottom to top)."""
    lo, _hi = s.level_range_v()
    vdiv = float(s.ch1.volts_per_div)
    return [lo + i * vdiv for i in range(SCREEN_DIVISIONS + 1)]


def is_dark(palette: QPalette | None = None) -> bool:
    pal = palette if palette is not None else QApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


def _theme() -> dict[str, Any]:
    pal = QApplication.palette()
    dark = is_dark(pal)
    return {
        "background": pal.color(QPalette.ColorRole.Base),
        "foreground": pal.color(QPalette.ColorRole.Text),
        "trace": QColor("#ffd23f") if dark else QColor("#0b63c5"),
        "trigger": QColor("#ff7b3b") if dark else QColor("#d1495b"),
        "cursor": QColor("#5ad1e6") if dark else QColor("#2a9d50"),
        "ground": QColor("#8fdc6a") if dark else QColor("#3c8d2f"),
    }


# --------------------------------------------------------------------------------------------
# Plot widget
# --------------------------------------------------------------------------------------------


class ScopePlot(QWidget):
    """pyqtgraph plot of CH1 plus indicator badges; emits user interactions as signals."""

    #: trigger level marker dragged to a new level (V)
    levelDragged = Signal(float)
    #: one of the cursors moved
    cursorsChanged = Signal()

    def __init__(self, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme = _theme()
        self._settings = settings if settings is not None else Settings()
        self._waveform: Waveform | None = None
        self.redraws = 0

        self.view_box = WheelViewBox(pan_y=True)
        self.widget: Any = pg.PlotWidget(background=theme["background"], viewBox=self.view_box)
        self.item: Any = self.widget.getPlotItem()
        self.item.showGrid(x=True, y=True, alpha=0.3)
        self.item.setMenuEnabled(False)
        self.item.hideButtons()
        self.item.setMouseEnabled(x=True, y=False)
        self.item.setLabel("bottom", "Time from trigger", units="s")
        self.item.setLabel("left", "CH1 [V]")
        for name in ("left", "bottom"):
            ax = self.item.getAxis(name)
            ax.setZValue(-200)  # graticule below the trace/markers (ViewBox z is -100)
            ax.setPen(theme["foreground"])
            ax.setTextPen(theme["foreground"])
        self.item.getAxis("left").enableAutoSIPrefix(False)

        self.curve: Any = self.item.plot(pen=pg.mkPen(theme["trace"], width=1.5))
        self.curve.setClipToView(True)

        def trig_pen() -> Any:  # one pen object per item (pyqtgraph keeps references)
            return pg.mkPen(theme["trigger"], width=1, style=Qt.PenStyle.DashLine)

        self.level_line: Any = pg.InfiniteLine(
            angle=0,
            movable=True,
            pen=trig_pen(),
            hoverPen=pg.mkPen(theme["trigger"], width=3),
            label="T {value:.3g} V",
            labelOpts={"position": 0.03, "color": theme["trigger"], "movable": False},
        )
        self.level_line.setZValue(10)
        self.level_line.sigPositionChangeFinished.connect(
            lambda line: self.levelDragged.emit(float(line.value()))
        )
        self.item.addItem(self.level_line, ignoreBounds=True)
        self.trigger_pos_line: Any = pg.InfiniteLine(
            pos=0.0,
            angle=90,
            movable=False,
            pen=trig_pen(),
            label="T",
            labelOpts={"position": 0.97, "color": theme["trigger"]},
        )
        self.item.addItem(self.trigger_pos_line, ignoreBounds=True)
        self.ground_line: Any = pg.InfiniteLine(
            pos=0.0,
            angle=0,
            movable=False,
            pen=pg.mkPen(theme["ground"], width=1, style=Qt.PenStyle.DotLine),
            label="GND",
            labelOpts={"position": 0.97, "color": theme["ground"]},
        )
        self.item.addItem(self.ground_line, ignoreBounds=True)

        self.t_cursors: list[Any] = []
        self.v_cursors: list[Any] = []
        for i in (1, 2):
            t = pg.InfiniteLine(
                angle=90,
                movable=True,
                pen=pg.mkPen(theme["cursor"], width=1),
                hoverPen=pg.mkPen(theme["cursor"], width=3),
                label=f"t{i}",
                labelOpts={"position": 0.08 * i, "color": theme["cursor"]},
            )
            v = pg.InfiniteLine(
                angle=0,
                movable=True,
                pen=pg.mkPen(theme["cursor"], width=1),
                hoverPen=pg.mkPen(theme["cursor"], width=3),
                label=f"v{i}",
                labelOpts={"position": 0.85 + 0.06 * i, "color": theme["cursor"]},
            )
            for line in (t, v):
                line.setVisible(False)
                line.sigPositionChanged.connect(lambda _l: self.cursorsChanged.emit())
                self.item.addItem(line, ignoreBounds=True)
            self.t_cursors.append(t)
            self.v_cursors.append(v)

        # indicator badges above the plot
        self.info_label = QLabel()
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.trigger_badge = QLabel()
        self.clipped_badge = QLabel("CLIPPED")
        self.clipped_badge.setToolTip("ADC at its limit: increase V/div or move the position")
        self.clipped_badge.setStyleSheet(
            "QLabel { background: #c62828; color: white; padding: 1px 6px; border-radius: 3px; }"
        )
        self.clipped_badge.setVisible(False)
        badges = QHBoxLayout()
        badges.addWidget(self.info_label, 1)
        badges.addWidget(self.trigger_badge)
        badges.addWidget(self.clipped_badge)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(badges)
        layout.addWidget(self.widget, 1)

        self._y_full: tuple[float, float] = (0.0, 1.0)
        self._graticule_ticks = True
        self.set_settings(self._settings)
        self.reset_cursors()
        self.view_box.sigYRangeChanged.connect(lambda *_a: self._on_y_range_changed())

    # -- settings / view -------------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self._settings

    def set_settings(self, s: Settings) -> None:
        old = self._settings
        self._settings = s
        lo, hi = s.level_range_v()
        vdiv = float(s.ch1.volts_per_div)
        self._y_full = (lo, hi)
        # Y zoom is display-only and limited to the screen window: zoom-out stops at the full
        # 8 divisions, zoom-in at MIN_VISIBLE_DIVISIONS. A V/div or position change resets it.
        self.item.setLimits(
            yMin=lo, yMax=hi, minYRange=MIN_VISIBLE_DIVISIONS * vdiv, maxYRange=hi - lo
        )
        if old.level_range_v() != s.level_range_v() or not hasattr(self, "_xrange"):
            self.item.setYRange(lo, hi, padding=0)
        self._apply_y_ticks()
        self.level_line.setBounds((lo, hi))
        self.level_line.setValue(s.trigger.level_v)
        self.level_line.setVisible(s.trigger.mode is not TriggerMode.NONE)
        tb = s.timebase
        self.item.getAxis("bottom").setTickSpacing(levels=[(5 * tb.seconds, 0), (tb.seconds, 0)])
        if old.timebase is not s.timebase or not hasattr(self, "_xrange"):
            self._reset_x()
        for line in self.v_cursors:
            if not lo <= line.value() <= hi:
                self.reset_cursors(time=False)
                break
        self._update_info()

    def reset_view(self) -> None:
        """Show the default time window and the full 8-division screen (both axes)."""
        self.reset_y_zoom()
        self._reset_x()

    def reset_y_zoom(self) -> None:
        lo, hi = self._y_full
        self.item.setYRange(lo, hi, padding=0)

    @property
    def y_zoom(self) -> float:
        """Vertical display zoom factor (1 = the full 8-division screen)."""
        lo, hi = self._y_full
        r = self.item.viewRange()[1]
        span = float(r[1] - r[0])
        return (hi - lo) / span if span > 0 else 1.0

    def y_range(self) -> tuple[float, float]:
        r = self.item.viewRange()[1]
        return float(r[0]), float(r[1])

    def _on_y_range_changed(self) -> None:
        self._apply_y_ticks()
        self._update_info()

    def _apply_y_ticks(self) -> None:
        """Graticule labels while unzoomed, pyqtgraph's automatic ticks (in volts) when zoomed."""
        graticule = self.y_zoom < 1 + _ZOOM_EPS
        if graticule:
            ticks = [[(v, f"{v:.3g}") for v in y_graticule(self._settings)], []]
            self.item.getAxis("left").setTicks(ticks)
        elif self._graticule_ticks:
            self.item.getAxis("left").setTicks(None)
        self._graticule_ticks = graticule

    def _reset_x(self) -> None:
        """Show the default time window for the current timebase."""
        tb = self._settings.timebase
        wf = self._waveform
        if wf is not None and wf.settings.timebase is tb:
            geom: tuple[int, int | None, float | None] = (wf.n, wf.trigger_index, wf.dt)
        else:
            geom = (FRAME_SAMPLES, None, None)
        x0, x1 = view_x_range(tb, *geom)
        f0, f1 = view_x_range(tb, *geom, zoomed=False)
        self._xrange = (x0, x1)
        self.item.setLimits(xMin=f0, xMax=f1)
        self.item.setXRange(x0, x1, padding=0)
        lo_c, hi_c = self.t_cursors[0].value(), self.t_cursors[1].value()
        if not (x0 <= lo_c <= x1 and x0 <= hi_c <= x1):
            self.reset_cursors(volts=False)

    def x_range(self) -> tuple[float, float]:
        r = self.item.viewRange()[0]
        return float(r[0]), float(r[1])

    # -- cursors -------------------------------------------------------------------------------
    def set_cursors_visible(self, visible: bool) -> None:
        for line in (*self.t_cursors, *self.v_cursors):
            line.setVisible(visible)
        self.cursorsChanged.emit()

    def cursors_visible(self) -> bool:
        return bool(self.t_cursors[0].isVisible())

    def reset_cursors(self, time: bool = True, volts: bool = True) -> None:
        if time:
            x0, x1 = getattr(self, "_xrange", view_x_range(self._settings.timebase))
            span = x1 - x0
            self.t_cursors[0].setValue(x0 + span / 3)
            self.t_cursors[1].setValue(x0 + 2 * span / 3)
        if volts:
            lo, hi = self._settings.level_range_v()
            self.v_cursors[0].setValue(lo + (hi - lo) / 4)
            self.v_cursors[1].setValue(lo + 3 * (hi - lo) / 4)

    def set_cursor_positions(self, t1: float, t2: float, v1: float, v2: float) -> None:
        self.t_cursors[0].setValue(t1)
        self.t_cursors[1].setValue(t2)
        self.v_cursors[0].setValue(v1)
        self.v_cursors[1].setValue(v2)

    def cursor_readout(self) -> CursorReadout:
        return cursor_readout(
            self.t_cursors[0].value(),
            self.t_cursors[1].value(),
            self.v_cursors[0].value(),
            self.v_cursors[1].value(),
        )

    # -- data ------------------------------------------------------------------------------------
    @property
    def waveform(self) -> Waveform | None:
        return self._waveform

    def show_waveform(self, wf: Waveform, volts: npt.NDArray[np.float64] | None = None) -> None:
        """Draw ``wf`` (``volts`` overrides ``wf.volts``, e.g. despiked ETS samples)."""
        prev = self._waveform
        self._waveform = wf
        y = wf.volts if volts is None else volts
        self.curve.setData(wf.t(), y)
        self.redraws += 1
        self.trigger_pos_line.setVisible(wf.trigger_index is not None)
        self.ground_line.setValue(0.0)
        self.clipped_badge.setVisible(bool(wf.clipped))
        if wf.settings.trigger.mode is TriggerMode.NONE:
            self.trigger_badge.setText("Free run")
        elif wf.triggered:
            self.trigger_badge.setText("Trig'd")
        else:
            self.trigger_badge.setText("Auto (untriggered)")
        tb_changed = prev is None or prev.settings.timebase is not wf.settings.timebase
        if tb_changed and wf.settings.timebase is self._settings.timebase:
            self._reset_x()
        self._update_info()

    def clear(self) -> None:
        self._waveform = None
        self.curve.setData([], [])
        self.clipped_badge.setVisible(False)
        self.trigger_badge.setText("")
        self._update_info()

    def _update_info(self) -> None:
        s = self._settings
        parts = [
            f"CH1 {vdiv_text(float(s.ch1.volts_per_div))} {s.ch1.coupling.value.upper()}",
            timebase_text(s.timebase.seconds),
        ]
        if self.y_zoom >= 1 + _ZOOM_EPS:
            parts.append(f"Y zoom \u00d7{self.y_zoom:.3g}")
        wf = self._waveform
        if wf is not None:
            parts.append(f"{format_si(wf.sample_rate_hz, 'S/s', 4)}")
            if wf.settings.timebase.is_ets:
                parts.append("ETS")
            parts.append(wf.waveform_id)
        self.info_label.setText("   ".join(parts))


# --------------------------------------------------------------------------------------------
# Measurements + cursor readout panel
# --------------------------------------------------------------------------------------------


class MeasurementPanel(QWidget):
    """Automatic measurements of the displayed frame plus the cursor readout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.values: dict[str, QLabel] = {}
        mgroup = QGroupBox("Measurements")
        mgrid = QGridLayout(mgroup)
        for row, name in enumerate(PANEL_MEASUREMENTS):
            mgrid.addWidget(QLabel(_LABELS[name]), row, 0)
            val = QLabel("—")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setMinimumWidth(90)
            mgrid.addWidget(val, row, 1)
            self.values[name] = val

        self.cursor_group = QGroupBox("Cursors")
        cgrid = QGridLayout(self.cursor_group)
        self.cursor_values: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(
            (("t1", "t1"), ("t2", "t2"), ("dt", "Δt"), ("freq", "1/Δt"))
            + (("v1", "v1"), ("v2", "v2"), ("dv", "ΔV"))
        ):
            cgrid.addWidget(QLabel(text), row, 0)
            val = QLabel("—")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cgrid.addWidget(val, row, 1)
            self.cursor_values[key] = val
        self.cursor_group.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(mgroup)
        layout.addWidget(self.cursor_group)
        layout.addStretch(1)

    def update_measurements(
        self, volts: npt.ArrayLike, dt: float
    ) -> dict[str, analysis.Measurement]:
        res = analysis.measure(volts, dt, PANEL_MEASUREMENTS)
        for name, m in res.items():
            label = self.values[name]
            label.setText(format_si(m.value, m.unit) if m.valid else "—")
            label.setToolTip(m.note or "")
        return res

    def clear(self) -> None:
        for label in self.values.values():
            label.setText("—")
            label.setToolTip("")

    def set_cursor_readout(self, r: CursorReadout | None) -> None:
        self.cursor_group.setVisible(r is not None)
        if r is None:
            return
        texts = {
            "t1": format_si(r.t1, "s"),
            "t2": format_si(r.t2, "s"),
            "dt": format_si(r.dt, "s"),
            "freq": format_si(r.frequency, "Hz"),
            "v1": format_si(r.v1, "V"),
            "v2": format_si(r.v2, "V"),
            "dv": format_si(r.dv, "V"),
        }
        for key, text in texts.items():
            self.cursor_values[key].setText(text)
