"""Gain / negative-nonlinearity fit of the calibration wizard (pure; spec 3.5).

Input: :class:`GainPoint` measurements taken with ``Calibration.without_gain_correction()`` (gain 1,
no nonlinearity): ``v_read = (mean - ground) * s * vdiv / counts_per_div`` and the reference
``v_ref`` (the multimeter reading). Points are classified by their *true* deflection
``d = v_ref / vdiv``, not by the planned purpose:

* ``g+``: least squares through the origin over ``d > 0``: ``g = sum(vt*vr) / sum(vr^2)``.
* ``g-``: the same over ``-knee_div <= d < 0``.
* nonlinearity (``d < -knee_div``, only with ``g-``): ``r = |v_read| / vdiv * g-``,
  ``e = r - |d|``; points within :data:`NL_MERGE_DIV` are averaged; knots
  ``[(min(knee_div, r_1 - 0.5), 0)] + [(r_i, e_i)]`` unless ``max e < NL_MIN_EXCESS_DIV``.
  Invalid knots (``units.validate_nonlinearity_knots``) are dropped with a warning.

Ranges without fitted values follow ``uncalibrated`` (``keep`` / ``typical`` / ``unity``); if ``g+``
is fitted but ``g-`` isn't, the previous ``g-`` and knots are dropped (``g-`` relative to an old
``g+`` is not trustworthy).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from edu09.errors import CalibrationError
from edu09.model import VoltsPerDiv
from edu09.protocol import tables
from edu09.units import Calibration, Knot, deflection_to_volts, validate_nonlinearity_knots

__all__ = [
    "NL_KNEE_DIV",
    "NL_MERGE_DIV",
    "NL_MIN_EXCESS_DIV",
    "FitResult",
    "GainPoint",
    "RangeFit",
    "UncalibratedPolicy",
    "fit_calibration",
]

NL_KNEE_DIV = 4.0
"""Negative deflections beyond this (true divisions) are nonlinearity points, not ``g-`` points."""
NL_MIN_EXCESS_DIV = 0.03
"""Below this largest excess (divisions) no knots are stored."""
NL_MERGE_DIV = 0.2
"""Nonlinearity points whose true deflections are within this of each other are averaged."""

WARN_GAIN_DEV = 0.05
"""Warn when ``|g - 1|`` exceeds this."""
WARN_NEG_POS_RATIO = 0.03
"""Warn when ``|g-/g+ - 1|`` exceeds this."""
WARN_RESIDUAL_PCT = 0.3
"""Warn when a point is off by more than this (percent) after the fit."""
WARN_NL_LAST_EXCESS_DIV = 0.6
"""Warn when the excess at the last knot exceeds this (divisions)."""

UncalibratedPolicy = Literal["keep", "typical", "unity"]
_POLICIES: tuple[str, ...] = get_args(UncalibratedPolicy)
_POLICY_STATUS = {"keep": "kept", "typical": "typical", "unity": "unity"}
_RANGES: tuple[float, ...] = tuple(sorted(float(v) for v in VoltsPerDiv))


def _sig(x: float | None, digits: int = 4) -> float | None:
    """Round to ``digits`` significant digits (JSON payloads)."""
    if x is None:
        return None
    return float(f"{x:.{digits}g}")


@dataclass(frozen=True)
class GainPoint:
    """One measured level: reference ``v_ref`` (multimeter) vs uncorrected scope ``v_read``.

    ``ground_source`` says where the ground level came from (``"map"``: measured in the zero step
    at the same range and DAC byte; ``"model"``: ``units.gnd_counts``). Raises ``ValueError``.
    """

    vdiv: float
    position_div: float
    v_ref: float
    v_read: float
    ground_source: Literal["map", "model"] = "map"
    noise_counts: float = 0.0
    setup_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "vdiv", float(VoltsPerDiv.parse(self.vdiv)))
        for name in ("position_div", "v_ref", "v_read", "noise_counts"):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
                raise ValueError(f"GainPoint.{name} must be a finite number, got {val!r}")
            object.__setattr__(self, name, float(val))
        if self.v_ref == 0.0:
            raise ValueError("GainPoint.v_ref must be non-zero")
        if self.ground_source not in ("map", "model"):
            raise ValueError(f"ground_source must be 'map' or 'model', got {self.ground_source!r}")

    @property
    def deflection_div(self) -> float:
        """True (reference) deflection in divisions, signed."""
        return self.v_ref / self.vdiv


@dataclass(frozen=True)
class RangeFit:
    """Fit result of one range. ``gain_pos`` / ``gain_neg`` / ``neg_knots`` are the values stored
    in the new calibration (``None`` / ``()`` = not stored: 1.0 resp. falls back to ``g+``).
    ``status``: ``fitted`` (at least one gain fitted from this session's points) or, for ranges
    without fitted values, the ``uncalibrated`` policy's outcome ``kept`` / ``typical`` /
    ``unity``. Errors are the largest ``|100*(v_cal/v_ref - 1)|`` over this range's points with the
    previous gains (before) and the new calibration (after); ``None`` without points."""

    vdiv: float
    gain_pos: float | None
    gain_neg: float | None
    neg_knots: tuple[Knot, ...]
    n_pos: int
    n_neg: int
    n_nl: int
    before_err_pct_max: float | None
    after_err_pct_max: float | None
    status: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vdiv": self.vdiv,
            "gain_pos": _sig(self.gain_pos),
            "gain_neg": _sig(self.gain_neg),
            "neg_knots": [[_sig(r), _sig(e)] for r, e in self.neg_knots],
            "n_pos": self.n_pos,
            "n_neg": self.n_neg,
            "n_nl": self.n_nl,
            "before_err_pct_max": _sig(self.before_err_pct_max),
            "after_err_pct_max": _sig(self.after_err_pct_max),
            "status": self.status,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class FitResult:
    """New calibration (``base`` offsets + fitted gains, ``source="measured"``) and its report."""

    calibration: Calibration
    ranges: dict[float, RangeFit] = field(hash=False)
    warnings: tuple[str, ...] = ()
    previous: Calibration | None = field(default=None, compare=False, hash=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Small JSON summary (floats with 4 significant digits; no raw samples)."""
        cal = self.calibration
        d: dict[str, Any] = {
            "calibration_id": cal.calibration_id,
            "ranges": {f"{v:g}": rf.to_dict() for v, rf in sorted(self.ranges.items())},
            "warnings": list(self.warnings),
            "offsets": {
                "base_ofs": cal.base_ofs,
                "gnd_k": _sig(cal.gnd_fit.k),
                "gnd_b": _sig(cal.gnd_fit.b),
                "range_corr": {f"{k:g}": v for k, v in sorted(cal.range_corr.items())},
            },
        }
        prev = self.previous
        if prev is not None:
            d["previous"] = {
                "calibration_id": prev.calibration_id,
                "base_ofs": prev.base_ofs,
                "gnd_k": _sig(prev.gnd_fit.k),
                "gnd_b": _sig(prev.gnd_fit.b),
            }
        return d


def _ls_gain(points: Sequence[GainPoint]) -> float | None:
    """Least squares through the origin ``V_true = g * V_read``."""
    if not points:
        return None
    num = sum(p.v_ref * p.v_read for p in points)
    den = sum(p.v_read * p.v_read for p in points)
    if den <= 0.0:
        return None
    g = num / den
    return g if math.isfinite(g) else None


def _gain_ok(g: float | None, what: str, warns: list[str]) -> float | None:
    if g is None:
        return None
    if not tables.RANGE_GAIN_MIN < g < tables.RANGE_GAIN_MAX:
        warns.append(
            f"{what} {g:.4f} outside ({tables.RANGE_GAIN_MIN:g}, {tables.RANGE_GAIN_MAX:g}); "
            "not used (check the typed readings and that the probe is on x1)"
        )
        return None
    return round(g, 5)


def _fit_nl(
    points: Sequence[GainPoint], g_neg: float, knee_div: float, warns: list[str]
) -> tuple[Knot, ...]:
    """Nonlinearity knots from points beyond the knee (empty: none needed or invalid)."""
    rows = sorted(
        (abs(p.v_ref) / p.vdiv, abs(p.v_read) / p.vdiv * g_neg) for p in points
    )  # (d, r), by true deflection
    groups: list[list[tuple[float, float]]] = []
    for d, r in rows:
        if groups and d - groups[-1][0][0] <= NL_MERGE_DIV:
            groups[-1].append((d, r))
        else:
            groups.append([(d, r)])
    merged: list[tuple[float, float]] = []  # (r, e)
    for grp in groups:
        d = sum(x[0] for x in grp) / len(grp)
        r = sum(x[1] for x in grp) / len(grp)
        merged.append((r, r - d))
    merged.sort()
    if max(e for _r, e in merged) < NL_MIN_EXCESS_DIV:
        return ()
    r1 = merged[0][0]
    knots = [(round(min(knee_div, r1 - 0.5), 4), 0.0)]
    knots += [(round(r, 4), round(e, 4)) for r, e in merged]
    try:
        valid = validate_nonlinearity_knots(knots, "nonlinearity")
    except CalibrationError as exc:
        warns.append(f"negative nonlinearity not stored ({exc}); the gains are kept")
        return ()
    last_r, last_e = valid[-1]
    if last_e > WARN_NL_LAST_EXCESS_DIV:
        warns.append(f"large negative expansion: {last_e:.2f} div at {last_r:.2f} div below ground")
    return valid


def _err_pct(p: GainPoint, cal: Calibration) -> float:
    dev = p.v_read / p.vdiv * cal.counts_per_div
    v_cal = float(deflection_to_volts(dev, p.vdiv, cal))
    return 100.0 * (v_cal / p.v_ref - 1.0)


def _with_gains_of(base: Calibration, src: Calibration) -> Calibration:
    return base.without_gain_correction().with_gains(
        pos=dict(src.range_gain),
        neg=dict(src.range_gain_neg),
        nonlinearity=dict(src.neg_nonlinearity),
    )


def fit_calibration(
    base: Calibration,
    points: Sequence[GainPoint],
    *,
    previous: Calibration,
    uncalibrated: UncalibratedPolicy = "keep",
    knee_div: float = NL_KNEE_DIV,
    meta: Mapping[str, Any] | None = None,
) -> FitResult:
    """Fit per-range ``g+``, ``g-`` and negative nonlinearity knots (module docstring).

    ``base`` provides the offsets (zero-step result or the scope's calibration), ``previous`` the
    calibration in use before the session (``keep`` policy and the "before" errors). The result's
    calibration is ``base`` with the new gain maps, ``meta`` (``points`` / ``ranges`` added when
    absent) and ``source="measured"``. Raises ``ValueError`` for bad arguments.
    """
    if uncalibrated not in _POLICIES:
        raise ValueError(f"uncalibrated must be one of {_POLICIES}, got {uncalibrated!r}")
    if not tables.NL_MIN_READ_DIV <= knee_div <= tables.NL_MAX_READ_DIV:
        raise ValueError(
            f"knee_div must be in [{tables.NL_MIN_READ_DIV}, {tables.NL_MAX_READ_DIV}]"
        )
    by_range: dict[float, list[GainPoint]] = {v: [] for v in _RANGES}
    for p in points:
        by_range[p.vdiv].append(p)

    pos_map: dict[float, float | None] = {}
    neg_map: dict[float, float | None] = {}
    nl_map: dict[float, tuple[Knot, ...] | None] = {}
    plan: dict[float, tuple[str, int, int, int, list[str]]] = {}
    for vdiv in _RANGES:
        pts = by_range[vdiv]
        warns: list[str] = []
        pos_pts = [p for p in pts if p.deflection_div > 0]
        neg_pts = [p for p in pts if -knee_div <= p.deflection_div < 0]
        nl_pts = [p for p in pts if p.deflection_div < -knee_div]
        g_pos = _gain_ok(_ls_gain(pos_pts), "positive gain", warns)
        g_neg = _gain_ok(_ls_gain(neg_pts), "negative gain", warns)
        knots: tuple[Knot, ...] = ()
        if nl_pts and g_neg is not None:
            knots = _fit_nl(nl_pts, g_neg, knee_div, warns)
        elif nl_pts:
            warns.append(
                f"points beyond {knee_div:g} div below ground need a negative gain point "
                f"(within {knee_div:g} div): nonlinearity not fitted"
            )
        prev_pos = previous.range_gain.get(vdiv)
        policy_pos = {
            "keep": prev_pos,
            "typical": tables.TYPICAL_RANGE_GAIN.get(vdiv),
            "unity": None,
        }[uncalibrated]
        if g_pos is None and g_neg is None:
            status = _POLICY_STATUS[uncalibrated]
            pos_map[vdiv] = policy_pos
            if uncalibrated == "keep":
                neg_map[vdiv] = previous.range_gain_neg.get(vdiv)
                nl_map[vdiv] = previous.nonlinearity_for(vdiv) or None
            else:
                neg_map[vdiv] = None
                nl_map[vdiv] = None
        else:
            status = "fitted"
            pos_map[vdiv] = g_pos if g_pos is not None else policy_pos
            neg_map[vdiv] = g_neg  # g+ fitted without g-: drop the previous g- and knots
            if g_neg is None:
                nl_map[vdiv] = None
            elif nl_pts:
                nl_map[vdiv] = knots or None
            elif uncalibrated == "keep":  # g- re-fitted, no points beyond the knee this time
                nl_map[vdiv] = previous.nonlinearity_for(vdiv) or None
            else:
                nl_map[vdiv] = None
        gp, gn = pos_map[vdiv], neg_map[vdiv]
        for g, what in ((g_pos, "positive"), (g_neg, "negative")):
            if g is not None and abs(g - 1.0) > WARN_GAIN_DEV:
                warns.append(f"{what} gain {g:.4f} differs from 1 by more than {WARN_GAIN_DEV:.0%}")
        if gn is not None and abs(gn / (gp if gp is not None else 1.0) - 1.0) > WARN_NEG_POS_RATIO:
            warns.append(
                f"negative/positive gain ratio {gn / (gp or 1.0):.4f} differs from 1 by more "
                f"than {WARN_NEG_POS_RATIO:.0%}"
            )
        n_model = sum(1 for p in pts if p.ground_source == "model")
        if n_model:
            warns.append(
                f"{n_model} point(s) used the ground model instead of a measured ground "
                "(no zero step): offsets may bias the gain"
            )
        plan[vdiv] = (status, len(pos_pts), len(neg_pts), len(nl_pts), warns)

    new_meta: dict[str, Any] | None = None
    if meta is not None:
        new_meta = dict(meta)
        new_meta.setdefault("points", len(points))
        new_meta.setdefault(
            "ranges",
            {
                f"{v:g}": {
                    "pos": pos_map[v],
                    "neg": neg_map[v],
                    "nl": len(nl_map[v] or ()),
                    "status": plan[v][0],
                }
                for v in _RANGES
            },
        )
    cal = base.without_gain_correction().with_gains(
        pos=pos_map, neg=neg_map, nonlinearity=nl_map, meta=new_meta
    )
    cal = dataclasses.replace(cal, source="measured")
    before_cal = _with_gains_of(base, previous)

    ranges: dict[float, RangeFit] = {}
    all_warns: list[str] = []
    for vdiv in _RANGES:
        status, n_pos, n_neg, n_nl, warns = plan[vdiv]
        pts = by_range[vdiv]
        before = [abs(_err_pct(p, before_cal)) for p in pts]
        after: list[float] = []
        for p in pts:
            err = _err_pct(p, cal)
            after.append(abs(err))
            if abs(err) > WARN_RESIDUAL_PCT:
                where = f" ({p.setup_id})" if p.setup_id else ""
                warns.append(
                    f"point {p.v_ref:+.3f} V at position {p.position_div:g}{where} is "
                    f"{err:+.2f} % off after the fit"
                )
        ranges[vdiv] = RangeFit(
            vdiv=vdiv,
            gain_pos=cal.range_gain.get(vdiv),
            gain_neg=cal.range_gain_neg.get(vdiv),
            neg_knots=cal.nonlinearity_for(vdiv),
            n_pos=n_pos,
            n_neg=n_neg,
            n_nl=n_nl,
            before_err_pct_max=max(before) if before else None,
            after_err_pct_max=max(after) if after else None,
            status=status,
            warnings=tuple(warns),
        )
        all_warns.extend(f"{vdiv:g} V/div: {w}" for w in warns)
    return FitResult(calibration=cal, ranges=ranges, warnings=tuple(all_warns), previous=previous)
