"""Spectrum view: FFT of the displayed CH1 frame (``edu09.analysis.spectrum``) in dBV.

The panel never talks to the Scope. The main window hands it every frame it draws
(:meth:`SpectrumPanel.update_waveform`, already throttled to the redraw rate) while the panel is
visible. Averaging (power, over the last N frames) restarts whenever the frame geometry or
acquisition settings change, or any panel setting changes. Peak hold is an extra trace with the
per-bin maximum of the displayed (possibly averaged) spectrum since the last reset.

``edu09.analysis`` spectrum functions are looked up at call time.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from edu09 import analysis
from edu09.gui.fmt import format_si
from edu09.gui.plot import _theme
from edu09.model import Waveform

if TYPE_CHECKING:
    from edu09.analysis import Spectrum

__all__ = [
    "AVERAGING_CHOICES",
    "DEFAULT_DB_RANGE",
    "ETS_NOTE",
    "MARKER_PEAKS",
    "SpectrumCursor",
    "SpectrumPanel",
    "nearest_bin",
    "peak_label",
]

#: averaging combo: (text, frames); 1 = off
AVERAGING_CHOICES: tuple[tuple[str, int], ...] = (
    ("Off", 1),
    ("4 frames", 4),
    ("8 frames", 8),
    ("16 frames", 16),
)
DEFAULT_DB_RANGE: tuple[float, float] = (-80.0, 20.0)
MARKER_PEAKS = 5
LABEL_LEVELS = 4
ETS_NOTE = "ETS (equivalent-time) frame: the spectrum is valid for repetitive signals only"
CLIPPED_NOTE = "clipped: the ADC is at its limit, so some harmonics are not real"


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpectrumCursor:
    """Crosshair readout snapped to the nearest bin."""

    bin: int
    freq_hz: float
    dbv: float
    vrms: float

    def text(self) -> str:
        return (
            f"Cursor: {format_si(self.freq_hz, 'Hz')}, {self.dbv:.1f} dBV, "
            f"{format_si(self.vrms, 'Vrms')}"
        )


def nearest_bin(freqs_hz: npt.ArrayLike, freq_hz: float) -> int:
    """Index of the bin closest to ``freq_hz`` (clamped to the array)."""
    f = np.asarray(freqs_hz, dtype=np.float64)
    if f.size == 0:
        raise ValueError("empty frequency axis")
    i = int(np.searchsorted(f, freq_hz))
    if i <= 0:
        return 0
    if i >= f.size:
        return int(f.size - 1)
    return i if abs(f[i] - freq_hz) < abs(freq_hz - f[i - 1]) else i - 1


def peak_label(freq_hz: float, dbv: float) -> str:
    """Marker text: ``"30.01 kHz, -3.2 dBV"``."""
    return f"{format_si(freq_hz, 'Hz')}, {dbv:.1f} dBV"


# --------------------------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------------------------


class SpectrumPanel(QWidget):
    """Spectrum plot with window/averaging/peak-hold/log-f controls, peak markers and readouts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme = _theme()
        self._frames: deque[npt.NDArray[np.float64]] = deque(maxlen=1)
        self._frame_ids: deque[str] = deque(maxlen=1)
        self._clipped: deque[bool] = deque(maxlen=1)
        self._key: tuple[Any, ...] | None = None
        self._fit_pending = True  # fit the frequency axis to the next spectrum
        self._held: npt.NDArray[np.float64] | None = None
        self._spectrum: Spectrum | None = None
        self._ets = False
        self.computed = 0  # spectra computed (for tests)
        self.crosshair: SpectrumCursor | None = None

        # controls
        self.window_combo = QComboBox()
        for name in analysis.WINDOWS:
            self.window_combo.addItem(name.capitalize() if name != "rect" else "Rectangular", name)
        self.window_combo.setToolTip(
            "FFT window: Hann (general), Flat-top (accurate amplitudes), Rectangular (no window)"
        )
        self.avg_combo = QComboBox()
        for text, frames in AVERAGING_CHOICES:
            self.avg_combo.addItem(text, frames)
        self.avg_combo.setToolTip("Power averaging over the last N frames (resets on any change)")
        self.peak_hold_check = QCheckBox("Peak hold")
        self.peak_hold_check.setToolTip("Show the per-bin maximum since the last change")
        self.log_check = QCheckBox("Log f")
        self.log_check.setToolTip("Logarithmic frequency axis (DC bin hidden)")
        self.autoscale_check = QCheckBox("Auto Y")
        self.autoscale_check.setToolTip(
            f"Autoscale the dBV axis (default {DEFAULT_DB_RANGE[0]:g}..{DEFAULT_DB_RANGE[1]:+g})"
        )
        #: mirrors View > Despike ETS frames (wired by the main window)
        self.despike_check = QCheckBox("Despike ETS")
        self.despike_check.setToolTip(
            "Remove isolated outlier samples of ETS frames before the FFT (View menu option)"
        )
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel("Window"))
        bar.addWidget(self.window_combo)
        bar.addWidget(QLabel("Average"))
        bar.addWidget(self.avg_combo)
        for w in (self.peak_hold_check, self.log_check, self.autoscale_check, self.despike_check):
            bar.addWidget(w)
        bar.addStretch(1)

        # plot
        self.widget: Any = pg.PlotWidget(background=theme["background"])
        self.item: Any = self.widget.getPlotItem()
        self.item.showGrid(x=True, y=True, alpha=0.3)
        self.item.setMenuEnabled(False)
        self.item.hideButtons()
        self.item.setMouseEnabled(x=True, y=False)
        self.item.setLabel("bottom", "Frequency", units="Hz")
        self.item.setLabel("left", "CH1 [dBV]")
        for name in ("left", "bottom"):
            ax = self.item.getAxis(name)
            ax.setPen(theme["foreground"])
            ax.setTextPen(theme["foreground"])
        self.item.getAxis("left").enableAutoSIPrefix(False)
        self.held_curve: Any = self.item.plot(
            pen=pg.mkPen(QColor(theme["trace"]).darker(160), width=1)
        )
        self.held_curve.setVisible(False)
        self.curve: Any = self.item.plot(pen=pg.mkPen(theme["trace"], width=1.2))
        self.markers: Any = self.item.plot(
            pen=None,
            symbol="o",
            symbolSize=7,
            symbolPen=pg.mkPen(theme["trigger"]),
            symbolBrush=None,
        )
        self.marker_labels: list[Any] = []
        for _ in range(MARKER_PEAKS):
            label = pg.TextItem(color=theme["trigger"], anchor=(0.5, 1.15))
            label.setVisible(False)
            self.item.addItem(label, ignoreBounds=True)
            self.marker_labels.append(label)
        cross_pen = pg.mkPen(theme["cursor"], width=1, style=Qt.PenStyle.DashLine)
        self.vline: Any = pg.InfiniteLine(angle=90, movable=False, pen=cross_pen)
        self.hline: Any = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(cross_pen))
        for line in (self.vline, self.hline):
            line.setVisible(False)
            self.item.addItem(line, ignoreBounds=True)
        self.item.setYRange(*DEFAULT_DB_RANGE, padding=0)
        self.widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # readouts
        self.readout_label = QLabel("—")
        self.readout_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cursor_label = QLabel("")
        self.cursor_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setStyleSheet("QLabel { color: #d9822b; }")
        self.warnings_label.setVisible(False)
        readouts = QHBoxLayout()
        readouts.setContentsMargins(0, 0, 0, 0)
        readouts.addWidget(self.readout_label, 1)
        readouts.addWidget(self.cursor_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addLayout(bar)
        layout.addWidget(self.widget, 1)
        layout.addLayout(readouts)
        layout.addWidget(self.warnings_label)

        self.window_combo.currentIndexChanged.connect(lambda _i: self._settings_changed())
        self.avg_combo.currentIndexChanged.connect(lambda _i: self._settings_changed())
        self.peak_hold_check.toggled.connect(lambda _on: self._settings_changed())
        self.log_check.toggled.connect(self._set_log)
        self.autoscale_check.toggled.connect(self._set_autoscale)
        self._apply_averaging()

    # -- settings ----------------------------------------------------------------------------
    @property
    def window_name(self) -> str:
        return str(self.window_combo.currentData())

    @property
    def averages(self) -> int:
        return int(self.avg_combo.currentData())

    @property
    def spectrum(self) -> Spectrum | None:
        """The spectrum shown last (None before the first frame / after a failure)."""
        return self._spectrum

    @property
    def held_dbv(self) -> npt.NDArray[np.float64] | None:
        return None if self._held is None else self._held.copy()

    @property
    def frames_averaged(self) -> int:
        return len(self._frames)

    def set_window(self, name: str) -> None:
        idx = self.window_combo.findData(name)
        if idx < 0:
            raise ValueError(f"unknown window {name!r}")
        self.window_combo.setCurrentIndex(idx)

    def set_averaging(self, frames: int) -> None:
        idx = self.avg_combo.findData(frames)
        if idx < 0:
            raise ValueError(f"averaging must be one of {[f for _t, f in AVERAGING_CHOICES]}")
        self.avg_combo.setCurrentIndex(idx)

    def reset(self) -> None:
        """Restart averaging and peak hold (the next frame starts afresh)."""
        self._frames.clear()
        self._frame_ids.clear()
        self._clipped.clear()
        self._held = None
        self._key = None

    def _apply_averaging(self) -> None:
        n = max(self.averages, 1)
        self._frames = deque(maxlen=n)
        self._frame_ids = deque(maxlen=n)
        self._clipped = deque(maxlen=n)
        self.reset()

    def _settings_changed(self) -> None:
        self._apply_averaging()
        self.held_curve.setVisible(self.peak_hold_check.isChecked())
        if not self.peak_hold_check.isChecked():
            self.held_curve.setData([], [])

    def _set_log(self, on: bool) -> None:
        self.item.setLogMode(x=on, y=False)
        self.crosshair = None
        self.cursor_label.setText("")
        for line in (self.vline, self.hline):
            line.setVisible(False)
        self._fit_pending = True
        self._redraw()

    def _set_autoscale(self, on: bool) -> None:
        if on:
            self.item.enableAutoRange(y=True)
        else:
            self.item.disableAutoRange(axis="y")
            self.item.setYRange(*DEFAULT_DB_RANGE, padding=0)

    # -- data ------------------------------------------------------------------------------------
    def update_waveform(
        self, wf: Waveform, volts: npt.NDArray[np.float64] | None = None
    ) -> Spectrum | None:
        """Add ``wf`` (``volts`` overrides ``wf.volts``, e.g. despiked) and redraw.

        Re-showing the frame added last (e.g. after toggling despike) replaces it instead of
        averaging it twice. Returns the new spectrum, or None if it could not be computed.
        """
        y = np.asarray(wf.volts if volts is None else volts, dtype=np.float64)
        key = (wf.settings, float(wf.dt), int(y.size))
        if key != self._key:
            self.reset()
            self._key = key
            self._fit_pending = True
        wid = f"{wf.waveform_id}:{wf.seq}"
        if self._frame_ids and self._frame_ids[-1] == wid:
            self._frames.pop()
            self._frame_ids.pop()
            self._clipped.pop()
        self._frames.append(y)
        self._frame_ids.append(wid)
        self._clipped.append(bool(wf.clipped))
        self._ets = bool(wf.settings.timebase.is_ets)
        try:
            if len(self._frames) > 1:
                spec = analysis.spectrum_average(
                    list(self._frames), float(wf.dt), window=self.window_name, mode="power"
                )
            else:
                spec = analysis.spectrum(y, float(wf.dt), window=self.window_name)
        except (ValueError, ArithmeticError) as exc:
            self._spectrum = None
            self._show_failure(f"{type(exc).__name__}: {exc}")
            return None
        self.computed += 1
        self._spectrum = spec
        if self.peak_hold_check.isChecked():
            if self._held is None or self._held.shape != spec.dbv.shape:
                self._held = np.array(spec.dbv, dtype=np.float64)
            else:
                self._held = np.maximum(self._held, spec.dbv)
        self._redraw()
        return spec

    def clear(self) -> None:
        self.reset()
        self._spectrum = None
        self.curve.setData([], [])
        self.held_curve.setData([], [])
        self.markers.setData([], [])
        for label in self.marker_labels:
            label.setVisible(False)
        self.readout_label.setText("—")
        self._set_warnings([])

    def warnings(self) -> list[str]:
        """The warnings line items for the current spectrum."""
        spec = self._spectrum
        out = list(spec.warnings) if spec is not None else []
        if any(self._clipped):
            out.append(str(getattr(analysis, "CLIPPED_WARNING", CLIPPED_NOTE)))
        if self._ets:
            out.append(str(getattr(analysis, "ETS_WARNING", ETS_NOTE)))
        return list(dict.fromkeys(out))  # unique, in order

    def _show_failure(self, text: str) -> None:
        self.curve.setData([], [])
        self.markers.setData([], [])
        for label in self.marker_labels:
            label.setVisible(False)
        self.readout_label.setText("—")
        self._set_warnings([f"spectrum failed: {text}"])

    def _set_warnings(self, items: list[str]) -> None:
        self.warnings_label.setText("⚠ " + "; ".join(items) if items else "")
        self.warnings_label.setVisible(bool(items))

    def _x(self, freq_hz: float) -> float:
        """View x coordinate of a frequency (log10 in log mode)."""
        if self.log_check.isChecked():
            return math.log10(max(freq_hz, 1e-12))
        return freq_hz

    def _redraw(self) -> None:
        spec = self._spectrum
        if spec is None:
            return
        f = np.asarray(spec.freqs_hz, dtype=np.float64)
        db = np.asarray(spec.dbv, dtype=np.float64)
        lo = 1 if self.log_check.isChecked() else 0  # no DC bin on a log axis
        self.curve.setData(f[lo:], db[lo:])
        if self.peak_hold_check.isChecked() and self._held is not None:
            self.held_curve.setData(f[lo:], self._held[lo:])
            self.held_curve.setVisible(True)
        peaks = [p for p in spec.peaks if p.freq_hz > 0][:MARKER_PEAKS]
        self.markers.setData([p.freq_hz for p in peaks], [p.dbv for p in peaks])
        if self._fit_pending and f.size > 1:
            self._fit_pending = False
            self.item.setXRange(self._x(float(f[lo])), self._x(float(f[-1])), padding=0)
        self._place_labels(peaks, float(f[-1]) if f.size else 0.0)
        self.readout_label.setText(self._readout_text(spec))
        self._set_warnings(self.warnings())
        if self.crosshair is not None:
            self.set_cursor_freq(self.crosshair.freq_hz)

    def _place_labels(self, peaks: list[analysis.SpectrumPeak], fmax: float) -> None:
        """Peak labels, left-aligned at the marker (right-aligned near the right edge).

        Neighbouring harmonics are closer than a label is wide, so each label (in frequency
        order) takes the lowest of LABEL_LEVELS heights where its estimated box (from the current
        view geometry) misses the boxes already placed.
        """
        vb = self.item.getViewBox()
        (x0, x1), (y0, y1) = vb.viewRange()
        w_px, h_px = max(float(vb.width()), 1.0), max(float(vb.height()), 1.0)
        x_per_px, y_per_px = (x1 - x0) / w_px, (y1 - y0) / h_px
        boxes: list[tuple[float, float, float, float]] = []  # x_lo, x_hi, y_lo, y_hi (view units)
        by_freq = sorted(range(len(peaks)), key=lambda i: peaks[i].freq_hz)
        for i in by_freq:
            p, label = peaks[i], self.marker_labels[i]
            label.setText(peak_label(p.freq_hz, p.dbv).replace(", ", "\n"))  # two lines
            rect = label.boundingRect()
            w, h = (rect.width() + 8.0) * x_per_px, (rect.height() + 2.0) * y_per_px
            right = fmax > 0 and p.freq_hz > 0.8 * fmax
            xp = self._x(p.freq_hz)
            xl, xh = (xp - w, xp) if right else (xp, xp + w)
            level = 0  # fallback when every height collides: overlap rather than leave the plot
            for lv in range(LABEL_LEVELS):
                yl = p.dbv + (0.15 + 1.15 * lv) * h
                if p.dbv + (1.15 + 1.15 * lv) * h > y1:
                    break
                if not any(
                    xl < bx1 and xh > bx0 and yl < by1 and yl + h > by0
                    for bx0, bx1, by0, by1 in boxes
                ):
                    level = lv
                    break
            boxes.append(
                (xl, xh, p.dbv + (0.15 + 1.15 * level) * h, p.dbv + (1.15 + 1.15 * level) * h)
            )
            label.setAnchor((1.0 if right else 0.0, 1.15 + 1.15 * level))
            label.setPos(QPointF(xp, p.dbv))
            label.setVisible(True)
        for i in range(len(peaks), len(self.marker_labels)):
            self.marker_labels[i].setVisible(False)

    def _readout_text(self, spec: Spectrum) -> str:
        parts: list[str] = []
        fund = spec.fundamental
        if fund is not None:
            parts.append(
                f"Fundamental {format_si(fund.freq_hz, 'Hz')}, {fund.dbv:.1f} dBV "
                f"({format_si(fund.vrms, 'Vrms')})"
            )
        else:
            parts.append("Fundamental —")
        parts.append(f"THD {format_si(spec.thd_pct, '%')}")
        parts.append(f"RBW {format_si(spec.resolution_hz, 'Hz')}")
        parts.append(f"DC {format_si(spec.dc_v, 'V')}")
        if spec.averages > 1:
            parts.append(f"avg {spec.averages}/{self.averages}")
        return "   ".join(parts)

    # -- crosshair -------------------------------------------------------------------------------
    def set_cursor_freq(self, freq_hz: float) -> SpectrumCursor | None:
        """Snap the crosshair to the bin nearest ``freq_hz`` and update its readout."""
        spec = self._spectrum
        if spec is None or len(spec.freqs_hz) == 0:
            return None
        k = nearest_bin(spec.freqs_hz, freq_hz)
        if self.log_check.isChecked() and k == 0 and len(spec.freqs_hz) > 1:
            k = 1
        cur = SpectrumCursor(
            bin=k,
            freq_hz=float(spec.freqs_hz[k]),
            dbv=float(spec.dbv[k]),
            vrms=float(spec.vrms[k]),
        )
        self.crosshair = cur
        self.vline.setValue(self._x(cur.freq_hz))
        self.hline.setValue(cur.dbv)
        self.vline.setVisible(True)
        self.hline.setVisible(True)
        self.cursor_label.setText(cur.text())
        return cur

    def _on_mouse_moved(self, pos: Any) -> None:
        vb = self.item.getViewBox()
        if self._spectrum is None or not vb.sceneBoundingRect().contains(pos):
            return
        x = float(vb.mapSceneToView(pos).x())
        if self.log_check.isChecked():
            x = 10.0 ** min(x, 300.0)
        self.set_cursor_freq(x)
