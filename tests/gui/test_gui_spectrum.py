"""Spectrum panel (``edu09.gui.spectrum``) and its main-window integration (simulator only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from gui_helpers import make_window

from edu09 import analysis, units
from edu09.device.scope import Scope
from edu09.gui.spectrum import (
    AVERAGING_CHOICES,
    DEFAULT_DB_RANGE,
    MARKER_PEAKS,
    SpectrumPanel,
    nearest_bin,
    peak_label,
)
from edu09.model import Settings, Waveform
from edu09.transport.sim import SimTransport

pytestmark = pytest.mark.gui

WAIT_MS = 10_000


def _wf(
    seq: int,
    settings: Settings,
    *,
    freq_hz: float = 30e3,
    amp_v: float = 2.0,
    noise: float = 0.0,
    clip: bool = False,
) -> Waveform:
    dt = units.sample_interval(settings.timebase)
    t = np.arange(1024) * dt
    rng = np.random.default_rng(seq)
    volts = amp_v * np.sin(2 * np.pi * freq_hz * t) + noise * rng.standard_normal(t.size)
    raw = np.clip(np.round(127 + volts / float(settings.ch1.volts_per_div) * 32), 1, 254)
    if clip:
        raw[10] = 255
    return Waveform(
        seq=seq,
        timestamp=datetime.now(UTC),
        raw=raw.astype(np.uint8),
        volts=volts,
        dt=dt,
        trigger_index=units.trigger_index(settings.timebase),
        settings=settings,
        calibration_id="t",
    )


S200 = Settings().replace(timebase="200us", volts_per_div=1.0)


@pytest.fixture
def panel(qtbot: Any) -> SpectrumPanel:
    p = SpectrumPanel()
    qtbot.addWidget(p)
    return p


# -- pure helpers -------------------------------------------------------------------------------


def test_nearest_bin_and_peak_label() -> None:
    f = np.arange(5) * 100.0
    assert nearest_bin(f, -5) == 0
    assert nearest_bin(f, 149) == 1
    assert nearest_bin(f, 151) == 2
    assert nearest_bin(f, 1e9) == 4
    with pytest.raises(ValueError):
        nearest_bin([], 1.0)
    assert peak_label(30_010.0, -3.24) == "30.01 kHz, -3.2 dBV"


# -- panel -------------------------------------------------------------------------------------


def test_panel_defaults(panel: SpectrumPanel) -> None:
    names = [panel.window_combo.itemData(i) for i in range(panel.window_combo.count())]
    assert names == list(analysis.WINDOWS)
    assert panel.window_name == "hann"
    frames = [panel.avg_combo.itemData(i) for i in range(panel.avg_combo.count())]
    assert frames == [f for _t, f in AVERAGING_CHOICES] == [1, 4, 8, 16]
    assert panel.averages == 1
    lo, hi = panel.item.viewRange()[1]
    assert (lo, hi) == pytest.approx(DEFAULT_DB_RANGE)
    assert panel.spectrum is None
    assert not panel.warnings_label.isVisibleTo(panel)


def test_sine_peak_fundamental_and_markers(panel: SpectrumPanel) -> None:
    spec = panel.update_waveform(_wf(1, S200))
    assert spec is not None and panel.spectrum is spec and panel.computed == 1
    assert spec.fundamental is not None
    assert spec.fundamental.freq_hz == pytest.approx(30e3, rel=0.01)
    assert spec.fundamental.vrms == pytest.approx(2.0 / np.sqrt(2), rel=0.15)
    x, y = panel.curve.getData()
    assert len(x) == len(spec.freqs_hz) and x[0] == 0.0
    shown = [lbl for lbl in panel.marker_labels if lbl.isVisible()]
    assert 1 <= len(shown) <= MARKER_PEAKS
    assert shown[0].toPlainText().replace("\n", ", ") == peak_label(
        spec.peaks[0].freq_hz, spec.peaks[0].dbv
    )
    text = panel.readout_label.text()
    assert "Fundamental 30" in text and "kHz" in text and "THD" in text
    # 156.25 kS/s realtime: fs/2 < 200 kHz -> aliasing warning, no ETS note
    warn = panel.warnings_label.text()
    assert panel.warnings_label.isVisibleTo(panel)
    assert "fold back" in warn
    assert "ETS" not in warn


def test_window_change_resets_averaging(panel: SpectrumPanel) -> None:
    panel.set_averaging(4)
    for seq in range(1, 7):
        spec = panel.update_waveform(_wf(seq, S200, noise=0.05))
        assert spec is not None
        assert spec.averages == min(seq, 4)
    assert panel.frames_averaged == 4
    assert "avg 4/4" in panel.readout_label.text()
    panel.set_window("flattop")
    assert panel.frames_averaged == 0
    spec = panel.update_waveform(_wf(7, S200, noise=0.05))
    assert spec is not None and spec.window == "flattop" and spec.averages == 1
    with pytest.raises(ValueError):
        panel.set_averaging(3)
    with pytest.raises(ValueError):
        panel.set_window("kaiser")


def test_settings_change_resets_and_same_frame_is_replaced(panel: SpectrumPanel) -> None:
    panel.set_averaging(8)
    wf = _wf(1, S200)
    panel.update_waveform(wf)
    panel.update_waveform(wf)  # re-shown (e.g. despike toggled): not averaged twice
    assert panel.frames_averaged == 1
    panel.update_waveform(_wf(2, S200))
    assert panel.frames_averaged == 2
    other = S200.replace(volts_per_div=2.0)
    spec = panel.update_waveform(_wf(3, other))
    assert panel.frames_averaged == 1 and spec is not None and spec.averages == 1


def test_peak_hold_keeps_maximum(panel: SpectrumPanel) -> None:
    panel.peak_hold_check.setChecked(True)
    panel.update_waveform(_wf(1, S200, freq_hz=20e3))
    held1 = panel.held_dbv
    spec2 = panel.update_waveform(_wf(2, S200, freq_hz=40e3))
    held2 = panel.held_dbv
    assert held1 is not None and held2 is not None and spec2 is not None
    assert np.all(held2 >= held1 - 1e-9) and np.all(held2 >= spec2.dbv - 1e-9)
    k20 = nearest_bin(spec2.freqs_hz, 20e3)
    assert held2[k20] > spec2.dbv[k20] + 20  # the 20 kHz tone is held
    assert panel.held_curve.isVisible()
    panel.peak_hold_check.setChecked(False)
    assert panel.held_dbv is None
    x, _y = panel.held_curve.getData()
    assert x is None or len(x) == 0


def test_log_axis_and_crosshair(panel: SpectrumPanel) -> None:
    spec = panel.update_waveform(_wf(1, S200))
    assert spec is not None
    cur = panel.set_cursor_freq(30.1e3)
    assert cur is not None
    assert cur.freq_hz == pytest.approx(spec.freqs_hz[nearest_bin(spec.freqs_hz, 30.1e3)])
    assert cur.dbv == pytest.approx(spec.dbv[cur.bin]) and cur.vrms == pytest.approx(
        spec.vrms[cur.bin]
    )
    assert "Cursor:" in panel.cursor_label.text() and "dBV" in panel.cursor_label.text()
    assert panel.vline.value() == pytest.approx(cur.freq_hz)

    assert panel.item.viewRange()[0] == pytest.approx((0.0, spec.fs_hz / 2))  # 0..fs/2

    panel.log_check.setChecked(True)
    lo, hi = panel.item.viewRange()[0]
    assert (lo, hi) == pytest.approx((np.log10(spec.freqs_hz[1]), np.log10(spec.fs_hz / 2)))
    x, _y = panel.curve.getData()
    assert len(x) == len(spec.freqs_hz) - 1 and x[0] > 0  # no DC bin on a log axis
    assert panel.item.ctrl.logXCheck.isChecked()
    cur = panel.set_cursor_freq(0.0)
    assert cur is not None and cur.bin == 1
    assert panel.vline.value() == pytest.approx(np.log10(cur.freq_hz))


def test_mouse_move_updates_crosshair(qtbot: Any, panel: SpectrumPanel) -> None:
    panel.resize(800, 300)
    panel.show()
    qtbot.waitExposed(panel)
    spec = panel.update_waveform(_wf(1, S200))
    assert spec is not None
    vb = panel.item.getViewBox()
    pos = vb.mapViewToScene(vb.viewRect().center())
    panel.widget.scene().sigMouseMoved.emit(pos)
    assert panel.crosshair is not None
    x_view = float(vb.mapSceneToView(pos).x())
    assert panel.crosshair.bin == nearest_bin(spec.freqs_hz, x_view)


def test_clipped_and_ets_warnings(panel: SpectrumPanel) -> None:
    panel.update_waveform(_wf(1, S200, clip=True))
    assert "clipped" in panel.warnings_label.text()
    ets = Settings().replace(timebase="20us", trigger_mode="normal")
    panel.update_waveform(_wf(2, ets))
    warn = panel.warnings_label.text()
    assert "repetitive" in warn
    assert "clipped" not in warn  # the clipped frame belonged to the old settings


def test_autoscale_toggle(panel: SpectrumPanel) -> None:
    panel.update_waveform(_wf(1, S200))
    panel.autoscale_check.setChecked(True)
    assert panel.item.getViewBox().autoRangeEnabled()[1]
    panel.autoscale_check.setChecked(False)
    assert not panel.item.getViewBox().autoRangeEnabled()[1]
    assert panel.item.viewRange()[1] == pytest.approx(DEFAULT_DB_RANGE)


def test_failure_is_shown_not_raised(panel: SpectrumPanel, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("bad frame")

    monkeypatch.setattr(analysis, "spectrum", boom)
    assert panel.update_waveform(_wf(1, S200)) is None
    assert panel.spectrum is None
    assert "bad frame" in panel.warnings_label.text()


# -- main window ------------------------------------------------------------------------------


def test_window_spectrum_toggle_and_live_update(qtbot: Any) -> None:
    scope = Scope.open(SimTransport.virtual("square:30khz:4vpp:duty=60%", seed=0))
    try:
        scope.configure(timebase="200us", volts_per_div=1.0)
        win = make_window(qtbot, scope)
        win.show()
        qtbot.waitExposed(win)
        assert not win.spectrum_visible()
        assert win.spectrum_action.shortcut().toString() == "Ctrl+F"
        view_menu = next(a.menu() for a in win.menuBar().actions() if "View" in a.text())
        assert win.spectrum_action in view_menu.actions()
        with qtbot.waitSignal(win.rendered, timeout=WAIT_MS):
            win.single()
        assert win.spectrum.computed == 0  # hidden: not computed

        win.spectrum_action.trigger()
        assert win.spectrum_visible() and win.plot.isVisible()
        qtbot.waitUntil(lambda: win.spectrum.spectrum is not None, timeout=WAIT_MS)
        before = win.spectrum.computed
        with qtbot.waitSignal(win.rendered, timeout=WAIT_MS):
            win.single()
        assert win.spectrum.computed > before
        spec = win.spectrum.spectrum
        assert spec is not None and spec.fundamental is not None
        assert spec.fundamental.freq_hz == pytest.approx(30e3, rel=0.02)
        assert spec.thd_pct is not None and spec.thd_pct > 10  # square wave
        assert "fold back" in win.spectrum.warnings_label.text()

        # despike option is mirrored both ways and resets averaging
        win.spectrum.despike_check.setChecked(True)
        assert win.despike_action.isChecked()
        win.despike_action.setChecked(False)
        assert not win.spectrum.despike_check.isChecked()

        win.spectrum_action.trigger()
        assert not win.spectrum_visible()
        n = win.spectrum.computed
        with qtbot.waitSignal(win.rendered, timeout=WAIT_MS):
            win.single()
        assert win.spectrum.computed == n
        win.shutdown()
    finally:
        scope.close()


def test_window_spectrum_averaging_while_running(qtbot: Any, vscope: Scope) -> None:
    win = make_window(qtbot, vscope)
    try:
        win.set_spectrum_visible(True)
        win.spectrum.set_averaging(4)
        win.run()
        qtbot.waitUntil(lambda: win.spectrum.frames_averaged == 4, timeout=WAIT_MS)
        spec = win.spectrum.spectrum
        assert spec is not None and spec.averages == 4
        win.stop()
        qtbot.waitUntil(lambda: not vscope.running, timeout=WAIT_MS)
    finally:
        win.shutdown()


def test_marker_labels_are_staggered_review(panel: SpectrumPanel, qtbot: Any) -> None:
    wf = _wf(1, S200)
    n = 1024
    t = np.arange(n) * wf.dt
    f1 = 3e3
    volts = sum(np.sin(2 * np.pi * f1 * k * t) / k for k in (1, 2, 3, 4))
    panel.resize(900, 800)
    panel.show()
    qtbot.waitExposed(panel)
    panel.update_waveform(wf, np.asarray(volts, dtype=np.float64))
    shown = [lbl for lbl in panel.marker_labels if lbl.isVisible()]
    assert len(shown) >= 3
    assert len({float(lbl.anchor[1]) for lbl in shown}) > 1  # actually staggered
