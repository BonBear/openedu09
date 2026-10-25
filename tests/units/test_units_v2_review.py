"""Review: randomized properties of the calibration v2 conversions (spec 1.2 / 1.3).

Random *valid* calibrations (gains near the 0.8 / 1.25 limits, knots on the NL_* limits: slope
exactly +-NL_MAX_SLOPE, |e| up to NL_MAX_EXCESS_DIV, r up to NL_MAX_READ_DIV, tiny knot gaps)
must give strictly increasing, continuous and exactly invertible conversions, and the trigger
byte / ADC model must agree with ``volts_to_counts``.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, TriggerSettings, VoltsPerDiv
from edu09.protocol import tables
from edu09.units import (
    Calibration,
    GndFit,
    Knot,
    counts_to_volts,
    deflection_to_volts,
    gnd_counts,
    offset_dac,
    trigger_byte,
    validate_nonlinearity_knots,
    volts_to_counts,
    volts_to_deflection,
    volts_to_raw,
)

RANGES = [float(v) for v in VoltsPerDiv]
_G_LO = math.nextafter(tables.RANGE_GAIN_MIN, 2.0)
_G_HI = math.nextafter(tables.RANGE_GAIN_MAX, 0.0)


def _rand_gain(rng: np.random.Generator) -> float:
    pick = rng.integers(0, 4)
    if pick == 0:
        return _G_LO
    if pick == 1:
        return _G_HI
    return float(rng.uniform(_G_LO, _G_HI))


def _rand_knots(rng: np.random.Generator) -> tuple[Knot, ...]:
    """Random knots that satisfy every validation rule (built segment by segment)."""
    for _ in range(1000):
        n = int(rng.integers(2, tables.NL_MAX_KNOTS + 1))
        r = float(rng.uniform(tables.NL_MIN_READ_DIV, 6.0))
        pts: list[Knot] = [(r, 0.0)]
        e = 0.0
        for _k in range(n - 1):
            gap = float(rng.choice([1e-6, 0.01, rng.uniform(0.05, 2.0)]))
            slope = float(
                rng.choice([tables.NL_MAX_SLOPE, -tables.NL_MAX_SLOPE, rng.uniform(-0.5, 0.5)])
            )
            r_new, e_new = r + gap, e + slope * gap
            if r_new > tables.NL_MAX_READ_DIV or abs(e_new) > tables.NL_MAX_EXCESS_DIV:
                break
            r, e = r_new, e_new
            pts.append((r, e))
        if len(pts) < 2:
            continue
        try:
            return validate_nonlinearity_knots(pts)
        except CalibrationError:  # float rounding of a limit slope: draw again
            continue
    raise AssertionError("could not draw valid knots")


def _rand_cal(rng: np.random.Generator) -> Calibration:
    gp = {v: _rand_gain(rng) for v in RANGES if rng.random() < 0.8}
    gn = {v: _rand_gain(rng) for v in RANGES if rng.random() < 0.8}
    nl = {v: _rand_knots(rng) for v in RANGES if rng.random() < 0.7}
    return Calibration(
        base_ofs=int(rng.integers(100, 160)),
        gnd_fit=GndFit(float(rng.uniform(0.9, 1.1)), float(rng.uniform(-20, 0))),
        range_corr={v: int(rng.integers(-3, 4)) for v in RANGES},
        adc_inverted=bool(rng.random() < 0.3),
        range_gain=gp,
        range_gain_neg=gn,
        neg_nonlinearity=nl,
    )


SEEDS = list(range(40))


@pytest.mark.parametrize("seed", SEEDS)
def test_random_valid_calibration_monotonic_and_invertible(seed: int) -> None:
    rng = np.random.default_rng(seed)
    cal = _rand_cal(rng)
    dev = np.concatenate(
        [np.linspace(-600, 600, 24001), rng.uniform(-600, 600, 2000), [-1e-12, 0.0, 1e-12]]
    )
    dev.sort()
    for vdiv in RANGES:
        v = deflection_to_volts(dev, vdiv, cal)
        assert np.all(np.isfinite(v))
        assert np.all(np.diff(v) > 0) or np.all(np.diff(v)[np.diff(dev) > 0] > 0)
        assert np.all(np.sign(v) == np.sign(dev))  # ground maps to 0 V, sides preserved
        back = volts_to_deflection(v, vdiv, cal)
        np.testing.assert_allclose(back, dev, rtol=1e-12, atol=1e-9)
        # the other direction too, on a volts grid
        volts = np.linspace(-12 * vdiv, 12 * vdiv, 4801)
        d = volts_to_deflection(volts, vdiv, cal)
        assert np.all(np.diff(d) > 0)
        np.testing.assert_allclose(deflection_to_volts(d, vdiv, cal), volts, rtol=1e-12, atol=1e-12)
        # continuity at ground and across every knot (both directions)
        assert abs(float(deflection_to_volts(-1e-9, vdiv, cal))) < 1e-9
        for r, _e in cal.nonlinearity_for(vdiv):
            c = r * cal.counts_per_div / cal.range_gain_neg_for(vdiv)
            lo, hi = deflection_to_volts(np.array([-c - 1e-7, -c + 1e-7]), vdiv, cal)
            assert 0 < hi - lo < 1e-6 * vdiv


@pytest.mark.parametrize("seed", SEEDS[:15])
def test_random_calibration_counts_trigger_and_raw_consistent(seed: int) -> None:
    rng = np.random.default_rng(1000 + seed)
    cal = _rand_cal(rng)
    raw = np.arange(256, dtype=np.float64)
    for vdiv in VoltsPerDiv:
        for pos in (-3.5, -1.25, 0.0, 2.0, 3.5):
            ch = ChannelSettings(vdiv, position_div=pos)
            v = counts_to_volts(raw, ch, cal)
            step = np.diff(v)
            assert np.all(step < 0) if cal.adc_inverted else np.all(step > 0)
            np.testing.assert_allclose(volts_to_counts(v, ch, cal), raw, atol=1e-9)
            # trigger byte == the rounded/clamped raw count of the level (TRIGGER_BYTE_INVERTED
            # False), and volts_to_raw agrees with it
            g = gnd_counts(ch, cal)
            for level in rng.uniform(-8 * float(vdiv), 8 * float(vdiv), 12):
                tb = trigger_byte(TriggerSettings(level_v=float(level)), ch, cal)
                c = float(volts_to_counts(level, ch, cal))
                assert tb == min(max(round(c), 0), 255)
                assert tb == int(volts_to_raw([level], ch, cal)[0]) or abs(c - round(c)) == 0.5
                # the level lies on the expected side of ground
                if not cal.adc_inverted and 0 < c < 255:
                    assert (c > g) == (level > 0)
            # position -> DAC uses the POSITIVE gain only (spec 1.3)
            pos_only = dataclasses.replace(cal, range_gain_neg={}, neg_nonlinearity={})
            assert offset_dac(ch, cal) == offset_dac(ch, pos_only)
            assert gnd_counts(ch, cal) == gnd_counts(ch, pos_only)


def test_volts_to_raw_saturates_monotonically_with_nl() -> None:
    rng = np.random.default_rng(7)
    cal = _rand_cal(rng)
    for vdiv in VoltsPerDiv:
        ch = ChannelSettings(vdiv, position_div=3.5)
        volts = np.linspace(-40, 40, 8001)
        r = volts_to_raw(volts, ch, cal).astype(int)
        d = np.diff(r)
        assert np.all(d <= 0) if cal.adc_inverted else np.all(d >= 0)
        assert {int(r.min()), int(r.max())} == {0, 255}


@pytest.mark.parametrize(
    "knots",
    [
        [(1.0, 0.0), (3.0, 1.0)],  # slope exactly +0.5
        [(1.0, 0.0), (3.0, -1.0)],  # slope exactly -0.5
        [(1.0, 0.0), (5.0, 2.0), (9.0, 0.0)],  # |e| == 2, both slopes at the limit
        [(9.999999, 0.0), (10.0, 0.0000004)],  # tiny last segment at r max
    ],
)
def test_limit_knots_monotonic(knots: list[Knot]) -> None:
    cal = Calibration(range_gain_neg={0.5: _G_LO}, neg_nonlinearity={0.5: knots})
    dev = np.linspace(-1000, 0, 100001)
    v = deflection_to_volts(dev, 0.5, cal)
    assert np.all(np.diff(v) > 0)
    np.testing.assert_allclose(volts_to_deflection(v, 0.5, cal), dev, rtol=1e-12, atol=1e-9)
    # the slope dd/dr never drops below 1 - NL_MAX_SLOPE (well conditioned)
    dv = np.diff(v) / np.diff(dev)
    lin = 0.5 * _G_LO / 32.0
    assert dv.min() >= lin * (1 - tables.NL_MAX_SLOPE) * (1 - 1e-6)
