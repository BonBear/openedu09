"""Reviewer tests: polarity chain raw <-> volts <-> trigger byte, against protocol.md 3.2 / 5.3."""

from __future__ import annotations

import numpy as np
import pytest

from edu09.model import ChannelSettings, Settings, Timebase, TriggerSettings, VoltsPerDiv
from edu09.protocol import tables
from edu09.units import (
    Calibration,
    GndFit,
    counts_to_volts,
    gnd_counts,
    offset_dac,
    sample_interval,
    settings_to_hwconfig,
    time_axis,
    trigger_byte,
)

CAL = Calibration()


def test_default_fit_reproduces_demo_mode_ground() -> None:
    # protocol.md 5.3: demo mode gnd_screen = 2*ypos + 128, with dac = base_ofs (0x80) + ypos.
    for ypos in (-40, 0, 17):
        dac = CAL.base_ofs + ypos
        assert CAL.gnd_fit.k * dac + CAL.gnd_fit.b == 2 * ypos + 128


def test_zero_volt_trigger_matches_edu09_default_trackbar() -> None:
    # EDU09.exe default: TrackBar3.Position = 128 -> TRIG = 255 - 128 = 127, on the ground line.
    ch = ChannelSettings()
    assert trigger_byte(TriggerSettings(level_v=0.0), ch, CAL) == 127
    assert round(gnd_counts(ch, CAL)) == 127
    assert settings_to_hwconfig(Settings(), CAL).trig == 127


@pytest.mark.parametrize("vdiv", list(VoltsPerDiv))
@pytest.mark.parametrize("pos", [-2.0, 0.0, 1.5])
def test_trigger_byte_equals_raw_count_of_level(vdiv: VoltsPerDiv, pos: float) -> None:
    # No double inversion: the trigger byte is the raw sample value at the trigger level, so a
    # sample equal to the trigger byte converts back to (about) level_v.
    ch = ChannelSettings(volts_per_div=vdiv, position_div=pos)
    lo, hi = ch.level_range_v()
    for level in np.linspace(lo * 0.9, hi * 0.9, 7):
        b = trigger_byte(TriggerSettings(level_v=float(level)), ch, CAL)
        v = float(counts_to_volts(np.array([b]), ch, CAL)[0])
        assert v == pytest.approx(level, abs=0.5 * float(vdiv) / CAL.counts_per_div + 1e-12)


def test_positive_voltage_is_larger_raw_and_larger_trigger_byte() -> None:
    ch = ChannelSettings(volts_per_div=VoltsPerDiv.V1)
    up = trigger_byte(TriggerSettings(level_v=1.0), ch, CAL)
    down = trigger_byte(TriggerSettings(level_v=-1.0), ch, CAL)
    assert up > 127 > down
    assert up - 127 == 32 and 127 - down == 32


def test_position_up_lowers_offset_dac_with_positive_k() -> None:
    # gnd_ad = k*dac + b with k > 0 and raw = 255 - ad: moving ground up (larger raw) needs a
    # smaller DAC value.
    assert offset_dac(ChannelSettings(position_div=1.0), CAL) < 0x80
    assert offset_dac(ChannelSettings(position_div=-1.0), CAL) > 0x80
    steep = Calibration(gnd_fit=GndFit(4.0, -384.0))
    assert offset_dac(ChannelSettings(position_div=1.0), steep) == 0x80 - 8


def test_zoomed_timebase_time_axis_uses_true_sample_rate() -> None:
    # 0.1 ms/div samples are the 0.2 ms/div samples (2x display zoom only): identical time axis.
    np.testing.assert_array_equal(
        time_axis(1024, Timebase.US100, tables.TRIGGER_INDEX),
        time_axis(1024, Timebase.US200, tables.TRIGGER_INDEX),
    )
    # ... which spans 2x the nominal 32.768 divisions of 0.1 ms.
    span_div = 1024 * sample_interval(Timebase.US100) / float(Timebase.US100)
    assert span_div == pytest.approx(2 * tables.DIVISIONS_PER_FRAME)
