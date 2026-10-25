"""Unit conversions: calibration, counts <-> volts, offset DAC, trigger byte, time axis.

``settings_to_hwconfig`` is the ONLY path from user ``Settings`` to configuration bytes.

Count domains (protocol.md 5.2/5.3):

* **raw**: the unsigned 8-bit sample as it arrives in the frame (0..255).
* **ad**: ``255 - raw``. The original calibration measures ``AD = 255 - mean(raw)`` and fits the
  ground level in this domain: ``gnd_ad = k * dac + b`` where ``dac = base_ofs + ypos`` (the
  range correction is *not* part of the fit, as in ``y1_viiva``). ``GndFit`` stores (k, b).

Volts are ``(raw - gnd_raw) * volts_per_div * gain / counts_per_div``, negated when the ADC is
inverted (``Calibration.adc_inverted``, default ``tables.ADC_INVERTED``; open question Q3).
``gain = Calibration.range_gain_for(volts_per_div)`` (default 1.0) is the per-range gain correction
``V_true / V_read``; equivalently the effective counts per division of a range is
``counts_per_div / gain`` (``Calibration.effective_counts_per_div``).

Calibration v2 (docs/specs/calibration_wizard.md 1): above ground the *positive* gain
``range_gain`` applies; below ground the negative gain ``range_gain_neg`` (falling back to the
positive one) plus the optional piecewise-linear expansion correction ``neg_nonlinearity``. The
single place where this happens is :func:`deflection_to_volts` / :func:`volts_to_deflection`
(exact inverses, strictly increasing, continuous at ground). ``counts_to_volts``,
``volts_to_counts``, ``volts_to_raw`` and ``trigger_byte`` all go through them. The position ->
offset DAC mapping keeps the positive effective counts per division. With the v2 fields empty
every result is bitwise identical to v1.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import EllipsisType
from typing import Any, Literal, NamedTuple, get_args

import numpy as np
import numpy.typing as npt

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
from edu09.protocol.commands import HwConfig

_CAL_KEYS: frozenset[str] = frozenset(
    {
        "base_ofs",
        "range_corr",
        "gnd_fit",
        "counts_per_div",
        "adc_inverted",
        "source",
        "range_gain",
        "range_gain_neg",
        "neg_nonlinearity",
        "meta",
    }
)
_V2_KEYS: frozenset[str] = frozenset({"range_gain_neg", "neg_nonlinearity", "meta"})
"""Keys that only exist in calibration file version 2 (``storage.save_calibration``)."""

__all__ = [
    "Calibration",
    "CalibrationSource",
    "GndFit",
    "Knot",
    "counts_to_volts",
    "deflection_to_volts",
    "expected_acq_mode",
    "flags_byte",
    "gnd_counts",
    "offset_dac",
    "sample_interval",
    "settings_to_hwconfig",
    "time_axis",
    "timebase_entry",
    "trigger_byte",
    "trigger_index",
    "validate_nonlinearity_knots",
    "volts_to_counts",
    "volts_to_deflection",
    "volts_to_raw",
]

Knot = tuple[float, float]
"""One negative-nonlinearity knot ``(r, e)`` in divisions (see ``Calibration``)."""

CalibrationSource = Literal["default", "typical", "file", "ini-import", "measured"]
_SOURCES: tuple[str, ...] = get_args(CalibrationSource)
_ADC_FULL = tables.ADC_MAX  # 255

FloatArray = npt.NDArray[np.float64]


# --------------------------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------------------------


class GndFit(NamedTuple):
    """Ground level fit in the ``ad`` domain: ``gnd_ad = k * dac + b`` (protocol.md 5.3)."""

    k: float = 2.0
    b: float = -128.0  # default: dac 0x80 -> gnd_ad 128 (mid-scale), slope 2 (demo mode)
    # NB: real units measure k ~1.03 (tables.TYPICAL_GND_K); see Calibration.typical()

    @classmethod
    def from_points(cls, points: Sequence[tuple[float, float]]) -> GndFit:
        """Fit from exactly three ``(dac, ad)`` points with the original averaging formula."""
        if len(points) != 3:
            raise CalibrationError(f"need exactly 3 (dac, ad) points, got {len(points)}")
        (p0, a0), (p1, a1), (p2, a2) = ((float(p), float(a)) for p, a in points)
        if len({p0, p1, p2}) != 3:
            raise CalibrationError(f"calibration DAC points must differ: {p0}, {p1}, {p2}")
        k = ((a2 - a0) / (p2 - p0) + (a2 - a1) / (p2 - p1) + (a1 - a0) / (p1 - p0)) / 3
        b = ((a0 - k * p0) + (a1 - k * p1) + (a2 - k * p2)) / 3
        if not (math.isfinite(k) and math.isfinite(b)) or k == 0:
            raise CalibrationError(f"degenerate ground fit k={k!r}, b={b!r}")
        return cls(k, b)


def _int_in(value: object, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationError(f"{what} must be an int, got {value!r}")
    if not lo <= value <= hi:
        raise CalibrationError(f"{what} {value} out of range {lo}..{hi}")
    return value


@dataclass(frozen=True)
class Calibration:
    """Host-side calibration (the device itself only receives offset DAC bytes).

    ``range_corr`` maps a volts/div value to a signed DAC correction added to the offset DAC
    (missing entries mean 0). It is excluded from ``hash()`` (dicts are unhashable) but compared
    by ``==``.

    ``range_gain`` maps a volts/div value to the **positive-side** gain correction
    ``g+ = V_true / V_read`` measured against a reference (``tables.RANGE_GAIN_MIN < g <
    tables.RANGE_GAIN_MAX``; missing entries mean 1.0). Above ground volts are multiplied by
    ``g+``; the effective counts per division is ``counts_per_div / g+`` (also used for the
    position -> offset DAC mapping). Excluded from ``hash()`` like ``range_corr``.

    ``range_gain_neg`` (v2) is the negative-side linear gain ``g-`` with the same limits. A missing
    range falls back to ``range_gain_for`` (v1 behaviour: one gain for both sides).

    ``neg_nonlinearity`` (v2) maps a volts/div value to knots ``(r, e)`` (:data:`Knot`) of the
    negative-side expansion. With ``c >= 0`` counts below ground, ``r = c * g- / counts_per_div``
    is the linear-corrected read deflection (divisions) and the true deflection is
    ``d = r - E(r)``: ``E`` is piecewise linear through the knots, 0 for ``r <= r_0`` and
    extrapolated with the last segment's slope beyond the last knot. Validation rules
    (:func:`validate_nonlinearity_knots`, limits ``tables.NL_*``): 2..8 knots, ``e_0 == 0``,
    ``1 <= r_0``, ``r`` strictly increasing and ``<= 10``, ``|e| <= 2`` and every slope
    ``de/dr`` within +-0.5 (so ``d(r)`` is strictly increasing). ``None`` is accepted as ``{}``.

    ``meta`` (v2) is provenance (a JSON object of at most ``tables.CAL_META_MAX_BYTES``). It is
    NOT compared, NOT hashed and NOT part of :attr:`calibration_id`.

    ``Calibration()`` is the idealised model of the original's demo mode (base 0x80, slope 2
    counts per DAC step: 0 V -> raw 127). The simulator's front end and the test fixtures use it.
    A real unit is better described by :meth:`typical` (HW-measured slope ~1.03), which
    ``Scope.open`` uses for hardware when no calibration file exists.
    """

    base_ofs: int = tables.OFFSET_DAC_DEFAULT
    range_corr: Mapping[float, int] = field(default_factory=dict, hash=False)
    gnd_fit: GndFit = GndFit()
    counts_per_div: float = tables.COUNTS_PER_DIV
    adc_inverted: bool = tables.ADC_INVERTED
    source: CalibrationSource = "default"
    range_gain: Mapping[float, float] = field(default_factory=dict, hash=False)
    neg_nonlinearity: Mapping[float, tuple[Knot, ...]] = field(default_factory=dict, hash=False)
    range_gain_neg: Mapping[float, float] = field(default_factory=dict, hash=False)
    meta: Mapping[str, Any] | None = field(default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        _int_in(self.base_ofs, tables.OFFSET_DAC_MIN, tables.OFFSET_DAC_MAX, "base_ofs")
        if not isinstance(self.range_corr, Mapping):
            raise CalibrationError("range_corr must be a mapping of volts/div -> int")
        corr: dict[float, int] = {}
        for key, val in self.range_corr.items():
            try:
                vdiv = float(VoltsPerDiv.parse(key))
            except ValueError as exc:
                raise CalibrationError(f"range_corr key: {exc}") from None
            corr[vdiv] = _int_in(val, -_ADC_FULL, _ADC_FULL, f"range_corr[{vdiv:g}]")
        object.__setattr__(self, "range_corr", corr)
        try:
            fit = GndFit(float(self.gnd_fit[0]), float(self.gnd_fit[1]))
        except (TypeError, ValueError, IndexError):
            raise CalibrationError(f"gnd_fit must be (k, b), got {self.gnd_fit!r}") from None
        if not (math.isfinite(fit.k) and math.isfinite(fit.b)) or fit.k == 0:
            raise CalibrationError(f"gnd_fit must be finite with k != 0, got {fit!r}")
        object.__setattr__(self, "gnd_fit", fit)
        cpd = self.counts_per_div
        if isinstance(cpd, bool) or not isinstance(cpd, int | float) or not cpd > 0:
            raise CalibrationError(f"counts_per_div must be > 0, got {cpd!r}")
        object.__setattr__(self, "counts_per_div", float(cpd))
        if not isinstance(self.adc_inverted, bool):
            raise CalibrationError(f"adc_inverted must be a bool, got {self.adc_inverted!r}")
        if self.source not in _SOURCES:
            raise CalibrationError(f"source {self.source!r} not in {_SOURCES}")
        object.__setattr__(self, "range_gain", _parse_range_gain(self.range_gain, "range_gain"))
        object.__setattr__(
            self, "range_gain_neg", _parse_range_gain(self.range_gain_neg, "range_gain_neg")
        )
        object.__setattr__(self, "neg_nonlinearity", _parse_nonlinearity(self.neg_nonlinearity))
        object.__setattr__(self, "meta", _parse_meta(self.meta))

    @classmethod
    def typical(cls) -> Calibration:
        """Measured typical values of a real EDU09 (``source="typical"``, protocol.md 11).

        Fallback for hardware without a calibration file: positions scale correctly (slope
        ~1.03 instead of the default model's 2) and 0 V is within a few counts. The per-range
        gains (``tables.TYPICAL_RANGE_GAIN``) were measured on one unit only. A per-unit
        calibration (``Scope.calibrate`` plus measured gains) is still more accurate.
        """
        return cls(
            base_ofs=tables.TYPICAL_BASE_OFS,
            range_corr=dict(tables.TYPICAL_RANGE_CORR),
            gnd_fit=GndFit(tables.TYPICAL_GND_K, tables.TYPICAL_GND_B),
            source="typical",
            range_gain=dict(tables.TYPICAL_RANGE_GAIN),
        )

    def range_correction(self, vdiv: VoltsPerDiv | float) -> int:
        return self.range_corr.get(float(vdiv), 0)

    def range_gain_for(self, vdiv: VoltsPerDiv | float) -> float:
        """Positive-side gain correction ``V_true / V_read`` of a range (1.0 if not calibrated)."""
        return self.range_gain.get(float(vdiv), 1.0)

    def range_gain_neg_for(self, vdiv: VoltsPerDiv | float) -> float:
        """Negative-side linear gain ``g-`` of a range (falls back to :meth:`range_gain_for`)."""
        g = self.range_gain_neg.get(float(vdiv))
        return self.range_gain_for(vdiv) if g is None else g

    def nonlinearity_for(self, vdiv: VoltsPerDiv | float) -> tuple[Knot, ...]:
        """Negative-side nonlinearity knots ``(r, e)`` of a range (``()`` = none)."""
        return self.neg_nonlinearity.get(float(vdiv), ())

    def effective_counts_per_div(self, vdiv: VoltsPerDiv | float) -> float:
        """Raw counts per (true) division above ground: ``counts_per_div / range_gain_for``."""
        return self.counts_per_div / self.range_gain_for(vdiv)

    def with_range_gain(self, gains: Mapping[Any, float]) -> Calibration:
        """Copy with ``gains`` merged into ``range_gain`` (keys as accepted by ``VoltsPerDiv``)."""
        merged: dict[Any, float] = dict(self.range_gain)
        merged.update({float(VoltsPerDiv.parse(k)): v for k, v in gains.items()})
        return dataclasses.replace(self, range_gain=merged)

    def with_gains(
        self,
        *,
        pos: Mapping[Any, float | None] | None = None,
        neg: Mapping[Any, float | None] | None = None,
        nonlinearity: Mapping[Any, Sequence[Sequence[float]] | None] | None = None,
        meta: Mapping[str, Any] | None | EllipsisType = ...,
    ) -> Calibration:
        """Copy with per-range merges into ``range_gain`` (``pos``), ``range_gain_neg`` (``neg``)
        and ``neg_nonlinearity`` (``nonlinearity``). A value of ``None`` (or an empty knot list)
        removes that range's key. ``meta`` replaces the provenance unless left at ``...``
        (keep). Keys are anything ``VoltsPerDiv.parse`` accepts. Raises ``CalibrationError``."""
        changes: dict[str, Any] = {
            "range_gain": _merge(self.range_gain, pos, "range_gain"),
            "range_gain_neg": _merge(self.range_gain_neg, neg, "range_gain_neg"),
            "neg_nonlinearity": _merge(self.neg_nonlinearity, nonlinearity, "neg_nonlinearity"),
        }
        if not isinstance(meta, EllipsisType):
            changes["meta"] = meta
        return dataclasses.replace(self, **changes)

    def without_gain_correction(self) -> Calibration:
        """Copy with all gain maps (``range_gain``, ``range_gain_neg``, ``neg_nonlinearity``)
        empty and ``meta=None``: the raw front end, used for every gain measurement."""
        return dataclasses.replace(
            self, range_gain={}, range_gain_neg={}, neg_nonlinearity={}, meta=None
        )

    @property
    def has_v2_fields(self) -> bool:
        """True if any calibration-file-v2-only field is set (``storage`` then writes v2)."""
        return bool(self.range_gain_neg or self.neg_nonlinearity or self.meta)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (``range_corr``/``range_gain`` keys become strings such as ``"0.1"``).

        The v2 keys ``range_gain_neg``, ``neg_nonlinearity`` (``{"0.5": [[4.0, 0.0], ...]}``)
        and ``meta`` are only present when non-empty, so a v1 calibration keeps its v1 dict.
        """
        d: dict[str, Any] = {
            "base_ofs": self.base_ofs,
            "range_corr": {f"{k:g}": v for k, v in sorted(self.range_corr.items())},
            "gnd_fit": {"k": self.gnd_fit.k, "b": self.gnd_fit.b},
            "counts_per_div": self.counts_per_div,
            "adc_inverted": self.adc_inverted,
            "source": self.source,
            "range_gain": {f"{k:g}": v for k, v in sorted(self.range_gain.items())},
        }
        if self.range_gain_neg:
            d["range_gain_neg"] = {f"{k:g}": v for k, v in sorted(self.range_gain_neg.items())}
        if self.neg_nonlinearity:
            d["neg_nonlinearity"] = {
                f"{k:g}": [[r, e] for r, e in knots]
                for k, knots in sorted(self.neg_nonlinearity.items())
            }
        if self.meta:
            d["meta"] = json.loads(json.dumps(self.meta))
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Calibration:
        """Inverse of :meth:`to_dict`. Missing keys take defaults; bad data -> CalibrationError.

        Unknown keys are rejected (also legacy opaque ``neg_nonlinearity`` data that does not
        follow the knot format).
        """
        if not isinstance(d, Mapping):
            raise CalibrationError(f"calibration must be a mapping, got {type(d).__name__}")
        unknown = sorted(set(d) - _CAL_KEYS)
        if unknown:
            raise CalibrationError(f"unknown calibration key(s) {unknown}")
        kwargs: dict[str, Any] = dict(d)
        if "gnd_fit" in kwargs:
            g = kwargs["gnd_fit"]
            try:
                kwargs["gnd_fit"] = GndFit(g["k"], g["b"]) if isinstance(g, Mapping) else GndFit(*g)
            except (KeyError, TypeError) as exc:
                raise CalibrationError(f"gnd_fit must be {{k, b}}, got {g!r}") from exc
        return cls(**kwargs)

    @property
    def calibration_id(self) -> str:
        """Stable id ``"<source>:<10 hex>"`` derived from the calibration values (not ``meta``)."""
        d = self.to_dict()
        d.pop("meta", None)
        payload = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return f"{self.source}:{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


def _vdiv_key(key: object, what: str) -> float:
    try:
        return float(VoltsPerDiv.parse(key))
    except (ValueError, TypeError) as exc:
        raise CalibrationError(f"{what} key: {exc}") from None


def _merge(
    current: Mapping[float, Any], updates: Mapping[Any, Any] | None, what: str
) -> dict[float, Any]:
    merged: dict[float, Any] = dict(current)
    if updates is None:
        return merged
    if not isinstance(updates, Mapping):
        raise CalibrationError(f"{what} updates must be a mapping of volts/div -> value")
    for key, val in updates.items():
        vdiv = _vdiv_key(key, what)
        if val is None or (isinstance(val, Sequence) and not isinstance(val, str) and not val):
            merged.pop(vdiv, None)
        else:
            merged[vdiv] = val
    return merged


def _parse_range_gain(value: object, what: str = "range_gain") -> dict[float, float]:
    if not isinstance(value, Mapping):
        raise CalibrationError(f"{what} must be a mapping of volts/div -> float")
    gains: dict[float, float] = {}
    for key, val in value.items():
        vdiv = _vdiv_key(key, what)
        if isinstance(val, bool) or not isinstance(val, int | float):
            raise CalibrationError(f"{what}[{vdiv:g}] must be a number, got {val!r}")
        g = float(val)
        if not tables.RANGE_GAIN_MIN < g < tables.RANGE_GAIN_MAX:  # also rejects NaN
            raise CalibrationError(
                f"{what}[{vdiv:g}] {g!r} out of range "
                f"({tables.RANGE_GAIN_MIN:g}, {tables.RANGE_GAIN_MAX:g})"
            )
        gains[vdiv] = g
    return gains


def validate_nonlinearity_knots(knots: object, what: str = "knots") -> tuple[Knot, ...]:
    """Validate and normalise negative-nonlinearity knots ``[(r, e), ...]`` (``Calibration``
    docstring / ``tables.NL_*``). Returns a tuple of float pairs; raises ``CalibrationError``."""
    if isinstance(knots, str | bytes) or not isinstance(knots, Sequence):
        raise CalibrationError(f"{what} must be a list of [r, e] pairs, got {knots!r}")
    pts: list[Knot] = []
    for item in knots:
        if isinstance(item, str | bytes) or not isinstance(item, Sequence) or len(item) != 2:
            raise CalibrationError(f"{what}: each knot must be [r, e], got {item!r}")
        vals: list[float] = []
        for x in item:
            if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
                raise CalibrationError(f"{what}: knot values must be finite numbers, got {item!r}")
            vals.append(float(x))
        pts.append((vals[0], vals[1]))
    if not 2 <= len(pts) <= tables.NL_MAX_KNOTS:
        raise CalibrationError(f"{what}: need 2..{tables.NL_MAX_KNOTS} knots, got {len(pts)}")
    r0, e0 = pts[0]
    if e0 != 0.0:
        raise CalibrationError(f"{what}: the first knot's excess must be 0, got {e0!r}")
    if r0 < tables.NL_MIN_READ_DIV:
        raise CalibrationError(f"{what}: first knot r={r0:g} < {tables.NL_MIN_READ_DIV:g} div")
    for r, e in pts:
        if r > tables.NL_MAX_READ_DIV:
            raise CalibrationError(f"{what}: knot r={r:g} > {tables.NL_MAX_READ_DIV:g} div")
        if abs(e) > tables.NL_MAX_EXCESS_DIV:
            raise CalibrationError(
                f"{what}: knot excess {e:g} beyond +-{tables.NL_MAX_EXCESS_DIV:g} div"
            )
    for (ra, ea), (rb, eb) in itertools.pairwise(pts):
        if not rb > ra:
            raise CalibrationError(f"{what}: knot r must be strictly increasing ({ra:g}, {rb:g})")
        slope = (eb - ea) / (rb - ra)
        if not -tables.NL_MAX_SLOPE <= slope <= tables.NL_MAX_SLOPE:
            raise CalibrationError(
                f"{what}: segment slope {slope:.3g} beyond +-{tables.NL_MAX_SLOPE:g} "
                f"(between r={ra:g} and r={rb:g})"
            )
    return tuple(pts)


def _parse_nonlinearity(value: object) -> dict[float, tuple[Knot, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CalibrationError(
            "neg_nonlinearity must be a mapping of volts/div -> [[r, e], ...], "
            f"got {type(value).__name__}"
        )
    out: dict[float, tuple[Knot, ...]] = {}
    for key, knots in value.items():
        vdiv = _vdiv_key(key, "neg_nonlinearity")
        out[vdiv] = validate_nonlinearity_knots(knots, f"neg_nonlinearity[{vdiv:g}]")
    return out


def _parse_meta(value: object) -> dict[str, Any] | None:
    """Provenance JSON object (deep-copied through JSON; at most ``CAL_META_MAX_BYTES``)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CalibrationError(f"meta must be a JSON object or null, got {type(value).__name__}")
    try:
        text = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"meta is not JSON-serialisable: {exc}") from None
    if len(text.encode()) > tables.CAL_META_MAX_BYTES:
        raise CalibrationError(
            f"meta is {len(text.encode())} bytes as JSON (max {tables.CAL_META_MAX_BYTES})"
        )
    copied = json.loads(text)
    return copied or None


# --------------------------------------------------------------------------------------------
# Timebase helpers
# --------------------------------------------------------------------------------------------


def timebase_entry(tb: Timebase) -> tables.TimebaseEntry:
    return tables.TIMEBASES[float(tb)]


def sample_interval(tb: Timebase) -> float:
    """Seconds per sample (the effective rate for ETS timebases)."""
    return timebase_entry(tb).dt


def expected_acq_mode(tb: Timebase) -> int:
    """Acquisition mode byte the status report must carry for this timebase."""
    return timebase_entry(tb).mode


def trigger_index(tb: Timebase) -> int:
    """Sample index of the trigger point in the time-ordered frame (HW-verified per mode, Q4)."""
    return tables.TRIGGER_INDEX_BY_MODE[expected_acq_mode(tb)]


def time_axis(n: int, tb: Timebase, trigger_index: int | None) -> FloatArray:
    """``(arange(n) - trigger_index) * dt`` in seconds; ``None`` index means t=0 at sample 0."""
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    return (np.arange(n, dtype=np.float64) - (trigger_index or 0)) * sample_interval(tb)


# --------------------------------------------------------------------------------------------
# Vertical: offset DAC, ground level, counts <-> volts
# --------------------------------------------------------------------------------------------


def _clamp_byte(x: int) -> int:
    return min(max(x, tables.OFFSET_DAC_MIN), tables.OFFSET_DAC_MAX)


def _ypos_steps(ch: ChannelSettings, cal: Calibration) -> int:
    """Position (divisions) -> offset DAC steps through the ground fit slope.

    A positive ``position_div`` moves ground up, i.e. towards larger voltages:
    raw shift = +position * effective counts_per_div (negated for an inverted ADC), and
    ``d gnd_raw / d dac = -k``. Using the gain-corrected counts per division keeps the ADC
    window centred on the screen.
    """
    cpd = cal.effective_counts_per_div(ch.volts_per_div)
    raw_shift = ch.position_div * cpd * (-1.0 if cal.adc_inverted else 1.0)
    steps = round(-raw_shift / cal.gnd_fit.k)
    return int(min(max(steps, -128), 127))


def offset_dac(ch: ChannelSettings, cal: Calibration) -> int:
    """CH1OFS byte: ``base_ofs + range_corr + ypos`` clamped to 0..255 (protocol.md 5.3)."""
    return _clamp_byte(cal.base_ofs + cal.range_correction(ch.volts_per_div) + _ypos_steps(ch, cal))


def gnd_counts(ch: ChannelSettings, cal: Calibration) -> float:
    """Ground (0 V) level in the raw sample domain (float, not rounded)."""
    dac = _clamp_byte(cal.base_ofs + _ypos_steps(ch, cal))
    return _ADC_FULL - (cal.gnd_fit.k * dac + cal.gnd_fit.b)


def _vpc_pos_neg(vdiv: float, cal: Calibration) -> tuple[float, float]:
    """Volts per count above / below ground (same operation order as v1: bitwise identical)."""
    pos = vdiv / cal.effective_counts_per_div(vdiv)
    neg = vdiv / (cal.counts_per_div / cal.range_gain_neg_for(vdiv))
    return pos, neg


def _scalar_like(x: npt.ArrayLike) -> FloatArray:
    """0-d results become ``np.float64`` scalars (as v1 returned for scalar input)."""
    arr = np.asarray(x, dtype=np.float64)
    return arr[()] if arr.ndim == 0 else arr


def _knot_arrays(knots: tuple[Knot, ...]) -> tuple[FloatArray, FloatArray, float]:
    rk = np.array([k[0] for k in knots], dtype=np.float64)
    ek = np.array([k[1] for k in knots], dtype=np.float64)
    tail = float((ek[-1] - ek[-2]) / (rk[-1] - rk[-2]))
    return rk, ek, tail


def _nl_excess(r: FloatArray, knots: tuple[Knot, ...]) -> FloatArray:
    """``E(r)``: 0 for ``r <= r_0``, linear through the knots, last slope beyond the last knot."""
    rk, ek, tail = _knot_arrays(knots)
    inner = np.interp(r, rk, ek)  # left of r_0: e_0 == 0
    return np.where(r > rk[-1], ek[-1] + tail * (r - rk[-1]), inner)


def _nl_read(d: FloatArray, knots: tuple[Knot, ...]) -> FloatArray:
    """Inverse of ``d = r - E(r)``: true deflection -> read deflection (knots ``(r-e, r)``)."""
    rk, ek, tail = _knot_arrays(knots)
    dk = rk - ek  # strictly increasing: every slope de/dr <= NL_MAX_SLOPE < 1
    inner = np.interp(d, dk, rk)
    r = np.where(d > dk[-1], rk[-1] + (d - dk[-1]) / (1.0 - tail), inner)
    return np.where(d < dk[0], d, r)  # dk[0] == rk[0] (e_0 == 0): identity below the first knot


def deflection_to_volts(
    dev_counts: npt.ArrayLike, vdiv: VoltsPerDiv | float, cal: Calibration
) -> FloatArray:
    """Counts relative to ground (``+`` = more positive volts) -> volts, the single place where
    the per-range gains and the negative nonlinearity are applied.

    ``dev >= 0``: ``v = dev * vdiv * g+ / cpd``. ``dev < 0``: ``r = -dev * g- / cpd`` (read
    deflection in divisions) and ``v = -(r - E(r)) * vdiv`` (``E`` from
    ``Calibration.nonlinearity_for``). Vectorised float64; strictly increasing and continuous at
    0; :func:`volts_to_deflection` is its exact inverse.
    """
    dev = np.asarray(dev_counts, dtype=np.float64)
    v = float(vdiv)
    vpc_pos, vpc_neg = _vpc_pos_neg(v, cal)
    out = dev * np.where(dev >= 0, vpc_pos, vpc_neg)
    knots = cal.nonlinearity_for(v)
    if knots:
        neg = dev < 0
        r = np.where(neg, -dev * (cal.range_gain_neg_for(v) / cal.counts_per_div), 0.0)
        out = out + np.where(neg, _nl_excess(r, knots) * v, 0.0)
    return _scalar_like(out)


def volts_to_deflection(
    volts: npt.ArrayLike, vdiv: VoltsPerDiv | float, cal: Calibration
) -> FloatArray:
    """Volts -> counts relative to ground (exact inverse of :func:`deflection_to_volts`)."""
    vv = np.asarray(volts, dtype=np.float64)
    v = float(vdiv)
    vpc_pos, vpc_neg = _vpc_pos_neg(v, cal)
    out = vv / np.where(vv >= 0, vpc_pos, vpc_neg)
    knots = cal.nonlinearity_for(v)
    if knots:
        d = np.where(vv < 0, -vv / v, 0.0)
        bent = d > knots[0][0]
        r = _nl_read(d, knots)
        out = np.where(bent, -r * (cal.counts_per_div / cal.range_gain_neg_for(v)), out)
    return _scalar_like(out)


def _sign(cal: Calibration) -> float:
    return -1.0 if cal.adc_inverted else 1.0


def counts_to_volts(raw: npt.ArrayLike, ch: ChannelSettings, cal: Calibration) -> FloatArray:
    """Raw samples -> volts. GND coupling yields exact zeros (the hardware runs DC)."""
    arr = np.asarray(raw, dtype=np.float64)
    if ch.coupling is Coupling.GND:
        return np.zeros_like(arr)
    return deflection_to_volts((arr - gnd_counts(ch, cal)) * _sign(cal), ch.volts_per_div, cal)


def volts_to_counts(volts: npt.ArrayLike, ch: ChannelSettings, cal: Calibration) -> FloatArray:
    """Volts -> raw counts as float64, unrounded and unclipped (exact inverse of the DC path)."""
    dev = volts_to_deflection(volts, ch.volts_per_div, cal)
    return gnd_counts(ch, cal) + _sign(cal) * dev


def volts_to_raw(
    volts: npt.ArrayLike, ch: ChannelSettings, cal: Calibration
) -> npt.NDArray[np.uint8]:
    """Volts -> uint8 samples as the ADC would produce them (rounded, saturating at 0/255)."""
    c = np.rint(volts_to_counts(volts, ch, cal))
    return np.clip(c, tables.ADC_MIN, tables.ADC_MAX).astype(np.uint8)


# --------------------------------------------------------------------------------------------
# Trigger and flags
# --------------------------------------------------------------------------------------------


def trigger_byte(trig: TriggerSettings, ch: ChannelSettings, cal: Calibration) -> int:
    """Trigger level byte (payload byte 7): ``level_v`` as a raw count, rounded and clamped."""
    c = float(volts_to_counts(trig.level_v, ch, cal))
    b = int(min(max(round(c), tables.ADC_MIN), tables.ADC_MAX))
    return _ADC_FULL - b if tables.TRIGGER_BYTE_INVERTED else b


def flags_byte(trig: TriggerSettings) -> int:
    """FLAGS byte (payload byte 9): CH1 source, enable unless mode NONE, edge bit."""
    flags = 0  # trigger source CH1 (FLAG_TRIG_SRC_CH2 clear), digital mode off
    if trig.mode is not TriggerMode.NONE:
        flags |= tables.FLAG_TRIG_ENABLE
    if trig.edge is TriggerEdge.FALLING:
        flags |= tables.FLAG_EDGE_FALLING
    return flags


def settings_to_hwconfig(s: Settings, cal: Calibration) -> HwConfig:
    """The ONLY Settings -> config-bytes path.

    CH1 GND coupling is sent as DC (no hardware bit); CH2 bytes are the neutral constants.
    AUTO and NORMAL both enable the hardware trigger (AUTO's free-run fallback is the worker's job).
    """
    ch = s.ch1
    gain1 = tables.GAIN_CODES_CH1[float(ch.volts_per_div)]
    if ch.coupling is Coupling.AC:
        gain1 |= tables.CH1_AC_BIT
    return HwConfig(
        gain1=gain1,
        gain2=tables.CH2_NEUTRAL_CFG,
        ofs1=offset_dac(ch, cal),
        ofs2=tables.CH2_NEUTRAL_OFS,
        trig=trigger_byte(s.trigger, ch, cal),
        tb=timebase_entry(s.timebase).hw_code,
        flags=flags_byte(s.trigger),
    )
