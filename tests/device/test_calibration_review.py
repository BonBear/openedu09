"""Reviewer tests for T9 calibration: an independent front-end model (not the simulator, not
``units.gnd_counts``) checks the sign conventions, the bytes sent and the reading budget."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from edu09 import units
from edu09.device import calibration as calmod
from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, Settings, Timebase, TriggerMode, VoltsPerDiv
from edu09.protocol import tables


@dataclass
class FakeIO:
    """Physical front end: raw = 255 - (k * (dac - corr[vdiv]) + b) + V * 32 / vdiv, plus noise.

    ``slope_scale`` lets the DAC slope differ per range (offset injected before a gain stage).
    Records every (gain, ofs, flags, tb) the calibration asks for.
    """

    k: float = 2.0
    b: float = -133.6
    corr: dict[float, int] = field(default_factory=lambda: {1.0: 5, 0.1: -9})
    slope_scale: dict[float, float] = field(default_factory=dict)
    volts: float = 0.0
    noise: float = 1.0
    seed: int = 7
    calibration: units.Calibration = field(default_factory=units.Calibration)
    sent: list[tuple[int, int, int, int]] = field(default_factory=list)
    readings: int = 0

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def physical_raw(self, dac: int, vdiv: float, volts: float = 0.0) -> float:
        kk = self.k * self.slope_scale.get(vdiv, 1.0)
        rc = self.corr.get(vdiv, 0)
        # the slope pivots around mid-scale DAC 0x80 (a gain stage after the offset scales it)
        gnd_ad = self.k * 128 + self.b + kk * (dac - rc - 128)
        return 255.0 - gnd_ad + volts * 32.0 / vdiv

    def live_samples(
        self,
        n: int,
        *,
        timebase: Any = None,
        settings: Settings | None = None,
        calibration: units.Calibration | None = None,
        **_: Any,
    ) -> bytes:
        assert settings is not None and calibration is not None
        tb = Timebase.parse(timebase)
        assert not tb.is_ets
        cfg = units.settings_to_hwconfig(
            settings.replace(timebase=tb, trigger_mode=TriggerMode.NONE), calibration
        )
        self.sent.append((cfg.gain1, cfg.ofs1, cfg.flags, cfg.tb))
        self.readings += 1
        vdiv = float(settings.ch1.volts_per_div)
        mean = self.physical_raw(cfg.ofs1, vdiv, self.volts)
        x = np.rint(mean + self.rng.normal(0.0, self.noise, n))
        return np.clip(x, 0, 255).astype(np.uint8).tobytes()


def _zero_volts_reading(io: FakeIO, cal: units.Calibration, vdiv: float, pos: float) -> float:
    """Volts the driver would report for a grounded input (exact, noise-free)."""
    ch = ChannelSettings(VoltsPerDiv.parse(vdiv), position_div=pos)
    dac = units.offset_dac(ch, cal)
    raw = io.physical_raw(dac, vdiv)
    return float(units.counts_to_volts(raw, ch, cal))


@pytest.mark.parametrize(
    ("k", "b", "corr"),
    [
        (2.0, -133.6, {1.0: 5, 0.1: -9}),
        (-1.7, 300.0, {1.0: -12, 0.1: 3}),
        (2.4, -170.0, {1.0: 0, 0.1: 30}),
    ],
)
def test_zero_volts_is_zero_everywhere_independent_model(
    k: float, b: float, corr: dict[float, int]
) -> None:
    io = FakeIO(k=k, b=b, corr=dict(corr))
    cal = calmod._measure(io)  # type: ignore[arg-type]
    assert cal.range_corr == {1.0: corr[1.0], 0.1: corr[0.1]}
    assert cal.gnd_fit.k == pytest.approx(k, abs=0.02)
    for vdiv in (5.0, 2.0, 1.0, 0.5, 0.2, 0.1):
        for pos in (0.0, 1.0, -2.5, 3.0):
            v = _zero_volts_reading(io, cal, vdiv, pos)
            # within one ADC count (the DAC step is ~2 counts, rounding to the closest byte)
            assert abs(v) <= vdiv / 32 + 1e-9, (vdiv, pos, v)


def test_dc_signal_reads_correctly_after_calibration() -> None:
    io = FakeIO()
    cal = calmod._measure(io)  # type: ignore[arg-type]
    for vdiv, volts in ((1.0, 1.5), (0.1, -0.2), (2.0, 3.0)):
        ch = ChannelSettings(VoltsPerDiv.parse(vdiv))
        raw = io.physical_raw(units.offset_dac(ch, cal), vdiv, volts)
        assert float(units.counts_to_volts(raw, ch, cal)) == pytest.approx(volts, abs=vdiv / 32)


def test_range_slope_differs_ground_at_position_zero_still_right() -> None:
    # if the offset DAC acted before a x2 stage at 0.1/1 V/div, only position 0 is guaranteed
    io = FakeIO(slope_scale={1.0: 2.0, 0.1: 2.0})
    cal = calmod._measure(io)  # type: ignore[arg-type]
    for vdiv in (1.0, 0.1):
        # resolution is half a DAC step: 2 counts at 4 counts/step (+ the fit residual)
        assert abs(_zero_volts_reading(io, cal, vdiv, 0.0)) <= 2.1 * vdiv / 32


def test_every_config_byte_is_valid_and_bounded() -> None:
    io = FakeIO(k=-1.7, b=300.0, corr={1.0: -12, 0.1: 3})
    cal = calmod._measure(io)  # type: ignore[arg-type]
    gains = set(tables.GAIN_CODES_CH1.values())
    for gain, ofs, flags, tb in io.sent:
        assert gain in gains and not gain & tables.CH1_AC_BIT  # DC, a known gain code
        assert 0 <= ofs <= 255
        assert flags == 0  # trigger off, rising, CH1, no digital mode
        assert tb == tables.TIMEBASES[float(calmod.CAL_TIMEBASE)].hw_code
    # range searches stay within base +/- RANGE_CORR_LIMIT
    for gain, ofs, _f, _t in io.sent:
        if gain in (tables.GAIN_CODES_CH1[1.0], tables.GAIN_CODES_CH1[0.1]):
            assert abs(ofs - cal.base_ofs) <= calmod.RANGE_CORR_LIMIT
    # run-time budget: ~15 readings x 2 frames x 164 ms at 5 ms/div ~ 5 s on hardware
    assert io.readings <= 20, io.readings


def test_noisy_readings_still_converge_quickly() -> None:
    # 1 count rms per sample -> the 1250-sample mean is ~0.03 counts: deterministic enough
    for seed in range(5):
        io = FakeIO(noise=1.5, seed=seed)
        cal = calmod._measure(io)  # type: ignore[arg-type]
        assert cal.range_corr == {1.0: 5, 0.1: -9}
        assert io.readings <= 20


def test_range_correction_beyond_limit_never_sends_out_of_window_bytes() -> None:
    io = FakeIO(corr={1.0: 60, 0.1: 0})
    with pytest.raises(CalibrationError, match="CALIBRATION FAILED"):
        calmod._measure(io)  # type: ignore[arg-type]
    base_like = [ofs for g, ofs, _f, _t in io.sent if g == tables.GAIN_CODES_CH1[2.0]]
    for g, ofs, _f, _t in io.sent:
        if g == tables.GAIN_CODES_CH1[1.0]:
            assert abs(ofs - base_like[-1]) <= calmod.RANGE_CORR_LIMIT


@pytest.mark.parametrize("b", [-133.6 - 200.0, -133.6 + 200.0, -500.0, 600.0])
def test_extreme_offsets_fail_without_out_of_range_bytes(b: float) -> None:
    io = FakeIO(b=b)
    with pytest.raises(CalibrationError, match="CALIBRATION FAILED"):
        calmod._measure(io)  # type: ignore[arg-type]
    assert all(0 <= ofs <= 255 for _g, ofs, _f, _t in io.sent)
    assert io.readings <= 60
