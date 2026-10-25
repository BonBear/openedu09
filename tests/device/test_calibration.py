"""T9 calibration: offset search, ground fit and range corrections on a simulated front end."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import storage, units
from edu09.device import Scope, measure_calibration
from edu09.device import calibration as calmod
from edu09.errors import CalibrationError, UnsafeOperation
from edu09.model import ChannelSettings, VoltsPerDiv
from edu09.protocol import tables
from edu09.transport.sim import SignalSpec, SimTransport

OPEN_INPUT = "dc:0v:noise=40mv"  # HW: open input raw 131..135 at 1 V/div (~1 count rms)

# Like the real unit (H1): open input at offset 0x80 reads raw ~132.6, not 127
# (ad 122.4 = 2 * 128 + b  ->  b = -133.6), plus per-range offsets.
PHYSICAL = units.Calibration(gnd_fit=units.GndFit(2.0, -133.6), range_corr={1.0: 3, 0.1: -7})


def _open(signal: str = OPEN_INPUT, physical: units.Calibration = PHYSICAL, seed: int = 1) -> Scope:
    t = SimTransport.virtual(signal, seed=seed)
    t.device.calibration = physical
    return Scope.open(t, reconnect_interval_s=0.01)


def _sim(scope: Scope) -> SimTransport:
    t = scope._transport
    assert isinstance(t, SimTransport)
    return t


def _mean_volts(scope: Scope, **settings: Any) -> float:
    scope.configure(**settings)
    return float(np.mean([scope.acquire(trigger_mode="none").volts.mean() for _ in range(3)]))


def test_uncalibrated_sim_reads_offset_like_the_hardware() -> None:
    with _open() as sc:
        sc.configure(volts_per_div=2.0, timebase="2ms", trigger_mode="none")
        raw = sc.acquire().raw.astype(float)
        assert raw.mean() == pytest.approx(132.6, abs=0.3)  # H1 finding 8
        assert _mean_volts(sc, volts_per_div=2.0) == pytest.approx(5.6 * 2 / 32, abs=0.03)


def test_recovers_offset_fit_and_range_corrections() -> None:
    with _open() as sc:
        cal = measure_calibration(sc)
        assert cal.source == "measured"
        assert cal.base_ofs == 131  # 2 * 131 - 133.6 = 128.4 (closest to ad 128)
        assert cal.gnd_fit.k == pytest.approx(2.0, abs=0.02)
        assert cal.gnd_fit.b == pytest.approx(-133.6, abs=1.0)
        assert dict(cal.range_corr) == {1.0: 3, 0.1: -7}
        assert tables.CAL_BASE_OFS_MIN < cal.base_ofs < tables.CAL_BASE_OFS_MAX
        # ground model consistent with the measurement: 0 V -> raw ~126.6 at 2 V/div
        gnd = units.gnd_counts(ChannelSettings(VoltsPerDiv.V2), cal)
        assert gnd == pytest.approx(255 - (2.0 * 131 - 133.6), abs=0.3)


def test_zero_volts_reads_zero_after_calibration(tmp_path: Path) -> None:
    with _open() as sc:
        before = _mean_volts(sc, volts_per_div=1.0, trigger_mode="none")
        assert abs(before) > 0.1
        sc.configure(volts_per_div=0.5, timebase="1ms", trigger_mode="auto")
        cal = sc.calibrate(confirm=True, input_disconnected=True, path=tmp_path / "cal.json")
        assert sc.calibration == cal
        assert storage.load_calibration(tmp_path / "cal.json") == cal
        assert sc.status.calibration_source == "measured"
        for vdiv in (5.0, 2.0, 1.0, 0.5, 0.2, 0.1):
            # within one count: range corrections are whole DAC steps (2 counts each)
            tol = float(vdiv) / 32
            for pos in (0.0, 2.0, -3.0):
                v = _mean_volts(sc, volts_per_div=vdiv, position_div=pos, trigger_mode="none")
                assert abs(v) <= tol, (vdiv, pos, v)


def test_signal_levels_correct_after_calibration() -> None:
    with _open() as sc:
        cal = measure_calibration(sc)
        sc.set_calibration(cal)
        _sim(sc).device.signal = SignalSpec.parse("dc:1.5v:noise=20mv")
        assert _mean_volts(sc, volts_per_div=1.0, trigger_mode="none") == pytest.approx(
            1.5, abs=1 / 32
        )
        assert _mean_volts(sc, volts_per_div=0.5, position_div=-2) == pytest.approx(
            1.5, abs=0.5 / 32
        )


def test_negative_dac_slope_is_handled() -> None:
    # the slope sign is measured, not assumed
    phys = units.Calibration(gnd_fit=units.GndFit(-1.6, 333.0), range_corr={1.0: -4})
    with _open(physical=phys) as sc:
        cal = measure_calibration(sc)
        assert cal.gnd_fit.k == pytest.approx(-1.6, abs=0.03)
        assert cal.range_corr == {1.0: -4, 0.1: 0}
        sc.set_calibration(cal)
        for vdiv in (2.0, 1.0, 0.1):
            v = _mean_volts(sc, volts_per_div=vdiv, position_div=1.5, trigger_mode="none")
            assert abs(v) <= vdiv / 32, (vdiv, v)


def test_mains_hum_is_averaged_out() -> None:
    with _open("sine:50hz:150mvpp:noise=40mv") as sc:
        cal = measure_calibration(sc)
        assert cal.base_ofs == 131
        assert cal.gnd_fit.b == pytest.approx(-133.6, abs=1.0)


def test_restores_user_settings_and_running_state() -> None:
    with _open() as sc:
        sc.configure(volts_per_div=0.5, timebase="1ms", trigger_mode="auto", position_div=1.0)
        user = sc.settings
        sc.run()
        cal = measure_calibration(sc)
        assert sc.running
        assert sc.settings == user
        sc.stop()
        sc.set_calibration(cal)
        wf = sc.acquire()
        assert wf.settings == user
        assert sc.worker.settings == user
        # the driver never sends 0x0D (H2)
        assert tables.OP_LIVE not in _sim(sc).device.received
        # the device was left with the user's configuration
        hw = _sim(sc).device.config
        assert hw == units.settings_to_hwconfig(user, cal)


def test_confirmation_flags_required() -> None:
    with _open() as sc, pytest.raises(UnsafeOperation):
        sc.calibrate(confirm=True, input_disconnected=False, save=False)


def test_signal_on_input_is_detected() -> None:
    with _open("sine:1khz:2vpp") as sc:
        before = sc.calibration
        with pytest.raises(CalibrationError, match="CALIBRATION FAILED: a signal seems"):
            sc.calibrate(confirm=True, input_disconnected=True, save=False)
        assert sc.calibration == before
        assert sc.acquire().settings == sc.settings  # still usable


def test_large_dc_voltage_fails_base_offset_bounds() -> None:
    with _open("dc:6v") as sc, pytest.raises(CalibrationError, match="CALIBRATION FAILED"):
        measure_calibration(sc)


def test_small_dc_voltage_fails_range_correction() -> None:
    # 0.5 V shifts the 2 V/div ground by only 8 counts (base offset still in bounds), but needs
    # 160 counts at 0.1 V/div
    with (
        _open("dc:0.5v:noise=20mv") as sc,
        pytest.raises(CalibrationError, match=r"CALIBRATION FAILED.*(0\.1 V/div|100mV)"),
    ):
        measure_calibration(sc)


@pytest.mark.parametrize("b", [-133.6 - 90.0, -133.6 + 90.0])
def test_ground_far_off_fails(b: float) -> None:
    phys = units.Calibration(gnd_fit=units.GndFit(2.0, b))
    with _open(physical=phys) as sc, pytest.raises(CalibrationError, match="CALIBRATION FAILED"):
        measure_calibration(sc)


def test_dac_without_effect_fails() -> None:
    phys = units.Calibration(gnd_fit=units.GndFit(0.01, 126.0))
    with _open(physical=phys) as sc, pytest.raises(CalibrationError, match="no effect"):
        measure_calibration(sc)


def test_search_dac_converges_to_closest_byte() -> None:
    def make(slope: float, offset: float) -> Callable[[int], calmod.LevelReading]:
        def read(dac: int) -> calmod.LevelReading:
            return calmod.LevelReading(dac, 2.0, slope * dac + offset, 1.0, 0.0)

        return read

    for slope, offset, target in [(-2.0, 389.0, 127.0), (3.0, -260.0, 127.0), (-0.6, 200, 127)]:
        dac, r = calmod._search_dac(make(slope, offset), 128, target, lambda d: 0 <= d <= 255, "t")
        best = min(range(256), key=lambda d: abs(slope * d + offset - target))
        assert dac == best and r.mean == pytest.approx(slope * best + offset)
