"""Tests for edu09.units: calibration, counts <-> volts, offset DAC, trigger byte, hwconfig."""

from __future__ import annotations

import json

import numpy as np
import pytest

from edu09.errors import CalibrationError
from edu09.model import (
    ChannelSettings,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    TriggerSettings,
    VoltsPerDiv,
)
from edu09.protocol import tables
from edu09.protocol.commands import HwConfig, encode_config
from edu09.units import (
    Calibration,
    GndFit,
    counts_to_volts,
    expected_acq_mode,
    flags_byte,
    gnd_counts,
    offset_dac,
    sample_interval,
    settings_to_hwconfig,
    time_axis,
    trigger_byte,
    volts_to_counts,
    volts_to_raw,
)

CAL = Calibration()


# -- calibration ------------------------------------------------------------------------------


def test_default_calibration() -> None:
    assert CAL.base_ofs == 0x80
    assert CAL.gnd_fit == GndFit(2.0, -128.0)
    assert CAL.counts_per_div == 32
    assert CAL.adc_inverted is tables.ADC_INVERTED
    assert CAL.source == "default"
    assert CAL.calibration_id.startswith("default:")
    assert hash(CAL) == hash(Calibration())


def test_calibration_dict_roundtrip_and_id() -> None:
    cal = Calibration(
        base_ofs=0x7C, range_corr={0.1: 3, 1: -2}, gnd_fit=GndFit(1.9, -110.0), source="measured"
    )
    assert cal.range_corr == {0.1: 3, 1.0: -2}
    d = json.loads(json.dumps(cal.to_dict()))
    assert d["range_corr"] == {"0.1": 3, "1": -2}
    back = Calibration.from_dict(d)
    assert back == cal
    assert back.calibration_id == cal.calibration_id != CAL.calibration_id
    assert Calibration.from_dict({}) == CAL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_ofs": 256},
        {"base_ofs": 1.5},
        {"range_corr": {0.3: 1}},
        {"range_corr": {0.1: 999}},
        {"gnd_fit": (0.0, 1.0)},
        {"gnd_fit": "x"},
        {"counts_per_div": 0},
        {"source": "guess"},
        {"adc_inverted": 1},
    ],
)
def test_calibration_rejects(kwargs: dict[str, object]) -> None:
    with pytest.raises(CalibrationError):
        Calibration(**kwargs)  # type: ignore[arg-type]


def test_calibration_from_dict_rejects_unknown() -> None:
    with pytest.raises(CalibrationError, match="unknown"):
        Calibration.from_dict({"foo": 1})
    with pytest.raises(CalibrationError):
        Calibration.from_dict({"gnd_fit": {"k": 2.0}})


def test_gnd_fit_from_points() -> None:
    pts = [(100.0, 2 * 100 - 128.0), (128.0, 128.0), (150.0, 2 * 150 - 128.0)]
    fit = GndFit.from_points(pts)
    assert fit.k == pytest.approx(2.0)
    assert fit.b == pytest.approx(-128.0)
    with pytest.raises(CalibrationError):
        GndFit.from_points(pts[:2])
    with pytest.raises(CalibrationError):
        GndFit.from_points([(1, 1), (1, 2), (3, 3)])


# -- vertical ---------------------------------------------------------------------------------


def test_ground_level_and_dac_default() -> None:
    ch = ChannelSettings()
    assert offset_dac(ch, CAL) == 0x80
    assert gnd_counts(ch, CAL) == pytest.approx(127.0)  # 255 - (2*128 - 128)


@pytest.mark.parametrize("vdiv", list(VoltsPerDiv))
@pytest.mark.parametrize("pos", [-3.0, 0.0, 2.5])
@pytest.mark.parametrize("inverted", [False, True])
def test_volts_roundtrip(vdiv: VoltsPerDiv, pos: float, inverted: bool) -> None:
    cal = Calibration(adc_inverted=inverted, gnd_fit=GndFit(1.8, -100.0), base_ofs=0x84)
    ch = ChannelSettings(volts_per_div=vdiv, position_div=pos)
    v = np.linspace(-2, 2, 33) * float(vdiv)
    counts = volts_to_counts(v, ch, cal)
    np.testing.assert_allclose(counts_to_volts(counts, ch, cal), v, atol=1e-12)
    # one division = counts_per_div counts, polarity per flag
    step = volts_to_counts(float(vdiv), ch, cal) - volts_to_counts(0.0, ch, cal)
    assert float(step) == pytest.approx(-32.0 if inverted else 32.0)


def test_position_moves_ground_by_divisions() -> None:
    for inverted in (False, True):
        cal = Calibration(adc_inverted=inverted)
        g0 = gnd_counts(ChannelSettings(), cal)
        g1 = gnd_counts(ChannelSettings(position_div=1.0), cal)
        assert g1 - g0 == pytest.approx(-32.0 if inverted else 32.0)
        # +1 div up == 0 V now reads like -1 div used to (relative to the old ground)
        v = counts_to_volts(np.array([g1]), ChannelSettings(), cal)
        assert float(v[0]) == pytest.approx(2.0)  # 1 div at 2 V/div


def test_offset_dac_range_corr_and_clamp() -> None:
    cal = Calibration(base_ofs=0xF0, range_corr={0.1: 5})
    ch = ChannelSettings(volts_per_div=VoltsPerDiv.V0_1)
    assert offset_dac(ch, cal) == 0xF5
    # range correction does not move the computed ground (protocol.md 5.3 formula)
    assert gnd_counts(ch, cal) == gnd_counts(ChannelSettings(), cal)
    assert offset_dac(ChannelSettings(volts_per_div=VoltsPerDiv.V0_1, position_div=-4), cal) == 255
    low = Calibration(base_ofs=0x10)
    assert offset_dac(ChannelSettings(position_div=4), low) == 0


def test_gnd_coupling_gives_zero_volts() -> None:
    ch = ChannelSettings(coupling=Coupling.GND)
    out = counts_to_volts(np.array([0, 100, 255], dtype=np.uint8), ch, CAL)
    assert out.dtype == np.float64
    assert np.all(out == 0.0)


def test_volts_to_raw_saturates() -> None:
    ch = ChannelSettings(volts_per_div=VoltsPerDiv.V1)
    raw = volts_to_raw([-100.0, 0.0, 1.0, 100.0], ch, CAL)
    assert raw.dtype == np.uint8
    assert raw.tolist() == [0, 127, 159, 255]


# -- trigger / flags --------------------------------------------------------------------------


def test_trigger_byte() -> None:
    ch = ChannelSettings(volts_per_div=VoltsPerDiv.V1)
    assert trigger_byte(TriggerSettings(level_v=0.0), ch, CAL) == 127
    assert trigger_byte(TriggerSettings(level_v=1.0), ch, CAL) == 159
    assert trigger_byte(TriggerSettings(level_v=4.0), ch, CAL) == 255  # clamped
    assert trigger_byte(TriggerSettings(level_v=-4.0), ch, CAL) == 0


def test_flags_byte() -> None:
    assert flags_byte(TriggerSettings()) == tables.FLAG_TRIG_ENABLE
    assert flags_byte(TriggerSettings(mode=TriggerMode.NORMAL)) == tables.FLAG_TRIG_ENABLE
    assert flags_byte(TriggerSettings(mode=TriggerMode.NONE)) == 0
    f = flags_byte(TriggerSettings(mode=TriggerMode.NONE, edge=TriggerEdge.FALLING))
    assert f == tables.FLAG_EDGE_FALLING


# -- settings_to_hwconfig ---------------------------------------------------------------------


def test_hwconfig_matches_probe_example() -> None:
    """protocol.md 10.3: CH1 1 V/div DC, offset 0x80, 2 ms/div, trigger on rising.

    Differences from the hand-written probe example: CH2 uses the neutral 0x29 (not 0x04), and
    with the default ground fit 0 V maps to count 127, so use the level that gives 0x80.
    """
    s = Settings().replace(volts_per_div=1.0, trigger_level_v=1.0 / 32)
    cfg = settings_to_hwconfig(s, CAL)
    assert cfg == HwConfig(
        gain1=0x0A, gain2=0x29, ofs1=0x80, ofs2=0x80, trig=0x80, tb=0x08, flags=0x02
    )
    assert encode_config(cfg)[:10] == bytes.fromhex("0E 80 07 0A 29 80 80 80 08 02")


def test_hwconfig_coupling_and_timebase() -> None:
    ac = settings_to_hwconfig(Settings().replace(coupling="ac", volts_per_div=0.5), CAL)
    assert ac.gain1 == 0x01 | tables.CH1_AC_BIT
    gnd = settings_to_hwconfig(Settings().replace(coupling="gnd", volts_per_div=0.5), CAL)
    assert gnd.gain1 == 0x01  # GND is DC on the wire
    for tb in Timebase:
        mode = "auto" if tb.is_ets else "none"
        cfg = settings_to_hwconfig(Settings().replace(timebase=tb, trigger_mode=mode), CAL)
        assert cfg.tb == tables.TIMEBASES[float(tb)].hw_code
        assert expected_acq_mode(tb) == tables.TIMEBASES[float(tb)].mode
        assert (expected_acq_mode(tb) != tables.MODE_REALTIME) == tb.is_ets
    for vdiv in VoltsPerDiv:
        cfg = settings_to_hwconfig(Settings().replace(volts_per_div=vdiv), CAL)
        assert cfg.gain1 == tables.GAIN_CODES_CH1[float(vdiv)]


def test_every_enum_member_has_a_table_entry() -> None:
    assert {float(v) for v in VoltsPerDiv} == set(tables.GAIN_CODES_CH1)
    assert {float(t) for t in Timebase} == set(tables.TIMEBASES)


# -- time -------------------------------------------------------------------------------------


def test_time_axis() -> None:
    t = time_axis(1024, Timebase.MS2, 258)
    dt = sample_interval(Timebase.MS2)
    assert dt == pytest.approx(1 / 15_625)
    assert t[258] == 0.0
    assert t[0] == pytest.approx(-258 * dt)
    assert t.dtype == np.float64 and t.shape == (1024,)
    assert time_axis(4, Timebase.US50, None)[0] == 0.0
    assert sample_interval(Timebase.US50) == pytest.approx(1 / 625_000)
    with pytest.raises(ValueError):
        time_axis(-1, Timebase.MS2, 0)


@pytest.mark.parametrize(
    ("tb", "expected"),
    [(Timebase.MS2, 255), (Timebase.US100, 255), (Timebase.US50, 248), (Timebase.US20, 253),
     (Timebase.US10, 253)],
)  # fmt: skip
def test_trigger_index_per_mode_hw_verified(tb: Timebase, expected: int) -> None:
    from edu09.units import trigger_index

    assert trigger_index(tb) == expected
