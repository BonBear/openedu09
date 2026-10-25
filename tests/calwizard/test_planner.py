"""Calibration wizard planner (spec 2): profiles, invariants, grammar, serialisation."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from edu09.calwizard import model as m
from edu09.calwizard.model import (
    FLOATING_WARNING,
    AdjustableSource,
    FixedSource,
    Plan,
    PlanOptions,
    TrimmerConfig,
    load_sources,
    save_sources,
)
from edu09.calwizard.planner import parse_source, plan_calibration, position_for

USB = FixedSource("USB", 5.0)
AA = FixedSource("AA", 1.5, tolerance_pct=10.0, reversible=True)
BENCH15 = AdjustableSource("bench", 0.0, 15.0, reversible=True)
BENCH30 = AdjustableSource("big", 0.0, 30.0)

PROFILES = {
    "usb_aa": [USB, AA],
    "bench15": [BENCH15],
    "bench30_norev": [BENCH30],
    "none": [],
}


def _compact(plan: Plan) -> list[tuple[object, ...]]:
    return [
        (
            s.id,
            s.kind,
            s.source_label,
            s.volts,
            tuple((p.vdiv, p.position_div, round(p.expected_div, 3), p.purpose) for p in s.points),
        )
        for s in plan.setups
        if s.kind != "zero"
    ]


# --------------------------------------------------------------------------------------------
# Profiles (deterministic snapshots)
# --------------------------------------------------------------------------------------------


def test_profile_usb_aa_snapshot() -> None:
    plan = plan_calibration(PROFILES["usb_aa"])
    assert _compact(plan) == [
        (
            "S02",
            "reference",
            "USB",
            5.0,
            ((2.0, -1.25, 2.5, "gain_pos"), (1.0, -2.5, 5.0, "gain_pos")),
        ),
        ("S03", "reference", "AA", 1.5, ((0.5, -1.5, 3.0, "gain_pos"),)),
        ("S04", "reference", "AA", -1.5, ((0.5, 1.5, -3.0, "gain_neg"),)),
        ("S05", "verify", "AA", -1.5, ((0.5, 2.5, -3.0, "verify"),)),
        ("S06", "verify", "AA", 1.5, ((0.5, -0.5, 3.0, "verify"),)),
    ]
    assert plan.setups[0].kind == "zero"
    cov = {c.vdiv: (c.pos, c.neg, c.nonlin) for c in plan.coverage}
    assert cov[1.0][0] == "good"
    assert cov[2.0][0] == "fallback"
    assert cov[0.5][:2] == ("fallback", "good")
    for v in (0.1, 0.2, 5.0):
        assert cov[v][:2] == ("missing", "missing")
    needs01 = " ".join(next(c for c in plan.coverage if c.vdiv == 0.1).needs)
    assert "+0.40..+0.60 V" in needs01
    assert "divide a larger source" in needs01
    needs5 = " ".join(next(c for c in plan.coverage if c.vdiv == 5.0).needs)
    assert "above the 15 V limit" in needs5
    assert FLOATING_WARNING in plan.warnings
    assert plan.reconnections == 3  # USB -> AA, AA lead swap, verify swap
    assert plan.user_actions == 5
    assert plan.estimated_s > 0


def test_profile_bench15_snapshot() -> None:
    plan = plan_calibration(PROFILES["bench15"])
    refs = [s for s in plan.setups if s.kind == "reference"]
    pos = [s.volts for s in refs if s.volts is not None and s.volts > 0]
    neg = [s.volts for s in refs if s.volts is not None and s.volts < 0]
    assert pos == [0.5, 1.0, 2.5, 5.0, 12.0]
    assert [-v for v in neg] == [12.0, 10.0, 6.0, 5.0, 3.0, 2.5, 1.2, 1.0, 0.6, 0.5, 0.3]
    cov = {c.vdiv: c for c in plan.coverage}
    for v in (0.1, 0.2, 0.5, 1.0, 2.0):
        assert (cov[v].pos, cov[v].neg, cov[v].nonlin) == ("good", "good", "good")
    assert cov[5.0].nonlin == "missing"
    assert any("nonlinearity" in n and "above the 15 V limit" in n for n in cov[5.0].needs)
    assert plan.reconnections == 2  # lead swap + the positive verify swap
    assert plan.setups[-1].kind == "verify"


def test_profile_bench30_not_reversible_capped() -> None:
    plan = plan_calibration(PROFILES["bench30_norev"], PlanOptions(max_abs_v=15))
    assert any("limited to 15 V" in w for w in plan.warnings)
    assert FLOATING_WARNING not in plan.warnings
    for c in plan.coverage:
        assert c.neg == "not_reversible"
        assert "no reversible source: negative side uses the positive gain" in c.needs
    for s in plan.setups:
        if s.volts is not None:
            assert 0 < s.volts <= 15


def test_profile_no_sources() -> None:
    plan = plan_calibration([])
    assert [s.kind for s in plan.setups] == ["zero"]
    assert plan.setups[0].points == ()
    assert all(c.pos == "missing" for c in plan.coverage)
    assert plan.user_actions == 0
    assert any("only the zero" in w for w in plan.warnings)


def test_plan_is_deterministic() -> None:
    for srcs in PROFILES.values():
        assert plan_calibration(srcs) == plan_calibration(srcs)


# --------------------------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------------------------


def _all_plans() -> list[Plan]:
    out = []
    for srcs in PROFILES.values():
        for mode in ("quick", "standard", "full"):
            for cap in (5.0, 15.0, 30.0):
                out.append(plan_calibration(srcs, PlanOptions(mode=mode, max_abs_v=cap)))
    return out


@pytest.mark.parametrize(
    "plan", _all_plans(), ids=lambda p: f"{p.options.mode}-{p.options.max_abs_v}"
)
def test_points_within_limits(plan: Plan) -> None:
    for s in plan.setups:
        if s.volts is not None:
            assert abs(s.volts) <= plan.options.max_abs_v + 1e-9
        for p in s.points:
            assert p.vdiv in plan.options.ranges
            assert abs(p.position_div / 0.25 - round(p.position_div / 0.25)) < 1e-9
            d = abs(p.expected_div)
            if p.expected_div > 0:
                assert p.position_div >= -3.5 - 1e-9
                assert p.position_div + d <= 3.5 + 1e-9
            elif p.expected_div < 0:
                assert p.position_div <= 3.5 + 1e-9
                assert p.position_div - 1.06 * d >= -3.75 - 1e-9
        if s.kind in ("reference", "verify"):
            assert s.accept_v is not None
            assert s.volts is not None
            lo, hi = s.accept_v
            assert lo - 1e-6 <= s.volts <= hi + 1e-6


@pytest.mark.parametrize(
    "plan", _all_plans(), ids=lambda p: f"{p.options.mode}-{p.options.max_abs_v}"
)
def test_grouping_and_order(plan: Plan) -> None:
    kinds = [s.kind for s in plan.setups]
    assert kinds.count("zero") == 1
    assert kinds.index("zero") == 0
    if "verify" in kinds:
        assert kinds[-1] == "verify"
    refs = [s for s in plan.setups if s.kind == "reference"]
    labels = [s.source_label for s in refs]
    order = [x.label for x in plan.sources]
    seen: list[str | None] = []
    for lab in labels:
        if not seen or seen[-1] != lab:
            assert lab not in seen, "a source's setups must be contiguous"
            seen.append(lab)
    assert seen == [x for x in order if x in seen]
    for lab in seen:
        vs = [s.volts for s in refs if s.source_label == lab]
        assert all(v is not None for v in vs)
        signs = [1 if v > 0 else -1 for v in vs]  # type: ignore[operator]
        assert signs == sorted(signs, reverse=True), "positive first, one lead swap"
        posv = [v for v in vs if v > 0]  # type: ignore[operator]
        negv = [-v for v in vs if v < 0]  # type: ignore[operator]
        assert posv == sorted(posv)
        assert negv == sorted(negv, reverse=True)
    # ground map = every (range, position) of reference and verify setups
    zero = plan.setups[0]
    used = {
        (p.vdiv, p.position_div)
        for s in plan.setups
        if s.kind in ("reference", "verify")
        for p in s.points
    }
    assert {(p.vdiv, p.position_div) for p in zero.points} == used


def test_adjustable_setups_accept_8_percent() -> None:
    plan = plan_calibration([BENCH15])
    for s in plan.setups:
        if s.kind != "reference" or any(p.purpose in ("nonlin6", "nonlin65") for p in s.points):
            continue
        assert s.accept_v is not None and s.volts is not None
        lo, hi = sorted(abs(x) for x in s.accept_v)
        assert lo <= abs(s.volts) * 0.92 + 1e-6
        assert hi >= abs(s.volts) * 1.08 - 1e-6


def test_verify_uses_last_reference_and_shifted_positions() -> None:
    plan = plan_calibration([USB, AA])
    last_ref = [s for s in plan.setups if s.kind == "reference"][-1]
    ver = [s for s in plan.setups if s.kind == "verify"][0]
    assert (ver.source_label, ver.volts) == (last_ref.source_label, last_ref.volts)
    assert 1 <= len(ver.points) <= 3
    for p in ver.points:
        fit_pos = position_for(abs(p.expected_div), p.expected_div < 0)
        assert abs(abs(p.position_div - fit_pos) - 1.0) < 1e-9
    assert plan_calibration([USB, AA], PlanOptions(verify=False)).setups[-1].kind != "verify"


def test_verify_points_keep_a_margin_from_the_window_edge() -> None:
    from edu09.calwizard.model import VERIFY_WINDOW

    w0, w1 = VERIFY_WINDOW
    m = 0.08 * (w1 - w0)
    for prof in ("usb_aa", "bench15", "bench30_norev"):
        plan = plan_calibration(PROFILES[prof])
        for s in plan.setups:
            if s.kind == "verify":
                for p in s.points:
                    assert w0 + m - 1e-9 <= abs(p.expected_div) <= w1 - m + 1e-9


def test_positive_verify_follows_negative_one() -> None:
    plan = plan_calibration(PROFILES["bench15"])
    vs = [s for s in plan.setups if s.kind == "verify"]
    assert len(vs) == 2
    assert vs[0].volts is not None and vs[0].volts < 0
    assert vs[1].volts is not None and vs[1].volts > 0
    assert "swap the leads" in vs[1].instruction
    # a positive-only plan gets a single verify
    pos_only = plan_calibration([FixedSource("USB", 5.0)])
    assert [s.kind for s in pos_only.setups].count("verify") == 1


def test_no_zero_option() -> None:
    plan = plan_calibration([USB], PlanOptions(zero=False))
    assert all(s.kind != "zero" for s in plan.setups)


def test_ranges_option_limits_points() -> None:
    plan = plan_calibration([BENCH15], PlanOptions(ranges=(0.5, 1.0)))
    assert {p.vdiv for s in plan.setups for p in s.points} <= {0.5, 1.0}
    assert [c.vdiv for c in plan.coverage] == [0.5, 1.0]


# --------------------------------------------------------------------------------------------
# Trimmer
# --------------------------------------------------------------------------------------------


def test_trimmer_defaults_after_zero_half_volt_battery() -> None:
    plan = plan_calibration([USB, AA], PlanOptions(trimmer=TrimmerConfig(enabled=True)))
    kinds = [s.kind for s in plan.setups]
    assert kinds[:2] == ["zero", "trimmer"]
    tr = plan.setups[1]
    assert tr.source_label == "AA"  # 3 div at 0.5 V/div preferred over USB (10 div)
    assert tr.volts == 1.5
    assert tr.points[0].vdiv == 0.5
    assert "RV1" in tr.instruction
    assert "middle position" in tr.instruction
    assert "ALL ranges" in tr.instruction


def test_trimmer_first_and_open() -> None:
    opts = PlanOptions(trimmer=TrimmerConfig(enabled=True, placement="first", reference="open"))
    plan = plan_calibration([USB], opts)
    assert [s.kind for s in plan.setups][:2] == ["trimmer", "zero"]
    assert plan.setups[0].volts is None
    assert not plan.setups[0].needs_reading


def test_trimmer_adjustable_and_explicit_label() -> None:
    plan = plan_calibration([BENCH15], PlanOptions(trimmer=TrimmerConfig(enabled=True)))
    tr = next(s for s in plan.setups if s.kind == "trimmer")
    assert tr.volts == 1.5 and tr.adjustable
    with pytest.raises(ValueError, match="trimmer source"):
        plan_calibration(
            [BENCH15], PlanOptions(trimmer=TrimmerConfig(enabled=True, source_label="nope"))
        )


def test_trimmer_without_sources_falls_back_to_open() -> None:
    plan = plan_calibration([], PlanOptions(trimmer=TrimmerConfig(enabled=True)))
    tr = next(s for s in plan.setups if s.kind == "trimmer")
    assert tr.volts is None
    assert any("open input" in w for w in plan.warnings)


# --------------------------------------------------------------------------------------------
# Grammar and validation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5,label=USB", FixedSource("USB", 5.0)),
        ("1.5@10%,rev,label=AA", FixedSource("AA", 1.5, 10.0, True)),
        ("9V", FixedSource("9 V", 9.0)),
        ("1.5@10", FixedSource("1.5 V", 1.5, 10.0)),
        ("0..15,rev,label=bench", AdjustableSource("bench", 0.0, 15.0, True)),
        ("0.5..12V,reversible", AdjustableSource("0.5..12 V", 0.5, 12.0, True)),
        ("3.3,label=a, b, c", FixedSource("a, b, c", 3.3)),
    ],
)
def test_parse_source(text: str, expected: object) -> None:
    assert parse_source(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "abc", "-5", "5,foo", "5..2", "0", "1@30%", "5,label=", "1..2..3", "5label=x"],
)
def test_parse_source_errors(text: str) -> None:
    with pytest.raises(ValueError):
        parse_source(text)


def test_source_validation() -> None:
    with pytest.raises(ValueError, match="at most 8"):
        plan_calibration([FixedSource(f"s{i}", 1.0) for i in range(9)])
    with pytest.raises(ValueError, match="duplicate"):
        plan_calibration([FixedSource("x", 1.0), FixedSource("x", 2.0)])
    with pytest.raises(ValueError):
        FixedSource("x", math.nan)
    with pytest.raises(ValueError):
        FixedSource("x", 1.0, tolerance_pct=26)
    with pytest.raises(ValueError):
        AdjustableSource("x", 5.0, 5.0)
    with pytest.raises(ValueError):
        FixedSource("x" * 41, 1.0)
    with pytest.raises(ValueError):
        FixedSource("bad\nlabel", 1.0)
    with pytest.raises(ValueError):
        PlanOptions(max_abs_v=31)
    with pytest.raises(ValueError):
        PlanOptions(ranges=(0.3,))
    with pytest.raises(ValueError):
        PlanOptions(mode="fast")  # type: ignore[arg-type]


def test_fixed_source_above_cap_is_unused() -> None:
    plan = plan_calibration([FixedSource("24V", 24.0)], PlanOptions(max_abs_v=15))
    assert any("not used" in w for w in plan.warnings)
    assert all(s.kind == "zero" for s in plan.setups)


# --------------------------------------------------------------------------------------------
# Serialisation and profiles
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(PROFILES))
def test_plan_dict_round_trip(name: str) -> None:
    opts = PlanOptions(mode="full", trimmer=TrimmerConfig(enabled=True), uncalibrated="typical")
    plan = plan_calibration(PROFILES[name], opts)
    d = json.loads(json.dumps(plan.to_dict()))
    assert Plan.from_dict(d) == plan


def test_plan_from_dict_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        Plan.from_dict({"setups": []})
    with pytest.raises(ValueError):
        Plan.from_dict([])  # type: ignore[arg-type]


def test_sources_profile_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "src.json"
    opts = PlanOptions(mode="quick", max_abs_v=12)
    save_sources([USB, AA, BENCH15], opts, path=p)
    assert load_sources(p) == ((USB, AA, BENCH15), opts)
    assert "secret" not in p.read_text()


def test_sources_profile_default_path_and_bad_file() -> None:
    assert load_sources() == ((), PlanOptions())
    path = save_sources([USB])
    assert path == m.default_sources_path()
    assert path.name == "calibration_sources.json"
    assert load_sources()[0] == (USB,)
    path.write_text("{not json")
    assert load_sources() == ((), PlanOptions())
    path.write_text(json.dumps({"version": 1, "sources": [{"type": "nope"}]}))
    assert load_sources() == ((), PlanOptions())
