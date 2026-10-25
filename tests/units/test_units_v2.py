"""Calibration data model v2 (spec 1): negative gain, negative nonlinearity, meta."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import numpy as np
import pytest

from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, Coupling, TriggerSettings, VoltsPerDiv
from edu09.protocol import tables
from edu09.transport.sim import UNIT1
from edu09.units import (
    Calibration,
    GndFit,
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

NL05 = ((4.0, 0.0), (5.083, 0.083), (6.315, 0.315))
RAW = np.arange(256, dtype=np.float64)
POSITIONS = (-3.5, 0.0, 3.5)
TYP = Calibration.typical()
INVERTED_UNIT1 = dataclasses.replace(UNIT1, adc_inverted=True)

USER_FILE_CAL = {  # the user's ~/.config/edu09/calibration.json (v1, no gains) on 2026-10-25
    "adc_inverted": False,
    "base_ofs": 134,
    "counts_per_div": 32.0,
    "gnd_fit": {"b": -9.780133333333234, "k": 1.0285999999999993},
    "range_corr": {"0.1": -1, "1": -1},
    "source": "measured",
}


# -- helpers ---------------------------------------------------------------------------------


def _v1_counts_to_volts(raw: Any, ch: ChannelSettings, cal: Calibration) -> Any:
    """The v1 formula (ec50d15), kept here as the bitwise reference."""
    vpc = float(ch.volts_per_div) / cal.effective_counts_per_div(ch.volts_per_div)
    vpc = -vpc if cal.adc_inverted else vpc
    return (np.asarray(raw, dtype=np.float64) - gnd_counts(ch, cal)) * vpc


def _v1_volts_to_counts(volts: Any, ch: ChannelSettings, cal: Calibration) -> Any:
    vpc = float(ch.volts_per_div) / cal.effective_counts_per_div(ch.volts_per_div)
    vpc = -vpc if cal.adc_inverted else vpc
    return np.asarray(volts, dtype=np.float64) / vpc + gnd_counts(ch, cal)


# -- fields, helpers, validation -------------------------------------------------------------


def test_defaults_and_lookups() -> None:
    cal = Calibration()
    assert cal.range_gain_neg == {} and cal.neg_nonlinearity == {} and cal.meta is None
    assert cal.range_gain_neg_for(0.5) == 1.0
    assert cal.nonlinearity_for(0.5) == ()
    g = Calibration(range_gain={0.5: 0.97})
    assert g.range_gain_neg_for(0.5) == 0.97  # falls back to g+
    assert UNIT1.range_gain_neg_for(VoltsPerDiv.V0_5) == 0.9697
    assert UNIT1.nonlinearity_for(VoltsPerDiv.V0_5) == NL05
    assert UNIT1.nonlinearity_for(1.0) == ()
    assert Calibration(neg_nonlinearity=None).neg_nonlinearity == {}


def test_nonlinearity_normalised() -> None:
    cal = Calibration(neg_nonlinearity={"500mV": [[4, 0], [5.083, 0.083]]})
    assert cal.neg_nonlinearity == {0.5: ((4.0, 0.0), (5.083, 0.083))}
    assert all(isinstance(x, float) for k in cal.neg_nonlinearity[0.5] for x in k)


@pytest.mark.parametrize(
    "knots",
    [
        [(4.0, 0.0)],  # < 2 knots
        [(1.0 + i, 0.0) for i in range(tables.NL_MAX_KNOTS + 1)],  # > 8 knots
        [(4.0, 0.01), (5.0, 0.1)],  # e_0 != 0
        [(0.9, 0.0), (5.0, 0.1)],  # r_0 < 1
        [(4.0, 0.0), (4.0, 0.1)],  # not strictly increasing
        [(4.0, 0.0), (5.0, 0.1), (4.5, 0.2)],
        [(4.0, 0.0), (10.5, 0.1)],  # r > 10
        [(4.0, 0.0), (8.0, 1.9), (9.5, 2.1)],  # |e| > 2
        [(4.0, 0.0), (5.0, 0.6)],  # slope > 0.5
        [(4.0, 0.0), (5.0, -0.6)],  # slope < -0.5
        [(4.0, 0.0), (5.0, 0.4), (5.5, 0.7)],  # last (tail) slope 0.6
        [(4.0, 0.0), (5.0, float("nan"))],
        [(4.0, 0.0), (5.0, float("inf"))],
        [(4.0, 0.0), (5.0, True)],
        [(4.0, 0.0), ("5", 0.1)],
        [(4.0, 0.0, 1.0), (5.0, 0.1)],
        "4,0;5,0.1",
        {"r": 4.0},
        [4.0, 5.0],
    ],
)
def test_knot_validation_rules(knots: Any) -> None:
    with pytest.raises(CalibrationError):
        validate_nonlinearity_knots(knots)
    with pytest.raises(CalibrationError, match="neg_nonlinearity"):
        Calibration(neg_nonlinearity={0.5: knots})


def test_knot_validation_limits_inclusive() -> None:
    ok = [(1.0, 0.0), (5.0, 2.0), (10.0, -0.5)]  # slopes 0.5 and -0.5, |e| == 2, r == 10
    assert validate_nonlinearity_knots(ok) == tuple(ok)
    assert len(validate_nonlinearity_knots([(1.0 + i, 0.0) for i in range(8)])) == 8


@pytest.mark.parametrize("key", ["0.3", "version", "x", 7])
def test_knot_keys_must_be_ranges(key: Any) -> None:
    with pytest.raises(CalibrationError, match="neg_nonlinearity key"):
        Calibration(neg_nonlinearity={key: NL05})


@pytest.mark.parametrize("gains", [{0.5: 0.8}, {0.5: 1.25}, {0.5: "1"}, {0.3: 1.0}, [1.0]])
def test_range_gain_neg_validation(gains: Any) -> None:
    with pytest.raises(CalibrationError, match="range_gain_neg"):
        Calibration(range_gain_neg=gains)


def test_with_gains_merges_and_removes() -> None:
    cal = Calibration(range_gain={0.5: 0.97, 1.0: 0.99}, range_gain_neg={0.5: 0.96})
    out = cal.with_gains(
        pos={"0.5": 0.98, 1: None},
        neg={0.2: 0.95},
        nonlinearity={"0.5": [[4.0, 0.0], [5.0, 0.1]]},
        meta={"tool": "t"},
    )
    assert out.range_gain == {0.5: 0.98}
    assert out.range_gain_neg == {0.5: 0.96, 0.2: 0.95}
    assert out.neg_nonlinearity == {0.5: ((4.0, 0.0), (5.0, 0.1))}
    assert out.meta == {"tool": "t"}
    assert cal.range_gain == {0.5: 0.97, 1.0: 0.99}  # original untouched
    kept = out.with_gains(nonlinearity={0.5: None}, neg={0.5: None})
    assert kept.meta == {"tool": "t"}  # meta=... keeps
    assert kept.neg_nonlinearity == {} and kept.range_gain_neg == {0.2: 0.95}
    assert out.with_gains(nonlinearity={0.5: ()}).neg_nonlinearity == {}
    assert out.with_gains(meta=None).meta is None
    with pytest.raises(CalibrationError):
        cal.with_gains(pos={0.5: 2.0})
    with pytest.raises(CalibrationError):
        cal.with_gains(neg={"bogus": 1.0})


def test_without_gain_correction() -> None:
    cal = UNIT1.with_gains(meta={"a": 1})
    raw = cal.without_gain_correction()
    assert raw.range_gain == raw.range_gain_neg == raw.neg_nonlinearity == {}
    assert raw.meta is None
    assert (raw.base_ofs, raw.gnd_fit, raw.range_corr) == (
        cal.base_ofs,
        cal.gnd_fit,
        cal.range_corr,
    )
    assert raw.source == cal.source


def test_meta_validation_and_copy() -> None:
    src: dict[str, Any] = {"tool": "x", "ranges": {0.5: {"pos": 0.97}}}
    cal = Calibration(meta=src)
    src["tool"] = "y"
    assert cal.meta == {"tool": "x", "ranges": {"0.5": {"pos": 0.97}}}
    assert Calibration(meta={}).meta is None
    for bad in ([1], "x", {"a": float("nan")}, {"a": object()}):
        with pytest.raises(CalibrationError, match="meta"):
            Calibration(meta=bad)  # type: ignore[arg-type]
    with pytest.raises(CalibrationError, match="bytes"):
        Calibration(meta={"blob": "x" * tables.CAL_META_MAX_BYTES})


# -- serialisation and identity --------------------------------------------------------------


def test_to_dict_omits_empty_v2_keys() -> None:
    d = TYP.to_dict()
    assert not {"range_gain_neg", "neg_nonlinearity", "meta"} & set(d)
    assert not TYP.has_v2_fields
    d1 = UNIT1.with_gains(meta={"tool": "edu09-calibrate"}).to_dict()
    assert d1["range_gain_neg"]["0.5"] == 0.9697
    assert d1["neg_nonlinearity"] == {
        "0.2": [[4.0, 0.0], [5.069, 0.069], [6.26, 0.26]],
        "0.5": [[4.0, 0.0], [5.083, 0.083], [6.315, 0.315]],
    }
    assert d1["meta"] == {"tool": "edu09-calibrate"}
    back = Calibration.from_dict(json.loads(json.dumps(d1)))
    assert back == UNIT1 and back.meta == {"tool": "edu09-calibrate"}


def test_pinned_calibration_id_of_user_file() -> None:
    cal = Calibration.from_dict(USER_FILE_CAL)
    assert cal.calibration_id == "measured:c9c4b95f56"  # same as before v2 (ec50d15)
    assert Calibration().calibration_id == "default:b778303fc6"
    assert TYP.calibration_id == "typical:29c1bad175"
    assert cal.to_dict() == {**USER_FILE_CAL, "range_gain": {}}


def test_meta_excluded_from_id_equality_and_hash() -> None:
    a = UNIT1.with_gains(meta={"created": "2026-10-25T10:00:00"})
    b = UNIT1.with_gains(meta={"created": "2026-10-26T11:00:00"})
    assert a == b == UNIT1
    assert hash(a) == hash(b)
    assert a.calibration_id == b.calibration_id == UNIT1.calibration_id
    assert UNIT1.calibration_id != UNIT1.without_gain_correction().calibration_id
    # v2 fields do count
    c = UNIT1.with_gains(nonlinearity={0.5: None})
    assert c != UNIT1 and c.calibration_id != UNIT1.calibration_id
    d = UNIT1.with_gains(neg={1.0: 0.99})
    assert d != UNIT1 and d.calibration_id != UNIT1.calibration_id


# -- conversions -----------------------------------------------------------------------------


@pytest.mark.parametrize("cal", [TYP, UNIT1, INVERTED_UNIT1], ids=["typical", "unit1", "inv"])
@pytest.mark.parametrize("vdiv", list(VoltsPerDiv))
@pytest.mark.parametrize("pos", POSITIONS)
def test_roundtrip_and_monotonic_grid(cal: Calibration, vdiv: VoltsPerDiv, pos: float) -> None:
    ch = ChannelSettings(vdiv, position_div=pos)
    v = counts_to_volts(RAW, ch, cal)
    back = volts_to_counts(v, ch, cal)
    np.testing.assert_allclose(back, RAW, rtol=0, atol=1e-9)
    step = np.diff(v)
    assert np.all(step < 0) if cal.adc_inverted else np.all(step > 0)
    # deflection functions are exact inverses on a fine grid too
    dev = np.linspace(-300, 300, 6001)
    np.testing.assert_allclose(
        volts_to_deflection(deflection_to_volts(dev, vdiv, cal), vdiv, cal), dev, atol=1e-9
    )
    assert np.all(np.diff(deflection_to_volts(dev, vdiv, cal)) > 0)


@pytest.mark.parametrize("cal", [Calibration(), TYP, dataclasses.replace(TYP, adc_inverted=True)])
@pytest.mark.parametrize("vdiv", list(VoltsPerDiv))
@pytest.mark.parametrize("pos", POSITIONS)
def test_bitwise_v1_equality_without_v2_fields(
    cal: Calibration, vdiv: VoltsPerDiv, pos: float
) -> None:
    ch = ChannelSettings(vdiv, position_div=pos)
    assert counts_to_volts(RAW, ch, cal).tobytes() == _v1_counts_to_volts(RAW, ch, cal).tobytes()
    volts = np.linspace(-6, 6, 241) * float(vdiv)
    got = volts_to_counts(volts, ch, cal)
    assert got.tobytes() == _v1_volts_to_counts(volts, ch, cal).tobytes()
    s = volts_to_counts(0.3, ch, cal)  # scalar in, numpy scalar out (as v1)
    assert isinstance(s, np.floating) and s == _v1_volts_to_counts(0.3, ch, cal)
    assert isinstance(counts_to_volts(100, ch, cal), np.floating)


def test_continuity_at_ground() -> None:
    for vdiv in VoltsPerDiv:
        eps = 1e-9
        lo, hi = deflection_to_volts(np.array([-eps, eps]), vdiv, UNIT1)
        assert abs(lo) < 1e-9 and abs(hi) < 1e-9 and lo < 0 < hi
        assert deflection_to_volts(0.0, vdiv, UNIT1) == 0.0
        assert volts_to_deflection(0.0, vdiv, UNIT1) == 0.0


def test_unit1_negative_model_values() -> None:
    # 0.5 V/div: 33.0 counts/div down to 4 div, secant 33.55 at 5 div, 34.73 at 6 div below ground
    for d, secant in ((2.0, 32 / 0.9697), (4.0, 32 / 0.9697), (5.0, 33.55), (6.0, 34.73)):
        c = -volts_to_deflection(-d * 0.5, 0.5, UNIT1)
        assert c / d == pytest.approx(secant, abs=0.01)
    # positive side: g+ only
    assert volts_to_deflection(3.0, 0.5, UNIT1) == pytest.approx(6 * 32 / 0.9741)
    # beyond the last knot: extrapolated with the last slope
    r_last, e_last = NL05[-1]
    slope = (NL05[-1][1] - NL05[-2][1]) / (NL05[-1][0] - NL05[-2][0])
    r = 7.5
    c = r * 32 / 0.9697
    v = deflection_to_volts(-c, 0.5, UNIT1)
    assert v == pytest.approx(-(r - (e_last + slope * (r - r_last))) * 0.5)


def test_gnd_coupling_and_position_mapping_unchanged() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5, Coupling.GND, position_div=2.0)
    assert np.all(counts_to_volts(RAW, ch, UNIT1) == 0.0)
    for pos in (-3.5, -1.0, 2.0, 3.5):
        dc = ChannelSettings(VoltsPerDiv.V0_5, position_div=pos)
        pos_only = Calibration(
            base_ofs=UNIT1.base_ofs,
            range_corr=UNIT1.range_corr,
            gnd_fit=UNIT1.gnd_fit,
            range_gain=UNIT1.range_gain,
        )
        assert offset_dac(dc, UNIT1) == offset_dac(dc, pos_only)
        assert gnd_counts(dc, UNIT1) == gnd_counts(dc, pos_only)


def test_trigger_byte_below_ground_uses_nonlinearity() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5, position_div=3.0)
    lin = UNIT1.with_gains(nonlinearity={0.5: None})
    level = -2.9  # 5.8 div below ground
    gnd = gnd_counts(ch, UNIT1)
    trig = TriggerSettings(level_v=level)
    b_nl = trigger_byte(trig, ch, UNIT1)
    b_lin = trigger_byte(trig, ch, lin)
    assert b_lin == round(gnd - 5.8 * 32 / 0.9697)
    assert b_nl == round(float(volts_to_counts(level, ch, UNIT1)))
    assert b_nl < b_lin - 5  # expansion: ~8 counts further below ground
    # above ground (and within 4 div below) identical
    for lv in (1.0, -1.5):
        t = TriggerSettings(level_v=lv)
        assert trigger_byte(t, ch, UNIT1) == trigger_byte(t, ch, lin)
    assert int(volts_to_raw([level], ch, UNIT1)[0]) == b_nl


def test_no_nl_in_other_ranges_and_inverted_sign() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5, position_div=3.0)
    v = np.array([-2.9, -1.0, 1.0])
    a = volts_to_counts(v, ch, UNIT1) - gnd_counts(ch, UNIT1)
    b = volts_to_counts(v, ch, INVERTED_UNIT1) - gnd_counts(ch, INVERTED_UNIT1)
    np.testing.assert_allclose(a, -b, atol=1e-12)
    cal = Calibration(gnd_fit=GndFit(1.03, -9.8), range_gain_neg={1.0: 0.98})
    one = ChannelSettings(VoltsPerDiv.V1)
    c = volts_to_counts(np.array([-1.0, 1.0]), one, cal) - gnd_counts(one, cal)
    assert c[0] == pytest.approx(-32 / 0.98) and c[1] == pytest.approx(32.0)
