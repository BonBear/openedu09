"""Pure plot/format helpers and the ScopePlot/MeasurementPanel widgets."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from edu09 import units
from edu09.gui.fmt import format_si, timebase_text, vdiv_text
from edu09.gui.plot import (
    MeasurementPanel,
    ScopePlot,
    cursor_readout,
    view_x_range,
    y_graticule,
)
from edu09.model import Settings, Timebase, Waveform

pytestmark = pytest.mark.gui


@pytest.mark.parametrize(
    ("value", "unit", "text"),
    [
        (0.00123, "s", "1.23 ms"),
        (2.5e-6, "s", "2.5 µs"),
        (1000.0, "Hz", "1 kHz"),
        (999.5, "Hz", "999.5 Hz"),
        (-0.5, "V", "-500 mV"),
        (0.0, "V", "0 V"),
        (63.2, "%", "63.2 %"),
        (None, "V", "—"),
        (float("nan"), "V", "—"),
    ],
)
def test_format_si(value: Any, unit: str, text: str) -> None:
    assert format_si(value, unit) == text


def test_labels() -> None:
    assert vdiv_text(0.1) == "100 mV/div"
    assert timebase_text(10e-6) == "10 µs/div"
    assert timebase_text(0.5) == "500 ms/div"


def test_cursor_math() -> None:
    r = cursor_readout(-1e-3, 1.5e-3, 2.0, -0.5)
    assert r.dt == pytest.approx(2.5e-3)
    assert r.frequency == pytest.approx(400.0)
    assert r.dv == pytest.approx(-2.5)
    swapped = cursor_readout(1.5e-3, -1e-3, 0.0, 0.0)
    assert swapped.dt == pytest.approx(-2.5e-3) and swapped.frequency == pytest.approx(400.0)
    assert cursor_readout(1.0, 1.0, 0, 0).frequency is None


def test_view_x_range_full_frame_and_zoom() -> None:
    tb = Timebase.MS2
    ti, dt = units.trigger_index(tb), units.sample_interval(tb)
    assert view_x_range(tb) == pytest.approx((-ti * dt, (1023 - ti) * dt))
    # 100 us/div: same hardware rate as 200 us/div, shown as a 2x zoom around the trigger
    z0, z1 = view_x_range(Timebase.US100)
    f0, f1 = view_x_range(Timebase.US100, zoomed=False)
    assert (z0, z1) == pytest.approx((f0 / 2, f1 / 2))
    assert f1 - f0 == pytest.approx(1023 * units.sample_interval(Timebase.US200))
    # in divisions, every row shows about one nominal frame width (32.8 div)
    for t in (Timebase.MS2, Timebase.US100, Timebase.US10, Timebase.US50):
        a, b = view_x_range(t)
        assert (b - a) / t.seconds == pytest.approx(32.74, rel=0.01)


def test_y_graticule_is_screen_anchored() -> None:
    s = Settings().replace(volts_per_div=0.5, position_div=0.5)
    lines = y_graticule(s)
    assert len(lines) == 9
    assert lines[0] == pytest.approx(-2.25) and lines[-1] == pytest.approx(1.75)
    assert np.diff(lines) == pytest.approx([0.5] * 8)


def _wf(raw: Any, settings: Settings, triggered: bool = True) -> Waveform:
    raw = np.asarray(raw, dtype=np.uint8)
    return Waveform(
        seq=7,
        timestamp=datetime.now(UTC),
        raw=raw,
        volts=(raw.astype(float) - 127.0) / 32.0 * float(settings.ch1.volts_per_div),
        dt=units.sample_interval(settings.timebase),
        trigger_index=units.trigger_index(settings.timebase),
        settings=settings,
        calibration_id="t",
        triggered=triggered,
    )


def test_scope_plot_shows_waveform_markers_and_badges(qtbot: Any) -> None:
    s = Settings().replace(volts_per_div=1.0, trigger_level_v=0.5)
    plot = ScopePlot(s)
    qtbot.addWidget(plot)
    assert plot.level_line.value() == pytest.approx(0.5)
    assert plot.item.viewRange()[1] == pytest.approx([-4.0, 4.0])
    raw = np.where(np.arange(1024) % 64 < 32, 160, 96)
    wf = _wf(raw, s)
    plot.show_waveform(wf)
    x, y = plot.curve.getData()
    assert len(x) == 1024 and x[wf.trigger_index] == pytest.approx(0.0)
    assert not plot.clipped_badge.isVisibleTo(plot)
    assert plot.trigger_badge.text() == "Trig'd"
    assert "w7" in plot.info_label.text()
    plot.show_waveform(_wf(np.full(1024, 255), s, triggered=False))
    assert plot.clipped_badge.isVisibleTo(plot)
    assert "untriggered" in plot.trigger_badge.text()
    free = s.replace(trigger_mode="none")
    plot.set_settings(free)
    assert not plot.level_line.isVisible()
    with qtbot.waitSignal(plot.levelDragged) as blocker:
        plot.level_line.sigPositionChangeFinished.emit(plot.level_line)
    assert blocker.args == [pytest.approx(0.5)]


def test_measurement_panel(qtbot: Any) -> None:
    panel = MeasurementPanel()
    qtbot.addWidget(panel)
    dt = 1e-5
    t = np.arange(1024) * dt
    volts = np.where((t * 1000) % 1 < 0.25, 2.0, 0.0)  # 1 kHz, 25 % duty, 0..2 V
    res = panel.update_measurements(volts, dt)
    assert res["frequency"].valid
    assert panel.values["frequency"].text() == "1 kHz"
    assert panel.values["period"].text() == "1 ms"
    assert panel.values["duty"].text().startswith("25")
    assert panel.values["vpp"].text() == "2 V"
    panel.update_measurements(np.zeros(1024), dt)
    assert panel.values["frequency"].text() == "—"
    assert panel.values["frequency"].toolTip()
