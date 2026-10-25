"""Gain / negative-nonlinearity fit of the calibration wizard (spec 3.5)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from edu09.calwizard.fit import (
    NL_KNEE_DIV,
    NL_MIN_EXCESS_DIV,
    FitResult,
    GainPoint,
    fit_calibration,
)
from edu09.model import VoltsPerDiv
from edu09.protocol import tables
from edu09.transport.sim import UNIT1
from edu09.units import Calibration, GndFit, deflection_to_volts, volts_to_deflection

RANGES = [float(v) for v in VoltsPerDiv]
BASE = Calibration(base_ofs=135, gnd_fit=GndFit(1.031, -9.5), range_corr={1.0: -1})
CPD = 32.0


def _read(v_ref: float, vdiv: float, phys: Calibration = UNIT1, noise: float = 0.0) -> float:
    """What the uncorrected scope reads (gain 1, no NL) for a true level on ``phys``."""
    return float(volts_to_deflection(v_ref, vdiv, phys)) * vdiv / CPD + noise


def _pt(vdiv: float, div: float, *, phys: Calibration = UNIT1, **kw: Any) -> GainPoint:
    noise = kw.pop("noise", 0.0)
    v_ref = div * vdiv
    return GainPoint(vdiv, kw.pop("pos", 0.0), v_ref, _read(v_ref, vdiv, phys, noise), **kw)


def _standard_points(rng: np.random.Generator | None = None) -> list[GainPoint]:
    pts: list[GainPoint] = []
    for vdiv in RANGES:
        for div in (5.0, 2.5, -3.0, -2.0, -5.0, -6.0):
            noise = 0.0 if rng is None else float(rng.normal(0, 0.05)) * vdiv / CPD
            pts.append(_pt(vdiv, div, noise=noise, setup_id="S02"))
    return pts


# previous calibration of the fits: typical offsets with one unit's gains (a saved file could have)
PREVIOUS = Calibration.typical().with_range_gain(tables.TYPICAL_RANGE_GAIN)


def _fit(points: list[GainPoint], **kw: Any) -> FitResult:
    kw.setdefault("previous", PREVIOUS)
    return fit_calibration(BASE, points, **kw)


# -- recovery ---------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [None, 1, 2])
def test_recovers_unit1(seed: int | None) -> None:
    rng = None if seed is None else np.random.default_rng(seed)
    res = _fit(_standard_points(rng))
    cal = res.calibration
    for vdiv in RANGES:
        rf = res.ranges[vdiv]
        assert rf.status == "fitted" and (rf.n_pos, rf.n_neg, rf.n_nl) == (2, 2, 2)
        assert cal.range_gain_for(vdiv) == pytest.approx(UNIT1.range_gain_for(vdiv), abs=0.001)
        assert cal.range_gain_neg_for(vdiv) == pytest.approx(
            UNIT1.range_gain_neg_for(vdiv), abs=0.002
        )
        # excess at 6 div below ground: fitted model vs physical, in divisions
        c_true = -float(volts_to_deflection(-6 * vdiv, vdiv, UNIT1))
        v_fit = float(deflection_to_volts(-c_true, vdiv, cal))
        assert v_fit / vdiv == pytest.approx(-6.0, abs=0.02)
        assert rf.after_err_pct_max is not None and rf.after_err_pct_max < 0.3
    assert len(cal.nonlinearity_for(0.5)) == 3 and len(cal.nonlinearity_for(0.2)) == 3
    for vdiv in (0.1, 1.0, 2.0, 5.0):  # no expansion in the physical model -> no knots
        assert cal.nonlinearity_for(vdiv) == ()
    if seed is None:
        knots = cal.nonlinearity_for(0.5)
        assert knots[0] == (NL_KNEE_DIV, 0.0)
        assert knots[1] == pytest.approx((5.083, 0.083), abs=2e-3)
        assert knots[2] == pytest.approx((6.315, 0.315), abs=2e-3)
        assert res.warnings == ()
    # offsets come from base, source is measured
    assert (cal.base_ofs, cal.gnd_fit, cal.range_corr) == (BASE.base_ofs, BASE.gnd_fit, {1.0: -1})
    assert cal.source == "measured"


def test_single_point_range_exact() -> None:
    p = GainPoint(1.0, -2.0, 4.0, 4.0 / 0.99)
    res = _fit([p], uncalibrated="unity")
    rf = res.ranges[1.0]
    assert rf.gain_pos == pytest.approx(0.99, abs=1e-5) and rf.gain_neg is None
    assert (rf.n_pos, rf.n_neg, rf.n_nl, rf.status) == (1, 0, 0, "fitted")
    assert rf.after_err_pct_max == pytest.approx(0.0, abs=1e-3)
    assert rf.before_err_pct_max == pytest.approx(100 * (0.996 / 0.99 - 1), abs=1e-3)
    neg = _fit([GainPoint(0.5, 1.5, -1.5, -1.5 / 0.97)], uncalibrated="unity").ranges[0.5]
    assert neg.gain_neg == pytest.approx(0.97, abs=1e-5) and neg.gain_pos is None


def test_classified_by_true_deflection() -> None:
    # a "gain_neg" point that is really 4.5 div below ground is a nonlinearity point
    pts = [_pt(0.5, 5.0), _pt(0.5, -3.0), _pt(0.5, -4.5), _pt(0.5, -6.0)]
    rf = _fit(pts).ranges[0.5]
    assert (rf.n_pos, rf.n_neg, rf.n_nl) == (1, 1, 2)
    res = _fit(pts, knee_div=5.0)
    assert (res.ranges[0.5].n_neg, res.ranges[0.5].n_nl) == (2, 1)


# -- nonlinearity edge cases ------------------------------------------------------------------


def test_nl_below_threshold_gives_no_knots() -> None:
    lin = UNIT1.with_gains(nonlinearity={0.5: None})
    pts = [_pt(0.5, d, phys=lin) for d in (5.0, -3.0, -5.0, -6.0)]
    rf = _fit(pts).ranges[0.5]
    assert rf.neg_knots == () and rf.n_nl == 2 and rf.warnings == ()
    # just above the threshold: knots stored
    bent = lin.with_gains(nonlinearity={0.5: [(4.0, 0.0), (6.0, 2 * NL_MIN_EXCESS_DIV)]})
    pts = [_pt(0.5, d, phys=bent) for d in (5.0, -3.0, -6.0)]
    assert len(_fit(pts).ranges[0.5].neg_knots) == 2


def test_nl_invalid_dropped_with_warning_gains_kept() -> None:
    g = 0.97
    # 4.2 div true, read 4.5 div: slope 0.3 / 0.5 = 0.6 > NL_MAX_SLOPE
    pts = [
        GainPoint(0.5, 0, 2.5, 2.5 / 0.975),
        GainPoint(0.5, 0, -1.5, -1.5 / g),
        GainPoint(0.5, 0, -2.1, -4.5 * 0.5 / g),
    ]
    res = _fit(pts)
    rf = res.ranges[0.5]
    assert rf.neg_knots == ()
    assert rf.gain_neg == pytest.approx(g, abs=1e-5) and rf.gain_pos == pytest.approx(0.975)
    assert any("nonlinearity not stored" in w and "slope" in w for w in rf.warnings)
    assert any(w.startswith("0.5 V/div: negative nonlinearity not stored") for w in res.warnings)


def test_nl_needs_negative_gain() -> None:
    pts = [_pt(0.5, 5.0), _pt(0.5, -6.0)]
    rf = _fit(pts).ranges[0.5]
    assert rf.neg_knots == () and rf.gain_neg is None
    assert any("nonlinearity not fitted" in w for w in rf.warnings)


def test_nl_points_close_together_are_averaged() -> None:
    pts = [_pt(0.5, d) for d in (5.0, -3.0, -5.95, -6.05, -6.1)]
    knots = _fit(pts).ranges[0.5].neg_knots
    assert len(knots) == 2  # knee + one merged knot
    assert knots[0] == (4.0, 0.0)
    r_mean = float(np.mean([-_read(d * 0.5, 0.5) / 0.5 * 0.9697 for d in (-5.95, -6.05, -6.1)]))
    assert knots[1][0] == pytest.approx(r_mean, abs=1e-3)


def test_first_knot_below_knee_for_close_points() -> None:
    pts = [_pt(0.5, d) for d in (5.0, -3.0)] + [GainPoint(0.5, 0, -2.1, -4.3 * 0.5 / 0.9697)]
    knots = _fit(pts).ranges[0.5].neg_knots
    assert knots[0][0] == pytest.approx(4.3 - 0.5)  # min(knee, r_1 - 0.5)


# -- policies ---------------------------------------------------------------------------------


PREV = Calibration(
    range_gain={0.5: 0.96, 1.0: 0.99, 2.0: 0.98},
    range_gain_neg={0.5: 0.955, 2.0: 0.975},
    neg_nonlinearity={0.5: [(4.0, 0.0), (6.0, 0.2)], 2.0: [(4.0, 0.0), (6.0, 0.1)]},
)


def test_policy_keep() -> None:
    res = fit_calibration(BASE, [_pt(1.0, 4.0)], previous=PREV, uncalibrated="keep")
    cal = res.calibration
    assert res.ranges[1.0].status == "fitted"
    assert cal.range_gain[0.5] == 0.96 and cal.range_gain_neg[0.5] == 0.955
    assert cal.nonlinearity_for(0.5) == ((4.0, 0.0), (6.0, 0.2))
    assert res.ranges[0.5].status == "kept" and res.ranges[0.5].gain_neg == 0.955
    assert res.ranges[0.1].status == "kept" and 0.1 not in cal.range_gain
    assert res.ranges[0.5].before_err_pct_max is None


def test_policy_typical_and_unity() -> None:
    typ = fit_calibration(BASE, [_pt(1.0, 4.0)], previous=PREV, uncalibrated="typical")
    assert typ.calibration.range_gain[0.5] == tables.TYPICAL_RANGE_GAIN[0.5]
    assert 0.5 not in typ.calibration.range_gain_neg
    assert typ.calibration.nonlinearity_for(0.5) == ()
    assert typ.ranges[0.2].status == "typical"
    uni = fit_calibration(BASE, [_pt(1.0, 4.0)], previous=PREV, uncalibrated="unity")
    assert set(uni.calibration.range_gain) == {1.0}
    assert uni.calibration.range_gain_neg == {} and uni.calibration.neg_nonlinearity == {}
    assert uni.ranges[0.5].status == "unity" and uni.ranges[0.5].gain_pos is None


def test_positive_fit_drops_previous_negative() -> None:
    res = fit_calibration(BASE, [_pt(0.5, 4.0)], previous=PREV, uncalibrated="keep")
    cal = res.calibration
    assert cal.range_gain[0.5] == pytest.approx(0.9741, abs=1e-4)
    assert 0.5 not in cal.range_gain_neg and cal.nonlinearity_for(0.5) == ()
    assert cal.range_gain_neg_for(0.5) == cal.range_gain_for(0.5)


def test_negative_refit_without_nl_points_keeps_previous_knots_only_with_keep() -> None:
    pts = [_pt(2.0, 4.0), _pt(2.0, -3.0)]
    keep = fit_calibration(BASE, pts, previous=PREV, uncalibrated="keep").calibration
    assert keep.nonlinearity_for(2.0) == ((4.0, 0.0), (6.0, 0.1))
    uni = fit_calibration(BASE, pts, previous=PREV, uncalibrated="unity").calibration
    assert uni.nonlinearity_for(2.0) == ()


def test_negative_only_range_uses_policy_for_positive() -> None:
    res = fit_calibration(BASE, [_pt(2.0, -3.0)], previous=PREV, uncalibrated="keep")
    assert res.calibration.range_gain[2.0] == 0.98
    assert res.calibration.range_gain_neg[2.0] == pytest.approx(0.9786, abs=1e-4)


# -- warnings ---------------------------------------------------------------------------------


def _warns(res: FitResult, vdiv: float) -> str:
    return " | ".join(res.ranges[vdiv].warnings)


def test_warning_thresholds() -> None:
    # |g - 1| > 5 %
    big = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.94)])
    assert "differs from 1 by more than 5%" in _warns(big, 1.0)
    ok = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.96)])
    assert "more than 5%" not in _warns(ok, 1.0)
    # g-/g+ ratio > 3 %
    ratio = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.98), GainPoint(1.0, 0, -3.0, -3.0 / 0.945)])
    assert "ratio" in _warns(ratio, 1.0)
    fine = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.98), GainPoint(1.0, 0, -3.0, -3.0 / 0.96)])
    assert "ratio" not in _warns(fine, 1.0)
    # residual > 0.3 % after the fit (two inconsistent positive points)
    res = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.98), GainPoint(1.0, 0, 2.0, 2.0 / 0.99)])
    assert "% off after the fit" in _warns(res, 1.0)
    # model ground
    model = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 0.98, ground_source="model")])
    assert "ground model" in _warns(model, 1.0)
    assert "ground model" not in _warns(ok, 1.0)


def test_warning_large_expansion() -> None:
    bent = UNIT1.with_gains(nonlinearity={0.5: [(4.0, 0.0), (6.0, 0.7)]})
    res = _fit([_pt(0.5, d, phys=bent) for d in (5.0, -3.0, -5.5)])
    assert "large negative expansion" in _warns(res, 0.5)
    small = _fit([_pt(0.5, d) for d in (5.0, -3.0, -6.0)])
    assert "large negative expansion" not in _warns(small, 0.5)


def test_gain_outside_limits_not_used() -> None:
    res = _fit([GainPoint(1.0, 0, 4.0, 4.0 / 1.3)], uncalibrated="unity")
    assert res.ranges[1.0].gain_pos is None and res.ranges[1.0].status == "unity"
    assert "outside" in _warns(res, 1.0)


# -- before / after, meta, dict ---------------------------------------------------------------


def test_before_after_numbers() -> None:
    pts = [_pt(1.0, 4.0), _pt(1.0, -3.0)]
    res = fit_calibration(BASE, pts, previous=Calibration(), uncalibrated="unity")
    rf = res.ranges[1.0]
    expect_before = max(abs(100 * (1 / 0.996 - 1)), abs(100 * (1 / 0.9892 - 1)))
    assert rf.before_err_pct_max == pytest.approx(expect_before, abs=1e-6)
    assert rf.after_err_pct_max == pytest.approx(0.0, abs=1e-3)
    same = fit_calibration(BASE, pts, previous=UNIT1).ranges[1.0]
    assert same.before_err_pct_max == pytest.approx(0.0, abs=1e-6)


def test_meta_points_ranges_and_id() -> None:
    pts = _standard_points()
    meta = {"tool": "edu09-calibrate", "created": "2026-10-25T12:00:00", "mode": "standard"}
    res = _fit(pts, meta=meta)
    m = res.calibration.meta
    assert m is not None and m["tool"] == "edu09-calibrate" and m["points"] == len(pts)
    assert m["ranges"]["0.5"]["nl"] == 3 and m["ranges"]["0.5"]["status"] == "fitted"
    no_meta = _fit(pts)
    assert no_meta.calibration.meta is None
    assert res.calibration.calibration_id == no_meta.calibration.calibration_id
    assert len(json.dumps(m)) <= tables.CAL_META_MAX_BYTES


def test_to_dict_rounded_and_json() -> None:
    res = _fit(_standard_points(np.random.default_rng(3)))
    d = res.to_dict()
    text = json.dumps(d)
    assert len(text) < 8000
    assert d["calibration_id"] == res.calibration.calibration_id
    r = d["ranges"]["0.5"]
    assert r["gain_pos"] == float(f"{res.ranges[0.5].gain_pos:.4g}")
    assert len(str(r["before_err_pct_max"]).replace("-", "").replace(".", "").lstrip("0")) <= 4
    assert d["offsets"]["base_ofs"] == 135
    assert d["previous"]["calibration_id"] == PREVIOUS.calibration_id


def test_argument_validation() -> None:
    with pytest.raises(ValueError):
        _fit([], uncalibrated="maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _fit([], knee_div=0.5)
    with pytest.raises(ValueError):
        GainPoint(0.3, 0, 1.0, 1.0)
    with pytest.raises(ValueError):
        GainPoint(1.0, 0, 0.0, 1.0)
    with pytest.raises(ValueError):
        GainPoint(1.0, 0, float("nan"), 1.0)
    with pytest.raises(ValueError):
        GainPoint(1.0, 0, 1.0, 1.0, ground_source="guess")  # type: ignore[arg-type]
    empty = _fit([], uncalibrated="unity")
    assert empty.calibration.range_gain == {} and empty.warnings == ()
    assert all(rf.status == "unity" for rf in empty.ranges.values())
