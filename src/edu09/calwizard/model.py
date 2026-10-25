"""Calibration wizard data model (docs/specs/calibration_wizard.md 2 and 3).

Pure, immutable dataclasses shared by the planner, the session and the front ends (CLI, GUI,
MCP): reference sources, plan options, the plan itself, and the session's views and results.
Also the source profile persistence (``calibration_sources.json`` in the user config dir).
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, get_args

from edu09.model import VoltsPerDiv
from edu09.protocol import tables

__all__ = [
    "ALL_RANGES",
    "DEFAULT_TRIMMER_TEXT",
    "FLOATING_WARNING",
    "INPUT_IMPEDANCE_OHM",
    "MAX_SOURCES",
    "SOURCES_FILENAME",
    "TARGETS",
    "AdjustableSource",
    "FixedSource",
    "MeasurePoint",
    "Plan",
    "PlanOptions",
    "PointResult",
    "QualityCode",
    "RangeCoverage",
    "SaveResult",
    "SessionState",
    "Setup",
    "SetupPhase",
    "SetupResult",
    "SetupView",
    "Source",
    "Target",
    "TrimmerConfig",
    "TrimmerReading",
    "default_sources_path",
    "load_sources",
    "options_from_dict",
    "options_to_dict",
    "save_sources",
    "source_from_dict",
    "source_to_dict",
    "target",
]

log = logging.getLogger(__name__)

Mode = Literal["quick", "standard", "full"]
Uncalibrated = Literal["keep", "typical", "unity"]
SetupKind = Literal["trimmer", "zero", "reference", "verify"]

MAX_SOURCES: Final = 8
MAX_LABEL_LEN: Final = 40
MAX_TOLERANCE_PCT: Final = 25.0
INPUT_ABS_MAX_V: Final = tables.INPUT_ABS_MAX_V
"""Maximum input magnitude of the EDU09 (AC + DC, manual). References are capped at this."""
INPUT_IMPEDANCE_OHM: Final = 100e3
"""Input impedance of CH1 (manual: 100 kOhm); a resistor divider must be much lower-ohmic."""
DEFAULT_MAX_ABS_V: Final = 15.0
ALL_RANGES: Final[tuple[float, ...]] = tuple(float(v) for v in VoltsPerDiv)
"""All six V/div ranges, ascending."""

POS_GROUND_MIN: Final = -3.5
"""Positive points: ground position >= this (div)."""
POS_LEVEL_MAX: Final = 3.5
"""Positive points: level (ground + deflection) <= this (div)."""
NEG_GROUND_MAX: Final = 3.5
"""Negative points: ground position <= this (div)."""
NEG_LEVEL_MIN: Final = -3.75
"""Negative points: ground - NEG_HEADROOM * deflection >= this (div)."""
NEG_HEADROOM: Final = 1.06
"""6 % expansion headroom below ground."""
POSITION_STEP: Final = 0.25
"""Positions are quantised to this many divisions."""

SOURCES_FILENAME: Final = "calibration_sources.json"
SOURCES_FILE_VERSION: Final = 1

FLOATING_WARNING: Final = (
    "Only reverse floating sources (batteries, isolated bench supplies). Never reverse a source "
    "that shares ground with this PC (e.g. USB 5 V from the same computer): the probe ground "
    "clip is the PC ground."
)

DEFAULT_TRIMMER_TEXT: Final = (
    'Trimmer RV1 ("fine tuning of the input amplifier circuit", manual) sets the input '
    "amplifier gain of ALL ranges, so it is adjusted before the gain steps. Vendor procedure: "
    "turn RV1 to its middle position before the zero calibration; then measure a 1.5 V battery "
    "with the multimeter, connect it to the input at 0.5 V/div DC and turn RV1 until the "
    "displayed DC mean equals the multimeter reading. Turn slowly with an insulated screwdriver "
    "and watch the live deviation; press Done when it is as small as you can get it."
)


# --------------------------------------------------------------------------------------------
# Targets (2.2)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A deflection target in *true* divisions (magnitude) and its windows."""

    purpose: str
    target: float
    good: tuple[float, float]
    accepted: tuple[float, float]
    modes: frozenset[str]
    negative: bool


_ALL_MODES = frozenset({"quick", "standard", "full"})
_STD_FULL = frozenset({"standard", "full"})
TARGETS: Final[tuple[Target, ...]] = (
    Target("gain_pos", 5.0, (4.0, 6.0), (2.0, 7.0), _ALL_MODES, False),
    Target("gain_pos2", 2.5, (2.0, 3.0), (2.0, 3.0), frozenset({"full"}), False),
    Target("gain_neg", 3.0, (2.0, 4.0), (1.5, 4.0), _ALL_MODES, True),
    Target("nonlin5", 5.0, (4.6, 5.4), (4.3, 5.6), _STD_FULL, True),
    Target("nonlin6", 6.0, (5.6, 6.4), (5.6, 6.6), _STD_FULL, True),
    Target("nonlin65", 6.5, (6.3, 6.6), (6.3, 6.6), frozenset({"full"}), True),
)
NONLIN_PURPOSES: Final = frozenset({"nonlin5", "nonlin6", "nonlin65"})
VERIFY_WINDOW: Final = (1.5, 6.5)
"""Nominal |deflection| of verify points (and their accepted window)."""
TRIMMER_WINDOW: Final = (3.0, 6.0)
"""Preferred deflection of the trimmer reference (div)."""
TRIMMER_TARGET_DIV: Final = 3.0
"""Deflection the trimmer step aims for (a 1.5 V battery at 0.5 V/div)."""


def target(purpose: str) -> Target:
    for t in TARGETS:
        if t.purpose == purpose:
            return t
    raise KeyError(purpose)


# --------------------------------------------------------------------------------------------
# Sources and options (2.1)
# --------------------------------------------------------------------------------------------


def _finite(x: object, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
        raise ValueError(f"{what} must be a finite number, got {x!r}")
    return float(x)


def _check_label(label: object) -> str:
    if not isinstance(label, str) or not 1 <= len(label) <= MAX_LABEL_LEN:
        raise ValueError(f"source label must be 1-{MAX_LABEL_LEN} characters, got {label!r}")
    if not label.isprintable():
        raise ValueError(f"source label must be printable, got {label!r}")
    return label


@dataclass(frozen=True)
class FixedSource:
    """A fixed voltage (battery, USB 5 V, ...). ``volts`` is a magnitude (> 0)."""

    label: str
    volts: float
    tolerance_pct: float = 5.0
    reversible: bool = False

    def __post_init__(self) -> None:
        _check_label(self.label)
        v = _finite(self.volts, "volts")
        if not v > 0:
            raise ValueError(f"source {self.label!r}: volts must be > 0, got {v:g}")
        object.__setattr__(self, "volts", v)
        tol = _finite(self.tolerance_pct, "tolerance_pct")
        if not 0 <= tol <= MAX_TOLERANCE_PCT:
            raise ValueError(
                f"source {self.label!r}: tolerance_pct must be 0..{MAX_TOLERANCE_PCT:g}"
            )
        object.__setattr__(self, "tolerance_pct", tol)
        if not isinstance(self.reversible, bool):
            raise ValueError("reversible must be a bool")


@dataclass(frozen=True)
class AdjustableSource:
    """An adjustable supply ``min_v..max_v`` (magnitudes, ``0 <= min_v < max_v``)."""

    label: str
    min_v: float
    max_v: float
    reversible: bool = False

    def __post_init__(self) -> None:
        _check_label(self.label)
        lo = _finite(self.min_v, "min_v")
        hi = _finite(self.max_v, "max_v")
        if not 0 <= lo < hi:
            raise ValueError(f"source {self.label!r}: need 0 <= min_v < max_v, got {lo:g}..{hi:g}")
        object.__setattr__(self, "min_v", lo)
        object.__setattr__(self, "max_v", hi)
        if not isinstance(self.reversible, bool):
            raise ValueError("reversible must be a bool")


Source = FixedSource | AdjustableSource


def validate_sources(sources: Sequence[Source]) -> tuple[Source, ...]:
    """Check count and label uniqueness (each source validates itself)."""
    out = tuple(sources)
    if len(out) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} sources, got {len(out)}")
    seen: set[str] = set()
    for s in out:
        if not isinstance(s, FixedSource | AdjustableSource):
            raise ValueError(f"not a source: {s!r}")
        if s.label in seen:
            raise ValueError(f"duplicate source label {s.label!r}")
        seen.add(s.label)
    return out


def source_to_dict(s: Source) -> dict[str, Any]:
    if isinstance(s, FixedSource):
        return {
            "type": "fixed",
            "label": s.label,
            "volts": s.volts,
            "tolerance_pct": s.tolerance_pct,
            "reversible": s.reversible,
        }
    return {
        "type": "adjustable",
        "label": s.label,
        "min_v": s.min_v,
        "max_v": s.max_v,
        "reversible": s.reversible,
    }


def source_from_dict(d: Mapping[str, Any]) -> Source:
    """Inverse of :func:`source_to_dict`. Without ``type``, ``volts`` means fixed."""
    if not isinstance(d, Mapping):
        raise ValueError(f"source must be an object, got {type(d).__name__}")
    kind = d.get("type", "fixed" if "volts" in d else "adjustable")
    rev = d.get("reversible", False)
    if kind == "fixed":
        extra = set(d) - {"type", "label", "volts", "tolerance_pct", "reversible"}
        if extra:
            raise ValueError(f"unknown fixed source key(s) {sorted(extra)}")
        return FixedSource(
            d.get("label", ""), d.get("volts", math.nan), d.get("tolerance_pct", 5.0), rev
        )
    if kind == "adjustable":
        extra = set(d) - {"type", "label", "min_v", "max_v", "reversible"}
        if extra:
            raise ValueError(f"unknown adjustable source key(s) {sorted(extra)}")
        return AdjustableSource(
            d.get("label", ""), d.get("min_v", 0.0), d.get("max_v", math.nan), rev
        )
    raise ValueError(f"unknown source type {kind!r} (fixed, adjustable)")


@dataclass(frozen=True)
class TrimmerConfig:
    """Optional trimmer (RV1, input amplifier gain) step. RV1 changes the gain of every range,
    so by default it runs after the zero step and before all gain steps (``after_zero``)."""

    enabled: bool = False
    placement: Literal["first", "after_zero"] = "after_zero"
    reference: Literal["source", "open"] = "source"
    source_label: str | None = None
    """Default: the source giving 3-6 div at ``vdiv`` (prefer ~3 div, e.g. a 1.5 V battery)."""
    vdiv: float = 0.5
    position_div: float | None = None
    """None: centred (ground at -d/2)."""
    instruction: str = DEFAULT_TRIMMER_TEXT

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("trimmer.enabled must be a bool")
        if self.placement not in ("first", "after_zero"):
            raise ValueError(f"trimmer.placement must be first|after_zero, not {self.placement!r}")
        if self.reference not in ("source", "open"):
            raise ValueError(f"trimmer.reference must be source|open, not {self.reference!r}")
        if self.source_label is not None:
            _check_label(self.source_label)
        object.__setattr__(self, "vdiv", float(VoltsPerDiv.parse(self.vdiv)))
        if self.position_div is not None:
            p = _finite(self.position_div, "trimmer.position_div")
            if abs(p) > abs(POS_GROUND_MIN):
                raise ValueError("trimmer.position_div must be within +-3.5 div")
            object.__setattr__(self, "position_div", p)
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("trimmer.instruction must be a non-empty string")


@dataclass(frozen=True)
class PlanOptions:
    mode: Mode = "standard"
    max_abs_v: float = DEFAULT_MAX_ABS_V
    ranges: tuple[float, ...] = ALL_RANGES
    zero: bool = True
    verify: bool = True
    trimmer: TrimmerConfig = field(default_factory=TrimmerConfig)
    uncalibrated: Uncalibrated = "keep"

    def __post_init__(self) -> None:
        if self.mode not in get_args(Mode):
            raise ValueError(f"mode must be one of {get_args(Mode)}, not {self.mode!r}")
        m = _finite(self.max_abs_v, "max_abs_v")
        if not 0 < m <= INPUT_ABS_MAX_V:
            raise ValueError(f"max_abs_v must be in (0, {INPUT_ABS_MAX_V:g}] V, got {m:g}")
        object.__setattr__(self, "max_abs_v", m)
        try:
            rs = sorted({float(VoltsPerDiv.parse(r)) for r in self.ranges})
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ranges: {exc}") from None
        if not rs:
            raise ValueError("ranges must not be empty")
        object.__setattr__(self, "ranges", tuple(rs))
        if not isinstance(self.zero, bool) or not isinstance(self.verify, bool):
            raise ValueError("zero/verify must be bools")
        if not isinstance(self.trimmer, TrimmerConfig):
            raise ValueError("trimmer must be a TrimmerConfig")
        if self.uncalibrated not in get_args(Uncalibrated):
            raise ValueError(f"uncalibrated must be one of {get_args(Uncalibrated)}")


def options_to_dict(o: PlanOptions) -> dict[str, Any]:
    t = o.trimmer
    return {
        "mode": o.mode,
        "max_abs_v": o.max_abs_v,
        "ranges": list(o.ranges),
        "zero": o.zero,
        "verify": o.verify,
        "trimmer": {
            "enabled": t.enabled,
            "placement": t.placement,
            "reference": t.reference,
            "source_label": t.source_label,
            "vdiv": t.vdiv,
            "position_div": t.position_div,
            "instruction": t.instruction,
        },
        "uncalibrated": o.uncalibrated,
    }


def options_from_dict(d: Mapping[str, Any]) -> PlanOptions:
    if not isinstance(d, Mapping):
        raise ValueError("options must be an object")
    kw: dict[str, Any] = dict(d)
    extra = set(kw) - {"mode", "max_abs_v", "ranges", "zero", "verify", "trimmer", "uncalibrated"}
    if extra:
        raise ValueError(f"unknown option key(s) {sorted(extra)}")
    if "ranges" in kw:
        kw["ranges"] = tuple(kw["ranges"])
    if "trimmer" in kw:
        t = kw["trimmer"]
        if not isinstance(t, Mapping):
            raise ValueError("trimmer must be an object")
        try:
            kw["trimmer"] = TrimmerConfig(**t)
        except TypeError as exc:
            raise ValueError(f"trimmer: {exc}") from None
    return PlanOptions(**kw)


# --------------------------------------------------------------------------------------------
# Plan (2.3)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurePoint:
    vdiv: float
    position_div: float
    expected_div: float
    """Nominal signed true deflection in divisions (0 for ground-map points)."""
    purpose: str


@dataclass(frozen=True)
class Setup:
    id: str
    kind: SetupKind
    source_label: str | None
    volts: float | None
    """Signed nominal volts (adjustable: the value to set); None for zero / open trimmer."""
    adjustable: bool
    instruction: str
    points: tuple[MeasurePoint, ...]
    accept_v: tuple[float, float] | None
    """Signed reading interval keeping every point in its accepted window (reference/verify)."""

    @property
    def needs_reading(self) -> bool:
        return self.kind in ("reference", "verify") or (
            self.kind == "trimmer" and self.volts is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_label": self.source_label,
            "volts": self.volts,
            "adjustable": self.adjustable,
            "instruction": self.instruction,
            "points": [
                {
                    "vdiv": p.vdiv,
                    "position_div": p.position_div,
                    "expected_div": p.expected_div,
                    "purpose": p.purpose,
                }
                for p in self.points
            ],
            "accept_v": list(self.accept_v) if self.accept_v is not None else None,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Setup:
        acc = d.get("accept_v")
        return cls(
            id=str(d["id"]),
            kind=d["kind"],
            source_label=d.get("source_label"),
            volts=None if d.get("volts") is None else float(d["volts"]),
            adjustable=bool(d.get("adjustable", False)),
            instruction=str(d.get("instruction", "")),
            points=tuple(
                MeasurePoint(
                    float(p["vdiv"]),
                    float(p["position_div"]),
                    float(p["expected_div"]),
                    str(p["purpose"]),
                )
                for p in d.get("points", ())
            ),
            accept_v=None if acc is None else (float(acc[0]), float(acc[1])),
        )


@dataclass(frozen=True)
class RangeCoverage:
    vdiv: float
    pos: str
    """good | fallback | missing"""
    neg: str
    """good | fallback | missing | not_reversible"""
    nonlin: str
    """good | partial | missing | not_in_mode"""
    needs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "vdiv": self.vdiv,
            "pos": self.pos,
            "neg": self.neg,
            "nonlin": self.nonlin,
            "needs": list(self.needs),
        }


@dataclass(frozen=True)
class Plan:
    setups: tuple[Setup, ...]
    coverage: tuple[RangeCoverage, ...]
    sources: tuple[Source, ...]
    options: PlanOptions
    reconnections: int
    user_actions: int
    estimated_s: float
    warnings: tuple[str, ...]

    def setup(self, setup_id: str) -> Setup:
        for s in self.setups:
            if s.id == setup_id:
                return s
        raise KeyError(setup_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setups": [s.to_dict() for s in self.setups],
            "coverage": [c.to_dict() for c in self.coverage],
            "sources": [source_to_dict(s) for s in self.sources],
            "options": options_to_dict(self.options),
            "reconnections": self.reconnections,
            "user_actions": self.user_actions,
            "estimated_s": self.estimated_s,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Plan:
        if not isinstance(d, Mapping):
            raise ValueError("plan must be an object")
        try:
            return cls(
                setups=tuple(Setup.from_dict(s) for s in d["setups"]),
                coverage=tuple(
                    RangeCoverage(
                        float(c["vdiv"]),
                        str(c["pos"]),
                        str(c["neg"]),
                        str(c["nonlin"]),
                        tuple(str(n) for n in c.get("needs", ())),
                    )
                    for c in d["coverage"]
                ),
                sources=validate_sources([source_from_dict(s) for s in d["sources"]]),
                options=options_from_dict(d["options"]),
                reconnections=int(d["reconnections"]),
                user_actions=int(d["user_actions"]),
                estimated_s=float(d["estimated_s"]),
                warnings=tuple(str(w) for w in d.get("warnings", ())),
            )
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError(f"invalid plan: {exc!r}") from None


# --------------------------------------------------------------------------------------------
# Session views and results (3)
# --------------------------------------------------------------------------------------------


class SessionState(StrEnum):
    PLANNED = "planned"
    IN_SETUP = "in_setup"
    READY_TO_FIT = "ready_to_fit"
    FITTED = "fitted"
    SAVED = "saved"
    DONE = "done"
    ABORTED = "aborted"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (SessionState.DONE, SessionState.ABORTED, SessionState.FAILED)


class SetupPhase(StrEnum):
    AWAIT_INPUT = "await_input"
    LIVE = "live"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class QualityCode(StrEnum):
    OK = "ok"
    SATURATED = "saturated"
    NOISY = "noisy"
    NO_SIGNAL = "no_signal"
    POLARITY = "polarity"
    READING_MISMATCH = "reading_mismatch"
    OUT_OF_WINDOW = "out_of_window"
    DRIFT = "drift"


PointStatus = Literal["ok", "warning", "failed"]
SetupStatus = Literal["ok", "warning", "failed", "skipped"]
Needs = Literal["input_disconnected", "reading_v", "done", "none"]


def _r4(x: float | None) -> float | None:
    """4 significant digits (JSON payloads)."""
    if x is None or not math.isfinite(x) or x == 0:
        return x
    return float(f"{x:.4g}")


@dataclass(frozen=True)
class PointResult:
    """One measured point. ``status`` ok/warning = usable for the fit (or verify), failed = not.

    Volts: ``v_ref`` is the signed reference (the multimeter reading, polarity of the setup),
    ``v_read`` the scope's reading with gain 1 and no NL (reference), or the installed
    calibration's reading (verify, ``v_scope``). ``deflection_div`` = ``v_ref / vdiv``.
    """

    vdiv: float
    position_div: float
    purpose: str
    status: PointStatus
    code: QualityCode
    message: str = ""
    hint: str = ""
    dac: int | None = None
    raw_mean: float | None = None
    std: float | None = None
    frame_spread: float | None = None
    clipped: float | None = None
    ground: float | None = None
    ground_source: Literal["map", "model"] | None = None
    v_ref: float | None = None
    v_read: float | None = None
    deflection_div: float | None = None
    err_pct: float | None = None

    @property
    def usable(self) -> bool:
        return self.status != "failed"

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "vdiv": self.vdiv,
            "position_div": self.position_div,
            "purpose": self.purpose,
            "status": self.status,
            "code": str(self.code),
            "message": self.message,
            "hint": self.hint,
            "deflection_div": _r4(self.deflection_div),
            "err_pct": _r4(self.err_pct),
        }
        if not compact:
            d.update(
                dac=self.dac,
                raw_mean=self.raw_mean,
                std=self.std,
                frame_spread=self.frame_spread,
                clipped=self.clipped,
                ground=self.ground,
                ground_source=self.ground_source,
                v_ref=self.v_ref,
                v_read=self.v_read,
            )
        return d


@dataclass(frozen=True)
class SetupResult:
    setup_id: str
    kind: SetupKind
    status: SetupStatus
    points: tuple[PointResult, ...] = ()
    messages: tuple[str, ...] = ()
    reading_v: float | None = None
    """The signed reference used (after the sign rule), None for zero/skipped."""
    extra: Mapping[str, Any] = field(default_factory=dict, hash=False)
    """Kind-specific data: zero (base_ofs, gnd_fit, ground_model_err_counts, ...), verify
    (passed), trimmer (samples)."""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "warning")

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "kind": self.kind,
            "status": self.status,
            "points": [p.to_dict(compact=compact) for p in self.points],
            "messages": list(self.messages),
            "reading_v": self.reading_v,
            "extra": json.loads(json.dumps(dict(self.extra), default=str)),
        }


@dataclass(frozen=True)
class TrimmerReading:
    v_meas: float
    v_ref: float
    dev_v: float
    dev_pct: float
    """Relative to ``v_ref`` (reference=source) or to the range's volts/div (reference=open)."""
    raw_mean: float
    noise_counts: float
    best_abs_dev_pct: float
    trend: Literal["better", "worse", "same"]
    n: int
    ground_source: Literal["map", "model"] = "model"
    """``"map"``: deflection relative to the ground measured in the zero step at this exact
    range/offset DAC; ``"model"``: the modelled ground (no zero step before the trimmer)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "v_meas": _r4(self.v_meas),
            "v_ref": _r4(self.v_ref),
            "dev_mv": _r4(self.dev_v * 1e3),
            "dev_pct": _r4(self.dev_pct),
            "raw_mean": _r4(self.raw_mean),
            "noise_counts": _r4(self.noise_counts),
            "best_abs_dev_pct": _r4(self.best_abs_dev_pct),
            "trend": self.trend,
            "n": self.n,
            "ground_source": self.ground_source,
        }


@dataclass(frozen=True)
class SetupView:
    """Cheap snapshot of the session for front ends (any thread)."""

    state: SessionState
    index: int
    """Index of the current setup in ``plan.setups`` (== total when past the last one)."""
    total: int
    setup: Setup | None
    phase: SetupPhase | None
    needs: Needs
    last_result: SetupResult | None = None
    sim_reading_v: float | None = None
    warnings: tuple[str, ...] = ()
    busy: bool = False
    trimmer_v_ref: float | None = None
    trimmer_ground_source: Literal["map", "model"] | None = None
    """Trimmer setup only: the ground its live readout uses (see ``TrimmerReading``)."""

    def to_dict(self) -> dict[str, Any]:
        s = self.setup
        return {
            "state": str(self.state),
            "index": self.index,
            "total": self.total,
            "setup": None
            if s is None
            else {
                "id": s.id,
                "kind": s.kind,
                "instruction": s.instruction,
                "source": s.source_label,
                "volts": s.volts,
                "adjustable": s.adjustable,
                "accept_v": list(s.accept_v) if s.accept_v is not None else None,
                "points": len(s.points),
                "needs": self.needs,
            },
            "phase": None if self.phase is None else str(self.phase),
            "needs": self.needs,
            "last_result": None
            if self.last_result is None
            else self.last_result.to_dict(compact=True),
            "sim_reading_v": self.sim_reading_v,
            "warnings": list(self.warnings),
            "busy": self.busy,
            "ground_source": self.trimmer_ground_source,
        }


@dataclass(frozen=True)
class SaveResult:
    saved: bool
    """True if a file was written (False: applied only)."""
    backup_path: Path | None
    calibration_id: str
    path: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "saved": self.saved,
            "backup_path": None if self.backup_path is None else str(self.backup_path),
            "calibration_id": self.calibration_id,
            "path": None if self.path is None else str(self.path),
        }


# --------------------------------------------------------------------------------------------
# Source profile persistence
# --------------------------------------------------------------------------------------------


def default_sources_path() -> Path:
    from edu09 import storage

    return storage.default_config_dir() / SOURCES_FILENAME


def load_sources(
    path: str | os.PathLike[str] | None = None,
) -> tuple[tuple[Source, ...], PlanOptions]:
    """Saved source profile and options; ``((), PlanOptions())`` if absent or unreadable
    (an unreadable file is logged and ignored)."""
    target = Path(path) if path is not None else default_sources_path()
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (), PlanOptions()
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable source profile %s: %s", target, exc)
        return (), PlanOptions()
    try:
        if not isinstance(doc, Mapping) or doc.get("version") != SOURCES_FILE_VERSION:
            raise ValueError(f"unsupported version {doc.get('version')!r}")
        sources = validate_sources([source_from_dict(s) for s in doc.get("sources", [])])
        options = options_from_dict(doc.get("options", {}))
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning("ignoring invalid source profile %s: %s", target, exc)
        return (), PlanOptions()
    return sources, options


def save_sources(
    sources: Sequence[Source],
    options: PlanOptions | None = None,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Write the source profile atomically (no secrets: labels and voltages only)."""
    from edu09 import storage

    target = Path(path) if path is not None else default_sources_path()
    doc = {
        "version": SOURCES_FILE_VERSION,
        "sources": [source_to_dict(s) for s in validate_sources(sources)],
        "options": options_to_dict(options if options is not None else PlanOptions()),
    }
    storage._atomic_write_text(target, json.dumps(doc, indent=2) + "\n")
    return target
