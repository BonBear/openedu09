"""Per-range gain calibration (``Calibration.range_gain``); v2 fields: test_units_v2.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import storage
from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, Settings, TriggerSettings, VoltsPerDiv
from edu09.protocol import tables
from edu09.units import (
    Calibration,
    GndFit,
    counts_to_volts,
    gnd_counts,
    offset_dac,
    settings_to_hwconfig,
    trigger_byte,
    volts_to_counts,
    volts_to_raw,
)

GAINS = {0.1: 0.987, 0.2: 0.975, 0.5: 0.974, 1.0: 0.996, 2.0: 0.981, 5.0: 0.979}
CAL_G = Calibration(range_gain=GAINS)


def test_default_gain_is_one() -> None:
    cal = Calibration()
    assert cal.range_gain == {}
    for v in VoltsPerDiv:
        assert cal.range_gain_for(v) == 1.0
        assert cal.effective_counts_per_div(v) == 32.0


def test_range_gain_keys_parsed_and_lookup() -> None:
    cal = Calibration(range_gain={"0.5": 0.974, "100mV": 0.987, 2: 1.01})
    assert cal.range_gain == {0.5: 0.974, 0.1: 0.987, 2.0: 1.01}
    assert cal.range_gain_for(VoltsPerDiv.V0_5) == 0.974
    assert cal.range_gain_for(1.0) == 1.0
    assert cal.effective_counts_per_div(0.5) == pytest.approx(32 / 0.974)


@pytest.mark.parametrize(
    "gains",
    [
        {0.5: 0.8},
        {0.5: 1.25},
        {0.5: 0.0},
        {0.5: -1.0},
        {0.5: float("nan")},
        {0.5: float("inf")},
        {0.5: True},
        {0.5: "0.98"},
        {0.3: 0.98},
        [(0.5, 0.98)],
    ],
)
def test_range_gain_validation(gains: Any) -> None:
    with pytest.raises(CalibrationError):
        Calibration(range_gain=gains)


def test_range_gain_bounds_exclusive_inside() -> None:
    Calibration(range_gain={0.5: 0.8001, 1: 1.2499})


def test_gain_excluded_from_hash_but_compared() -> None:
    assert hash(CAL_G) == hash(Calibration())
    assert Calibration() != CAL_G


def test_dict_roundtrip_and_id_include_gain() -> None:
    d = json.loads(json.dumps(CAL_G.to_dict()))
    assert d["range_gain"] == {
        "0.1": 0.987,
        "0.2": 0.975,
        "0.5": 0.974,
        "1": 0.996,
        "2": 0.981,
        "5": 0.979,
    }
    assert "neg_nonlinearity" not in d
    back = Calibration.from_dict(d)
    assert back == CAL_G
    assert back.calibration_id == CAL_G.calibration_id != Calibration().calibration_id
    other = Calibration(range_gain={**GAINS, 0.5: 0.975})
    assert other.calibration_id != CAL_G.calibration_id


def test_old_json_without_gain_loads(tmp_path: Path) -> None:
    old = {
        "version": 1,
        "calibration": {
            "adc_inverted": False,
            "base_ofs": 134,
            "counts_per_div": 32.0,
            "gnd_fit": {"b": -9.8, "k": 1.03},
            "range_corr": {"0.1": -1, "1": -1},
            "source": "measured",
        },
    }
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps(old), encoding="utf-8")
    cal = storage.load_calibration(p)
    assert cal.range_gain == {}
    assert cal.neg_nonlinearity == {}
    assert cal.range_gain_for(0.5) == 1.0
    # save -> load keeps the (now explicit, empty) range_gain
    storage.save_calibration(cal.with_range_gain({"0.5": 0.974}), p)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["calibration"]["range_gain"] == {"0.5": 0.974}
    assert storage.load_calibration(p).range_gain == {0.5: 0.974}


def test_unknown_keys_still_rejected_and_legacy_opaque_nl_rejected(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="unknown"):
        Calibration.from_dict({"bogus": 1})
    legacy = {"version": 1, "points": {"0.5": [[-4.0, 1.06], [-3.0, 1.02]]}, "note": "x"}
    with pytest.raises(CalibrationError, match="neg_nonlinearity"):
        Calibration.from_dict({"range_gain": {"1": 0.996}, "neg_nonlinearity": legacy})
    # null is "absent"
    assert Calibration.from_dict({"neg_nonlinearity": None}).neg_nonlinearity == {}


@pytest.mark.parametrize("bad", [[1, 2], "x", 3, {"a": float("nan")}, {"a": object()}])
def test_neg_nonlinearity_must_be_knot_mapping(bad: Any) -> None:
    with pytest.raises(CalibrationError, match="neg_nonlinearity"):
        Calibration(neg_nonlinearity=bad)


def test_neg_nonlinearity_is_copied() -> None:
    src: dict[str, Any] = {"0.5": [[4.0, 0.0], [5.0, 0.1]]}
    cal = Calibration(neg_nonlinearity=src)
    src["0.5"].append([6.0, 0.3])
    assert cal.neg_nonlinearity == {0.5: ((4.0, 0.0), (5.0, 0.1))}


def test_with_range_gain_merges() -> None:
    cal = Calibration(range_gain={0.5: 0.97}).with_range_gain({"1": 0.99, 0.5: 0.98})
    assert cal.range_gain == {0.5: 0.98, 1.0: 0.99}
    with pytest.raises(CalibrationError):
        cal.with_range_gain({1.0: 2.0})


def test_typical_includes_measured_gains() -> None:
    cal = Calibration.typical()
    assert cal.range_gain == GAINS == tables.TYPICAL_RANGE_GAIN
    assert cal.effective_counts_per_div(0.5) == pytest.approx(32.854, abs=1e-3)


# -- conversions ------------------------------------------------------------------------------


@pytest.mark.parametrize("vdiv", list(VoltsPerDiv))
@pytest.mark.parametrize("pos", [-3.0, 0.0, 2.5])
def test_counts_volts_roundtrip_with_gain(vdiv: VoltsPerDiv, pos: float) -> None:
    cal = Calibration(gnd_fit=GndFit(1.03, -9.8), base_ofs=134, range_gain=GAINS)
    ch = ChannelSettings(vdiv, position_div=pos)
    v = np.linspace(-1.5, 1.5, 31) * float(vdiv)
    c = volts_to_counts(v, ch, cal)
    np.testing.assert_allclose(counts_to_volts(c, ch, cal), v, atol=1e-12)
    # one true division is 32 / g counts
    g = GAINS[float(vdiv)]
    one = volts_to_counts(np.array([0.0, float(vdiv)]), ch, cal)
    assert one[1] - one[0] == pytest.approx(32 / g)


def test_gain_scales_volts() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5)
    gnd = gnd_counts(ch, CAL_G)
    assert gnd == gnd_counts(ch, Calibration())  # gain does not move the ground at position 0
    raw = np.array([gnd + 96.0])
    plain = counts_to_volts(raw, ch, Calibration())[0]
    assert plain == pytest.approx(1.5)
    assert counts_to_volts(raw, ch, CAL_G)[0] == pytest.approx(1.5 * 0.974)


def test_volts_to_raw_with_gain() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5)
    gnd = gnd_counts(ch, CAL_G)
    raw = volts_to_raw([1.5], ch, CAL_G)
    assert int(raw[0]) == round(gnd + 1.5 / 0.5 * 32 / 0.974)


def test_trigger_byte_with_gain() -> None:
    ch = ChannelSettings(VoltsPerDiv.V0_5)
    trig = TriggerSettings(level_v=1.5)
    gnd = gnd_counts(ch, CAL_G)
    expect = round(gnd + 3 * 32 / 0.974)  # 98.6 counts above ground, not 96
    assert trigger_byte(trig, ch, CAL_G) == expect
    assert trigger_byte(trig, ch, Calibration()) == round(gnd + 96)
    assert expect != round(gnd + 96)
    s = Settings(ch1=ch, trigger=trig)
    assert settings_to_hwconfig(s, CAL_G).trig == expect


def test_position_uses_effective_counts_per_div() -> None:
    # keeps the ADC window centred on screen: ground at +2 div is 2 * 32 / g counts above mid
    cal = Calibration(range_gain={2.0: 0.9})
    up = ChannelSettings(VoltsPerDiv.V2, position_div=2.0)
    steps = Calibration().base_ofs - offset_dac(up, cal)
    assert steps == round(2 * 32 / 0.9 / 2.0)  # default model slope k = 2
    assert Calibration().base_ofs - offset_dac(up, Calibration()) == 32
