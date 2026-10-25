"""Calibration wizard planner (pure; docs/specs/calibration_wizard.md 2).

:func:`plan_calibration` turns the user's reference sources into an ordered list of setups
(trimmer, zero, reference setups grouped by source, verify) with measurement points, a per-range
coverage report and time/effort estimates. Deterministic: the same input gives the same plan.

Deviation notes (documented choices where the spec leaves room):

* The +-8 % rule for adjustable setups is applied in the coverage test: an adjustable value
  covers a (range, purpose) only if a +-8 % setting error (less for targets whose accepted
  window is narrower: nonlin6 6.7 %, nonlin65 1.5 %) keeps the point inside its accepted
  window and position limits. So every setup accepts its value +-8 % (or the narrowest
  target's own margin, further limited by the level limit for nonlin6) and no setup ever
  needs splitting.
* ``reconnections`` counts disconnects, source changes and lead swaps; the very first
  connection is not counted.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from edu09.calwizard.model import (
    FLOATING_WARNING,
    INPUT_IMPEDANCE_OHM,
    NEG_GROUND_MAX,
    NEG_HEADROOM,
    NEG_LEVEL_MIN,
    NONLIN_PURPOSES,
    POS_GROUND_MIN,
    POS_LEVEL_MAX,
    POSITION_STEP,
    TARGETS,
    TRIMMER_TARGET_DIV,
    TRIMMER_WINDOW,
    VERIFY_WINDOW,
    AdjustableSource,
    FixedSource,
    MeasurePoint,
    Plan,
    PlanOptions,
    RangeCoverage,
    Setup,
    Source,
    Target,
    validate_sources,
)
from edu09.model import VoltsPerDiv

__all__ = [
    "ADJUSTABLE_MARGIN",
    "DIVIDER_HINT",
    "accept_interval",
    "level_ok",
    "parse_source",
    "plan_calibration",
    "position_for",
]

ADJUSTABLE_MARGIN: Final = 0.08
"""Adjustable setups must accept at least +-8 % around the value to set."""
VERIFY_MAX_RANGES: Final = 3
VERIFY_SHIFT_DIV: Final = 1.0
EST_S_PER_ACTION: Final = 30.0
EST_S_PER_POINT: Final = 0.8
EST_S_ZERO: Final = 15.0
EST_S_PER_GROUND_POINT: Final = 0.5
DIVIDER_HINT: Final = (
    "or divide a larger source with two resistors (well below the scope's "
    f"{INPUT_IMPEDANCE_OHM / 1e3:g} kOhm input impedance, e.g. 1-10 kOhm); only the multimeter "
    "reading at the probe tip matters"
)
_EPS: Final = 1e-9


# --------------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------------


def _q(x: float) -> float:
    return round(x / POSITION_STEP) * POSITION_STEP


def _q_up(x: float) -> float:
    return math.ceil(x / POSITION_STEP) * POSITION_STEP


def _q_down(x: float) -> float:
    return math.floor(x / POSITION_STEP) * POSITION_STEP


def position_for(d: float, negative: bool) -> float:
    """Ground position (div, quantised to 0.25) for a deflection magnitude ``d``.

    Positive: ``p = -d/2`` with ``p >= -3.5`` and ``p + d <= 3.5``. Negative: ``p = d/2`` with
    ``p <= 3.5`` and ``p - 1.06 d >= -3.75``. The level limit wins when they conflict.
    """
    if negative:
        p = min(_q(d / 2), NEG_GROUND_MAX)
        lo = _q_up(NEG_LEVEL_MIN + NEG_HEADROOM * d - _EPS)
        return float(max(p, lo) + 0.0)
    p = max(_q(-d / 2), POS_GROUND_MIN)
    hi = _q_down(POS_LEVEL_MAX - d + _EPS)
    return float(min(p, hi) + 0.0)


def level_ok(p: float, d: float, negative: bool) -> bool:
    """Ground and level of a ``d`` div deflection at position ``p`` within the limits."""
    if negative:
        return p <= NEG_GROUND_MAX + _EPS and p - NEG_HEADROOM * d >= NEG_LEVEL_MIN - _EPS
    return p >= POS_GROUND_MIN - _EPS and p + d <= POS_LEVEL_MAX + _EPS


def accept_interval(
    window: tuple[float, float], p: float, negative: bool
) -> tuple[float, float] | None:
    """Deflection interval (div) inside ``window`` whose level stays within the limits at ``p``."""
    lo, hi = window
    if negative:
        hi = min(hi, (p - NEG_LEVEL_MIN) / NEG_HEADROOM)
        if p > NEG_GROUND_MAX + _EPS:
            return None
    else:
        hi = min(hi, POS_LEVEL_MAX - p)
        if p < POS_GROUND_MIN - _EPS:
            return None
    return (lo, hi) if lo <= hi + _EPS else None


def _nice(v: float) -> float:
    """Round a voltage to a value that is easy to set: 0.01 V below 1 V, 0.05 V up to 10 V,
    0.1 V above."""
    step = 0.01 if v < 1.0 else 0.05 if v <= 10.0 else 0.1
    return round(round(v / step) * step, 2)


def _fmt_v(x: float) -> str:
    return f"{x:.2f}" if abs(x) < 10 else f"{x:.3g}"


def _vlabel(vdiv: float) -> str:
    return VoltsPerDiv.parse(vdiv).label


# --------------------------------------------------------------------------------------------
# Source grammar (CLI)
# --------------------------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d*)?|\.\d+)"
_FIXED_RE: Final = re.compile(rf"^{_NUM}\s*v?(?:\s*@\s*{_NUM}\s*%?)?$", re.IGNORECASE)
_ADJ_RE: Final = re.compile(rf"^{_NUM}\s*v?\s*\.\.\s*{_NUM}\s*v?$", re.IGNORECASE)


def parse_source(text: str) -> Source:
    """Parse a CLI source spec.

    Fixed: ``VOLTS[@TOL%][,rev][,label=TEXT]`` (``5,label=USB``, ``1.5@10%,rev,label=AA``).
    Adjustable: ``MIN..MAX[,rev][,label=TEXT]`` (``0..15,rev,label=bench``). ``label=`` must be
    last and may contain commas. Values are magnitudes; a ``V`` suffix is allowed.
    Raises ``ValueError``.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty source spec")
    s = text.strip()
    label: str | None = None
    idx = s.find("label=")
    if idx >= 0:
        if idx > 0 and s[idx - 1] != ",":
            raise ValueError(f"source spec {text!r}: 'label=' must follow a comma")
        label = s[idx + len("label=") :].strip()
        if not label:
            raise ValueError(f"source spec {text!r}: empty label")
        s = s[: max(idx - 1, 0)]
    parts = [p.strip() for p in s.split(",")]
    if not parts or not parts[0]:
        raise ValueError(f"source spec {text!r}: missing voltage")
    value, flags = parts[0], parts[1:]
    reversible = False
    for f in flags:
        if f.lower() in ("rev", "reversible"):
            reversible = True
        else:
            raise ValueError(f"source spec {text!r}: unknown option {f!r} (rev, label=TEXT)")
    if m := _ADJ_RE.match(value):
        lo, hi = float(m.group(1)), float(m.group(2))
        return AdjustableSource(label or f"{lo:g}..{hi:g} V", lo, hi, reversible)
    if m := _FIXED_RE.match(value):
        v = float(m.group(1))
        tol = float(m.group(2)) if m.group(2) is not None else 5.0
        return FixedSource(label or f"{v:g} V", v, tol, reversible)
    raise ValueError(
        f"source spec {text!r}: expected VOLTS[@TOL%] (fixed) or MIN..MAX (adjustable)"
    )


# --------------------------------------------------------------------------------------------
# Candidates and coverage
# --------------------------------------------------------------------------------------------

Item = tuple[float, str]  # (vdiv, purpose)


@dataclass
class _Cand:
    src_idx: int
    source: Source
    sign: int
    mag: float
    cover: dict[Item, int] = field(default_factory=dict)  # item -> 2 good / 1 fallback

    @property
    def fixed(self) -> bool:
        return isinstance(self.source, FixedSource)

    @property
    def volts(self) -> float:
        return self.sign * self.mag


def _within(lo: float, hi: float, window: tuple[float, float]) -> bool:
    return window[0] - _EPS <= lo and hi <= window[1] + _EPS


def _margin(t: Target) -> float:
    """Relative setting tolerance an adjustable source needs for target ``t``: +-8 %, or less
    when the target's accepted window is narrower (nonlin6: 6.7 %, nonlin65: 1.5 %)."""
    lo, hi = t.accepted
    return min(ADJUSTABLE_MARGIN, (t.target - lo) / t.target, (hi - t.target) / t.target)


def _quality(c: _Cand, vdiv: float, t: Target) -> int:
    """2 good, 1 fallback, 0 not covered. Fixed sources: the whole tolerance band must lie in
    the accepted window (good if inside the good window). Adjustable sources: the nominal value
    must be in the good (good) or accepted (fallback) window and a +-``_margin(t)`` band around
    it inside the accepted window, so every point of a setup tolerates that setting error
    (position/level limits are checked at the nominal value)."""
    if (c.sign < 0) != t.negative:
        return 0
    d = c.mag / vdiv
    p = position_for(d, t.negative)
    if isinstance(c.source, FixedSource):
        tol = c.source.tolerance_pct / 100
        lo, hi = d * (1 - tol), d * (1 + tol)
        good = _within(lo, hi, t.good)
        level_d = hi
    else:
        m = _margin(t)
        lo, hi = d * (1 - m), d * (1 + m)
        good = _within(d, d, t.good)
        level_d = d  # the 6 % negative headroom already allows for a setting error
    if not _within(lo, hi, t.accepted) or not level_ok(p, level_d, t.negative):
        return 0
    return 2 if good else 1


def _usable_range(s: Source, cap: float) -> tuple[float, float] | None:
    if isinstance(s, FixedSource):
        return (s.volts, s.volts) if s.volts <= cap + _EPS else None
    hi = min(s.max_v, cap)
    return (s.min_v, hi) if s.min_v <= hi else None


def _candidates(
    sources: Sequence[Source], targets: Sequence[Target], options: PlanOptions
) -> list[_Cand]:
    out: dict[tuple[int, int, float], _Cand] = {}
    for i, s in enumerate(sources):
        rng = _usable_range(s, options.max_abs_v)
        if rng is None:
            continue
        signs = (1, -1) if s.reversible else (1,)
        mags: list[float] = []
        if isinstance(s, FixedSource):
            mags.append(s.volts)
        else:
            for t in targets:
                for v in options.ranges:
                    m = _nice(t.target * v)
                    if rng[0] - _EPS <= m <= rng[1] + _EPS and m > 0:
                        mags.append(m)
        for m in mags:
            for sg in signs:
                key = (i, sg, m)
                if key not in out:
                    out[key] = _Cand(i, s, sg, m)
    cands = list(out.values())
    for c in cands:
        for v in options.ranges:
            for t in targets:
                q = _quality(c, v, t)
                if q:
                    c.cover[(v, t.purpose)] = q
    return [c for c in cands if c.cover]


def _greedy(cands: list[_Cand]) -> list[_Cand]:
    best: dict[Item, int] = {}
    used: set[tuple[int, int]] = set()
    chosen: list[_Cand] = []
    remaining = list(cands)
    while remaining:

        def key(c: _Cand) -> tuple[int, int, int, float, int]:
            gain = sum(max(0, q - best.get(it, 0)) for it, q in c.cover.items())
            return (gain, int(c.fixed), int((c.src_idx, c.sign) in used), -c.mag, -c.src_idx)

        top = max(remaining, key=key)
        if key(top)[0] <= 0:
            break
        chosen.append(top)
        remaining.remove(top)
        used.add((top.src_idx, top.sign))
        for it, q in top.cover.items():
            best[it] = max(best.get(it, 0), q)
    return chosen


# --------------------------------------------------------------------------------------------
# Setups
# --------------------------------------------------------------------------------------------


@dataclass
class _Draft:
    cand: _Cand
    points: list[MeasurePoint]
    kind: str = "reference"


def _point(c: _Cand, vdiv: float, purpose: str) -> MeasurePoint:
    d = c.mag / vdiv
    neg = c.sign < 0
    return MeasurePoint(vdiv, position_for(d, neg), c.sign * d, purpose)


def _target_by_purpose(purpose: str) -> Target:
    for t in TARGETS:
        if t.purpose == purpose:
            return t
    raise KeyError(purpose)


def _window(purpose: str) -> tuple[float, float]:
    if purpose == "verify":
        return VERIFY_WINDOW
    return _target_by_purpose(purpose).accepted


def _point_accept_v(p: MeasurePoint) -> tuple[float, float] | None:
    neg = p.expected_div < 0
    iv = accept_interval(_window(p.purpose), p.position_div, neg)
    if iv is None:
        return None
    return (iv[0] * p.vdiv, iv[1] * p.vdiv)


def _intersect(ivs: Sequence[tuple[float, float] | None]) -> tuple[float, float] | None:
    lo, hi = -math.inf, math.inf
    for iv in ivs:
        if iv is None:
            return None
        lo, hi = max(lo, iv[0]), min(hi, iv[1])
    return (lo, hi) if lo <= hi else None


def _signed(iv: tuple[float, float] | None, sign: int) -> tuple[float, float] | None:
    if iv is None:
        return None
    a, b = round(iv[0], 4), round(iv[1], 4)
    return (a, b) if sign > 0 else (-b, -a)


def _instruction_reference(s: Setup, source: Source, swap: bool, first_of_source: bool) -> str:
    v = s.volts or 0.0
    parts: list[str] = []
    polarity = (
        "probe tip on + and the ground clip on -"
        if v > 0
        else "probe tip on - and the ground clip on + (reversed)"
    )
    if swap:
        parts.append(
            f"Swap the leads at {source.label}: {polarity}. Only reverse floating sources."
        )
    elif first_of_source:
        parts.append(f"Connect {source.label} to CH1: {polarity}.")
    else:
        parts.append(f"Keep {source.label} connected ({polarity}).")
    if isinstance(source, AdjustableSource):
        acc = s.accept_v
        rng = f" (allowed {_fmt_v(acc[0])}..{_fmt_v(acc[1])} V)" if acc else ""
        parts.append(f"Set it to {_fmt_v(v)} V{rng}.")
    else:
        parts.append(f"Nominal {_fmt_v(v)} V.")
    parts.append(
        "Measure the voltage at the probe tip with the multimeter and type the reading "
        f"(it is measured at {', '.join(_vlabel(p.vdiv) + '/div' for p in s.points)})."
    )
    return " ".join(parts)


def _verify_setup(last: Setup, source: Source, options: PlanOptions) -> Setup | None:
    assert last.volts is not None
    sign = 1 if last.volts > 0 else -1
    mag = abs(last.volts)
    neg = sign < 0
    cands: list[tuple[float, float]] = []
    for v in options.ranges:
        d = mag / v
        if VERIFY_WINDOW[0] - _EPS <= d <= VERIFY_WINDOW[1] + _EPS:
            cands.append((abs(d - 3.5), v))
    if not cands:
        return None
    picked = sorted(v for _, v in sorted(cands)[:VERIFY_MAX_RANGES])
    points: list[MeasurePoint] = []
    for v in sorted(picked, reverse=True):
        d = mag / v
        p0 = position_for(d, neg)
        p = p0
        for shift in (VERIFY_SHIFT_DIV, -VERIFY_SHIFT_DIV):
            if level_ok(p0 + shift, d, neg):
                p = p0 + shift
                break
        points.append(MeasurePoint(v, p, sign * d, "verify"))
    acc = _signed(_intersect([_point_accept_v(p) for p in points]), sign)
    instr = (
        f"Verify: keep {source.label} connected as in the last step"
        + (" (same setting)" if isinstance(source, AdjustableSource) else "")
        + ". Re-read the multimeter and type the reading; the scope now measures it through the "
        "new calibration at different positions."
    )
    return Setup(
        "",
        "verify",
        source.label,
        last.volts,
        isinstance(source, AdjustableSource),
        instr,
        tuple(points),
        acc,
    )


def _trimmer_setup(sources: Sequence[Source], options: PlanOptions, warnings: list[str]) -> Setup:
    tc = options.trimmer
    v = tc.vdiv
    base = tc.instruction
    if tc.reference == "source":
        best: tuple[tuple[int, int, float, int], Source, float] | None = None
        for i, s in enumerate(sources):
            if tc.source_label is not None and s.label != tc.source_label:
                continue
            rng = _usable_range(s, options.max_abs_v)
            if rng is None:
                continue
            if isinstance(s, FixedSource):
                mag = s.volts
            else:
                mag = min(max(_nice(TRIMMER_TARGET_DIV * v), rng[0]), rng[1])
            d = mag / v
            if not 0 < d <= POS_LEVEL_MAX - POS_GROUND_MIN:
                continue
            in_win = TRIMMER_WINDOW[0] - _EPS <= d <= TRIMMER_WINDOW[1] + _EPS
            k = (
                int(not in_win),
                int(not isinstance(s, FixedSource)),
                abs(d - TRIMMER_TARGET_DIV),
                i,
            )
            if best is None or k < best[0]:
                best = (k, s, mag)
        if tc.source_label is not None and best is None:
            raise ValueError(
                f"trimmer source {tc.source_label!r} is not a usable source at {_vlabel(v)}/div"
            )
        if best is not None:
            _, s, mag = best
            d = mag / v
            if not TRIMMER_WINDOW[0] - _EPS <= d <= TRIMMER_WINDOW[1] + _EPS:
                warnings.append(
                    f"trimmer reference {s.label} gives {d:.2g} div at {_vlabel(v)}/div "
                    "(3-6 div preferred, e.g. a 1.5 V battery at 0.5 V/div)"
                )
            p = tc.position_div if tc.position_div is not None else position_for(d, False)
            adj = isinstance(s, AdjustableSource)
            what = (
                f"set {s.label} to {_fmt_v(mag)} V and connect it" if adj else f"connect {s.label}"
            )
            instr = (
                f"{base} Now {what} to CH1 (probe tip on +, ground clip on -), measure it with "
                "the multimeter and type the reading; the live readout then shows the scope's "
                f"DC mean at {_vlabel(v)}/div DC and its deviation from your reading in mV and %."
            )
            return Setup(
                "", "trimmer", s.label, mag, adj, instr, (MeasurePoint(v, p, d, "trimmer"),), None
            )
        warnings.append("trimmer step: no usable reference source; using an open input (0 V)")
    p = tc.position_div if tc.position_div is not None else 0.0
    instr = (
        f"{base} Reference: open input. Disconnect the probe from every source; the live "
        f"readout shows the deviation from 0 V at {_vlabel(v)}/div."
    )
    return Setup(
        "", "trimmer", None, None, False, instr, (MeasurePoint(v, p, 0.0, "trimmer"),), None
    )


# --------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------

_QWORD = {2: "good", 1: "fallback", 0: "missing"}


def _coverage(
    options: PlanOptions,
    targets: Sequence[Target],
    q: dict[Item, int],
    sources: Sequence[Source],
    reversible: bool,
) -> tuple[RangeCoverage, ...]:
    cap = options.max_abs_v
    out: list[RangeCoverage] = []
    biggest = max(
        (s.volts if isinstance(s, FixedSource) else s.min_v for s in sources), default=0.0
    )

    def limit_note(lo: float) -> str:
        return f" (above the {cap:g} V limit)" if lo > cap + _EPS else ""

    for v in sorted(options.ranges):
        needs: list[str] = []
        divider = False
        tp = _target_by_purpose("gain_pos")
        pos = _QWORD[q.get((v, "gain_pos"), 0)]
        if pos != "good":
            lo, hi = tp.good[0] * v, tp.good[1] * v
            needs.append(
                f"{_vlabel(v)}/div positive gain: apply +{_fmt_v(lo)}..+{_fmt_v(hi)} V"
                + limit_note(lo)
            )
            divider |= biggest > hi and lo <= cap
        tn = _target_by_purpose("gain_neg")
        if not reversible:
            neg = "not_reversible"
        else:
            neg = _QWORD[q.get((v, "gain_neg"), 0)]
            if neg != "good":
                lo, hi = tn.good[0] * v, tn.good[1] * v
                needs.append(
                    f"{_vlabel(v)}/div negative gain: apply -{_fmt_v(lo)}..-{_fmt_v(hi)} V "
                    "(reversed leads)" + limit_note(lo)
                )
                divider |= biggest > hi and lo <= cap
        nl_targets = [t for t in targets if t.purpose in NONLIN_PURPOSES]
        if options.mode == "quick":
            nonlin = "not_in_mode"
        else:
            qs = [q.get((v, t.purpose), 0) for t in nl_targets]
            nonlin = "good" if all(x == 2 for x in qs) else "partial" if any(qs) else "missing"
            missing = [t for t, x in zip(nl_targets, qs, strict=True) if x == 0]
            if missing and reversible:
                lo = min(t.good[0] for t in missing) * v
                hi = max(t.good[1] for t in missing) * v
                needs.append(
                    f"{_vlabel(v)}/div nonlinearity needs -{_fmt_v(lo)}..-{_fmt_v(hi)} V"
                    + limit_note(lo)
                )
                divider |= biggest > hi and lo <= cap
        if not reversible:
            needs.append("no reversible source: negative side uses the positive gain")
        if divider:
            needs.append(DIVIDER_HINT)
        out.append(RangeCoverage(v, pos, neg, nonlin, tuple(needs)))
    return tuple(out)


# --------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------


def _conn_state(s: Setup) -> tuple[str, int] | None:
    if s.kind == "zero" or s.volts is None or s.source_label is None:
        return None
    return (s.source_label, 1 if s.volts > 0 else -1)


def plan_calibration(sources: Sequence[Source], options: PlanOptions | None = None) -> Plan:
    """Plan a calibration run for ``sources`` (see the module docstring). Raises ``ValueError``
    for invalid sources or options."""
    opts = options if options is not None else PlanOptions()
    srcs = validate_sources(sources)
    warnings: list[str] = []
    cap = opts.max_abs_v
    for s in srcs:
        if isinstance(s, FixedSource) and s.volts > cap:
            warnings.append(
                f"source {s.label} ({s.volts:g} V) is above the {cap:g} V limit; not used"
            )
        elif isinstance(s, AdjustableSource) and s.max_v > cap:
            if s.min_v > cap:
                warnings.append(
                    f"source {s.label} ({s.min_v:g}..{s.max_v:g} V) is above the {cap:g} V "
                    "limit; not used"
                )
            else:
                warnings.append(f"source {s.label}: limited to {cap:g} V for planning")
    reversible = any(s.reversible and _usable_range(s, cap) is not None for s in srcs)
    if reversible:
        warnings.append(FLOATING_WARNING)
    if not srcs:
        warnings.append("no reference sources: only the zero (offset) step is planned")

    targets = [t for t in TARGETS if opts.mode in t.modes]
    cands = _candidates(srcs, targets, opts)
    chosen = _greedy(cands)

    # assignment: every item to its best chosen candidate
    assign: dict[int, list[MeasurePoint]] = {}
    q: dict[Item, int] = {}
    for v in opts.ranges:
        for t in targets:
            it = (v, t.purpose)
            best: tuple[tuple[int, float, int], int] | None = None
            for n, c in enumerate(chosen):
                qq = c.cover.get(it, 0)
                if not qq:
                    continue
                k = (-qq, abs(c.mag / v - t.target), n)
                if best is None or k < best[0]:
                    best = (k, n)
            if best is not None:
                n = best[1]
                q[it] = chosen[n].cover[it]
                assign.setdefault(n, []).append(_point(chosen[n], v, t.purpose))
    drafts: list[_Draft] = []
    for n, c in enumerate(chosen):
        pts = assign.get(n)
        if pts:
            seen: set[tuple[float, float]] = set()
            uniq = []
            for p in pts:  # one measurement serves several purposes at the same range/position
                if (p.vdiv, p.position_div) not in seen:
                    seen.add((p.vdiv, p.position_div))
                    uniq.append(p)
            pts = sorted(uniq, key=lambda p: -p.vdiv)
            drafts.append(_Draft(c, pts))

    # order: by source, positive ascending |V|, then negative descending |V|
    def order(d: _Draft) -> tuple[int, int, float]:
        c = d.cand
        return (c.src_idx, 0 if c.sign > 0 else 1, c.mag if c.sign > 0 else -c.mag)

    drafts.sort(key=order)

    refs: list[Setup] = []
    prev: tuple[int, int] | None = None
    for d in drafts:
        c = d.cand
        acc = _signed(_intersect([_point_accept_v(p) for p in d.points]), c.sign)
        st = Setup("", "reference", c.source.label, c.volts, not c.fixed, "", tuple(d.points), acc)
        swap = prev is not None and prev[0] == c.src_idx and prev[1] != c.sign
        first = prev is None or prev[0] != c.src_idx
        refs.append(_replace(st, instruction=_instruction_reference(st, c.source, swap, first)))
        prev = (c.src_idx, c.sign)

    verify: list[Setup] = []
    if opts.verify and refs:
        last = refs[-1]
        src = next(x for x in srcs if x.label == last.source_label)
        vs = _verify_setup(last, src, opts)
        if vs is not None:
            verify.append(vs)

    ground_pts = sorted(
        {(p.vdiv, p.position_div) for s in (*refs, *verify) for p in s.points},
        key=lambda x: (-x[0], x[1]),
    )
    zero: list[Setup] = []
    if opts.zero:
        zero.append(
            Setup(
                "",
                "zero",
                None,
                None,
                False,
                "Disconnect the probe from every source (leave the tip open or clip it to the "
                "probe's own ground clip), then confirm. The wizard measures the offsets and "
                f"the ground level at {len(ground_pts)} range/position combinations used later.",
                tuple(MeasurePoint(v, p, 0.0, "ground") for v, p in ground_pts),
                None,
            )
        )
    trim: list[Setup] = []
    if opts.trimmer.enabled:
        trim.append(_trimmer_setup(srcs, opts, warnings))
    if opts.trimmer.placement == "first":
        ordered = [*trim, *zero, *refs, *verify]
    else:
        ordered = [*zero, *trim, *refs, *verify]
    setups = tuple(_replace(s, id=f"S{i + 1:02d}") for i, s in enumerate(ordered))

    # effort
    reconnections = 0
    state: tuple[str, int] | None = None
    for st in setups:
        new = _conn_state(st)
        if st.kind == "trimmer" and st.volts is None:
            new = None
        if state is not None and new != state:
            reconnections += 1
        state = new
    user_actions = sum(1 for s in setups if s.needs_reading)
    n_points = sum(len(s.points) for s in setups if s.kind != "zero")
    est = EST_S_PER_ACTION * user_actions + EST_S_PER_POINT * n_points
    if zero:
        est += EST_S_ZERO + EST_S_PER_GROUND_POINT * len(ground_pts)

    coverage = _coverage(opts, targets, q, srcs, reversible)
    return Plan(
        setups=setups,
        coverage=coverage,
        sources=srcs,
        options=opts,
        reconnections=reconnections,
        user_actions=user_actions,
        estimated_s=round(est, 1),
        warnings=tuple(warnings),
    )


def _replace(s: Setup, **kw: object) -> Setup:
    import dataclasses

    return dataclasses.replace(s, **kw)  # type: ignore[arg-type]
