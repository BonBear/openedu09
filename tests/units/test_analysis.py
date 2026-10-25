"""Tests for edu09.analysis: measurements on analytic signals and decimation."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pytest

from edu09 import analysis
from edu09.analysis import MEASUREMENTS, decimate, find_edges, measure, measure_one, stats

N = 1024
DT = 1e-5  # 100 kS/s -> frame of 10.24 ms


def _t() -> npt.NDArray[np.float64]:
    return np.arange(N) * DT


def sine(freq: float, amp: float = 1.0, offset: float = 0.0, phase: float = 0.3):
    return offset + amp * np.sin(2 * np.pi * freq * _t() + phase)


def square(freq: float, duty: float, lo: float = 0.0, hi: float = 2.0, phase: float = 0.1):
    frac = (freq * _t() + phase) % 1.0
    return np.where(frac < duty, hi, lo).astype(np.float64)


# --------------------------------------------------------------------------- amplitude


def test_sine_amplitude_and_rms() -> None:
    # 1 kHz over 10.24 ms -> 10.24 periods; use an integer number for exact rms
    x = sine(1000 / 1.024, amp=1.5, offset=0.25)  # exactly 10 periods
    m = measure(x, DT)
    assert m["vpp"].value == pytest.approx(3.0, rel=1e-3)
    assert m["vmax"].value == pytest.approx(1.75, rel=1e-3)
    assert m["vmin"].value == pytest.approx(-1.25, rel=1e-3)
    assert m["mean"].value == pytest.approx(0.25, abs=1e-9)
    assert m["ac_rms"].value == pytest.approx(1.5 / math.sqrt(2), rel=1e-6)
    assert m["vrms"].value == pytest.approx(math.sqrt(0.25**2 + 1.5**2 / 2), rel=1e-6)
    assert all(m[k].unit == "V" for k in ("vpp", "vmax", "vmin", "mean", "vrms", "ac_rms"))


def test_measure_returns_all_in_canonical_order() -> None:
    m = measure(sine(1000), DT)
    assert tuple(m) == MEASUREMENTS
    for res in m.values():
        assert res.valid
        assert res.value is not None
        assert res.note is None


def test_measure_subset_order_dedup_and_case() -> None:
    m = measure(sine(1000), DT, ["Period", "vpp", "period"])
    assert tuple(m) == ("period", "vpp")
    assert measure(sine(1000), DT, "vpp").keys() == {"vpp"}


def test_unknown_measurement_and_bad_args() -> None:
    with pytest.raises(ValueError, match="unknown measurement"):
        measure(sine(1000), DT, ["bogus"])
    with pytest.raises(ValueError, match="dt"):
        measure(sine(1000), 0.0)
    with pytest.raises(ValueError, match="dt"):
        measure(sine(1000), float("nan"))
    with pytest.raises(ValueError, match="1-D"):
        measure(np.zeros((2, 2)), DT)


# --------------------------------------------------------------------------- timing


@pytest.mark.parametrize("freq", [250.0, 1000.0, 3333.0, 12000.0])
def test_sine_frequency_and_period(freq: float) -> None:
    m = measure(sine(freq), DT)
    assert m["frequency"].value == pytest.approx(freq, rel=1e-3)
    assert m["period"].value == pytest.approx(1 / freq, rel=1e-3)
    assert m["frequency"].unit == "Hz"
    assert m["period"].unit == "s"


def test_sine_duty_widths_rise_fall() -> None:
    # integer number of periods so the frame mean equals the true offset
    f = 1000.0 / 1.024
    m = measure(sine(f, amp=2.0, offset=1.0), DT)
    assert m["duty"].value == pytest.approx(50.0, abs=0.5)
    assert m["pos_width"].value == pytest.approx(0.5 / f, rel=1e-2)
    assert m["neg_width"].value == pytest.approx(0.5 / f, rel=1e-2)
    # 10-90 % of a sine: 2*asin(0.8)/(2*pi*f)
    expected = 2 * math.asin(0.8) / (2 * math.pi * f)
    assert m["rise_time"].value == pytest.approx(expected, rel=1e-2)
    assert m["fall_time"].value == pytest.approx(expected, rel=1e-2)


@pytest.mark.parametrize("duty", [0.1, 0.25, 0.5, 0.8])
def test_square_frequency_duty(duty: float) -> None:
    f = 900.0
    m = measure(square(f, duty), DT)
    period_samples = 1 / f / DT
    assert m["frequency"].value == pytest.approx(f, rel=2e-3)
    # edges are quantised to one sample
    assert m["duty"].value == pytest.approx(100 * duty, abs=100 * 1.0 / period_samples)
    assert m["pos_width"].value == pytest.approx(duty / f, abs=DT)
    assert m["neg_width"].value == pytest.approx((1 - duty) / f, abs=DT)
    # ideal steps: rise/fall shorter than one sample
    assert 0 < (m["rise_time"].value or 0) <= DT
    assert 0 < (m["fall_time"].value or 0) <= DT


def test_square_with_noise_does_not_double_count_edges() -> None:
    rng = np.random.default_rng(1)
    f = 500.0
    x = square(f, 0.5, lo=-1.0, hi=1.0) + rng.normal(0.0, 0.03, N)
    m = measure(x, DT)
    assert m["frequency"].value == pytest.approx(f, rel=5e-3)
    assert m["duty"].value == pytest.approx(50.0, abs=2.0)


def test_noisy_sine_hysteresis() -> None:
    rng = np.random.default_rng(7)
    x = sine(2000.0) + rng.normal(0.0, 0.02, N)
    assert measure_one(x, DT, "frequency").value == pytest.approx(2000.0, rel=5e-3)


def test_ramp_rise_time() -> None:
    # trapezoid: 0 V, linear ramp over 40 samples to 1 V, hold, ramp down over 20 samples
    seg = np.concatenate(
        [np.zeros(60), np.linspace(0, 1, 41)[1:], np.ones(60), np.linspace(1, 0, 21)[1:]]
    )
    x = np.tile(seg, N // seg.size + 1)[:N]
    m = measure(x, DT)
    assert m["rise_time"].value == pytest.approx(0.8 * 40 * DT, rel=1e-6)
    assert m["fall_time"].value == pytest.approx(0.8 * 20 * DT, rel=1e-6)
    assert m["period"].value == pytest.approx(seg.size * DT, rel=1e-6)


def test_too_few_periods_invalid() -> None:
    # 1.5 periods in the frame
    x = sine(1.5 / (N * DT))
    m = measure(x, DT)
    for name in ("frequency", "period", "duty"):
        assert not m[name].valid
        assert m[name].value is None
        assert m[name].note and "periods" in m[name].note
    assert m["vpp"].valid


def test_exactly_two_periods_valid() -> None:
    x = sine(2.2 / (N * DT), phase=-1.0)
    m = measure(x, DT)
    assert m["frequency"].valid
    assert m["frequency"].value == pytest.approx(2.2 / (N * DT), rel=1e-2)


def test_single_pulse_widths_but_no_frequency() -> None:
    x = np.zeros(N)
    x[300:400] = 1.0
    m = measure(x, DT)
    assert not m["frequency"].valid
    assert m["pos_width"].value == pytest.approx(100 * DT, abs=DT)
    assert not m["neg_width"].valid
    assert m["rise_time"].valid and m["fall_time"].valid


def test_single_step_no_pulse() -> None:
    x = np.where(np.arange(N) < 500, 0.0, 1.0)
    m = measure(x, DT)
    assert not m["pos_width"].valid and m["pos_width"].note == "no complete pulse"
    assert m["rise_time"].valid
    assert not m["fall_time"].valid


# --------------------------------------------------------------------------- edge cases


def test_empty() -> None:
    m = measure(np.array([]), DT)
    for name in MEASUREMENTS:
        assert not m[name].valid
        assert m[name].value is None
        assert m[name].note
    assert m["vpp"].note == "no samples"
    s = stats(np.array([]), DT)
    assert s == dict.fromkeys(("min", "max", "mean", "vpp", "vrms", "frequency_hz"))


def test_single_sample() -> None:
    m = measure([1.0], DT)
    assert m["vpp"].value == 0.0
    assert m["mean"].value == 1.0
    assert m["frequency"].note == "too few samples"


def test_constant_signal() -> None:
    x = np.full(N, 0.7)
    m = measure(x, DT)
    assert m["vpp"].value == 0.0
    assert m["mean"].value == pytest.approx(0.7)
    assert m["vrms"].value == pytest.approx(0.7)
    assert m["ac_rms"].value == pytest.approx(0.0, abs=1e-12)
    for name in ("frequency", "period", "duty", "rise_time", "fall_time", "pos_width", "neg_width"):
        assert not m[name].valid
        assert "Vpp = 0" in (m[name].note or "")


def test_nan_samples() -> None:
    x = sine(1000.0)
    x[10] = np.nan
    m = measure(x, DT)
    assert m["vpp"].valid
    assert m["vpp"].value == pytest.approx(2.0, rel=1e-3)
    assert m["vpp"].note == "1 non-finite samples ignored"
    assert not m["frequency"].valid
    assert m["frequency"].note == "non-finite samples"


def test_all_nan() -> None:
    m = measure(np.full(16, np.nan), DT)
    assert not m["vpp"].valid and m["vpp"].note == "no finite samples"
    assert not m["frequency"].valid


def test_find_edges_none_cases() -> None:
    assert find_edges([]) is None
    assert find_edges(np.ones(10)) is None
    assert find_edges([0.0, np.nan, 1.0]) is None


def test_find_edges_no_spurious_start_edge() -> None:
    # starts high: the first transition seen is a falling edge, no rising edge at index 0
    x = np.concatenate([np.ones(10), np.zeros(10), np.ones(10)])
    e = find_edges(x)
    assert e is not None
    assert e.falling.tolist() == pytest.approx([9 + 1 / 3])  # mean = 2/3, crossing in 9..10
    assert e.rising.tolist() == pytest.approx([19 + 2 / 3])


def test_stats_matches_measure() -> None:
    x = sine(1000.0, amp=1.0, offset=0.5)
    s = stats(x, DT)
    assert set(s) == {"min", "max", "mean", "vpp", "vrms", "frequency_hz"}
    assert s["vpp"] == pytest.approx(2.0, rel=1e-3)
    assert s["frequency_hz"] == pytest.approx(1000.0, rel=1e-3)
    assert stats(np.zeros(N), DT)["frequency_hz"] is None


@dataclass
class _FakeWaveform:
    volts: npt.NDArray[np.float64]
    dt: float


def test_waveform_wrappers() -> None:
    wf = _FakeWaveform(volts=sine(1000.0), dt=DT)
    assert analysis.measure_waveform(wf, ["vpp"])["vpp"].value == pytest.approx(2.0, rel=1e-3)
    assert analysis.stats_waveform(wf)["frequency_hz"] == pytest.approx(1000.0, rel=1e-3)


def test_integer_input_accepted() -> None:
    raw = np.array([0, 255, 0, 255] * 4, dtype=np.uint8)
    m = measure(raw, DT, ["vpp", "frequency"])
    assert m["vpp"].value == 255.0
    assert m["frequency"].value == pytest.approx(1 / (2 * DT))


# --------------------------------------------------------------------------- decimation


def test_decimate_passthrough() -> None:
    x = sine(1000.0)
    d = decimate(x, 2048)
    assert d.factor == 1
    np.testing.assert_array_equal(d.values, x)
    np.testing.assert_array_equal(d.indices, np.arange(N))
    d.values[0] = 99.0  # a copy, not a view
    assert x[0] != 99.0


@pytest.mark.parametrize("max_points", [2, 16, 100, 200, 333, 1023])
def test_minmax_preserves_extrema_and_cap(max_points: int) -> None:
    rng = np.random.default_rng(3)
    x = sine(700.0) + rng.normal(0, 0.1, N)
    x[517] = 5.0  # one-sample glitch
    x[3] = -4.0
    d = decimate(x, max_points)
    assert d.method == "minmax"
    assert d.values.size <= max_points
    assert d.values.max() == 5.0
    assert d.values.min() == -4.0
    assert np.all(np.diff(d.indices) > 0)
    np.testing.assert_array_equal(d.values, x[d.indices])
    # each bucket contributes its own min and max
    for b in range(-(-N // d.factor)):
        seg = x[b * d.factor : (b + 1) * d.factor]
        inb = d.values[(d.indices >= b * d.factor) & (d.indices < (b + 1) * d.factor)]
        assert inb.min() == seg.min() and inb.max() == seg.max()


def test_minmax_time_order_within_bucket() -> None:
    x = np.array([0.0, 3.0, 1.0, -2.0, 5.0, 4.0, -1.0, 2.0])
    d = decimate(x, 4)  # 2 buckets of 4
    assert d.factor == 4
    assert d.indices.tolist() == [1, 3, 4, 6]  # max before min in bucket 0, max before min in 1
    assert d.values.tolist() == [3.0, -2.0, 5.0, -1.0]


def test_minmax_constant_bucket_single_point() -> None:
    x = np.zeros(10)
    d = decimate(x, 4)
    assert d.values.size <= 4
    assert np.all(d.values == 0.0)


def test_minmax_nan_handling() -> None:
    x = np.arange(12, dtype=np.float64)
    x[0:3] = np.nan  # bucket 0 = [nan, nan, nan, 3]
    x[4:8] = np.nan  # bucket 1 all NaN
    d = decimate(x, 6)
    assert d.factor == 4
    assert d.values[0] == 3.0
    assert np.isnan(d.values[1])
    assert d.values[-2:].tolist() == [8.0, 11.0]


def test_stride() -> None:
    x = np.arange(N, dtype=np.float64)
    d = decimate(x, 200, method="stride")
    assert d.factor == 6
    assert d.values.size <= 200
    np.testing.assert_array_equal(d.values, x[::6])
    np.testing.assert_array_equal(d.indices, np.arange(0, N, 6))


def test_decimate_bad_args() -> None:
    with pytest.raises(ValueError):
        decimate(np.zeros(10), 1)
    with pytest.raises(ValueError):
        decimate(np.zeros(10), 0, method="stride")
    with pytest.raises(ValueError, match="method"):
        decimate(np.zeros(10), 4, method="bogus")  # type: ignore[arg-type]
    d = decimate(np.array([]), 4)
    assert d.values.size == 0
