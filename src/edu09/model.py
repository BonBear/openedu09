"""Pure data model: enums, settings dataclasses and the Waveform container.

This module knows nothing about protocol bytes (see ``edu09.protocol.tables`` and ``edu09.units``).
All validation errors are ``ValueError`` with a message that lists the allowed values.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Self

import numpy as np
import numpy.typing as npt

__all__ = [
    "POSITION_DIV_RANGE",
    "SCREEN_DIVISIONS",
    "SETTINGS_KEYS",
    "ChannelSettings",
    "Coupling",
    "Settings",
    "Timebase",
    "TriggerEdge",
    "TriggerMode",
    "TriggerSettings",
    "VoltsPerDiv",
    "Waveform",
]

#: Vertical screen size in divisions (the 8-bit ADC spans exactly this many divisions).
SCREEN_DIVISIONS = 8
#: Allowed ``position_div`` range (ground marker position relative to screen centre).
POSITION_DIV_RANGE: tuple[float, float] = (-4.0, 4.0)
#: Relative tolerance used when snapping a float to an enum member.
SNAP_TOLERANCE = 0.01
_LEVEL_EPS = 1e-9

_NUM_UNIT_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Zµμ/]*)\s*$")


def _snap[E: Enum](cls: type[E], value: float, what: str, allowed: str) -> E:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid {what} {value!r}; allowed: {allowed}")
    for member in cls:
        target = float(member.value)
        if abs(value - target) <= SNAP_TOLERANCE * target:
            return member
    raise ValueError(f"invalid {what} {value!r}; allowed: {allowed}")


def _fmt_num(x: float) -> str:
    return f"{x:g}"


def _parse_str_enum[S: StrEnum](
    cls: type[S], value: object, what: str, aliases: Mapping[str, S]
) -> S:
    if isinstance(value, cls):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        for member in cls:
            if key in (member.value, member.name.lower()):
                return member
        if key in aliases:
            return aliases[key]
    allowed = ", ".join(repr(m.value) for m in cls)
    raise ValueError(f"invalid {what} {value!r}; allowed: {allowed}")


# --------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------


class VoltsPerDiv(float, Enum):
    """Vertical sensitivity of CH1 (x1 probe), volts per division."""

    V0_1 = 0.1
    V0_2 = 0.2
    V0_5 = 0.5
    V1 = 1.0
    V2 = 2.0
    V5 = 5.0

    @property
    def full_scale_v(self) -> float:
        """Full ADC span in volts (8 divisions)."""
        return float(self.value) * SCREEN_DIVISIONS

    @property
    def label(self) -> str:
        v = float(self.value)
        return f"{_fmt_num(v * 1e3)}mV" if v < 1 else f"{_fmt_num(v)}V"

    @classmethod
    def allowed_text(cls) -> str:
        return ", ".join(_fmt_num(float(m.value)) for m in cls) + " V/div"

    @classmethod
    def parse(cls, value: object) -> VoltsPerDiv:
        """Accept a member, a name (``"V0_5"``), a number, or ``"0.5"``/``"500mV"``/``"2 V"``.

        Numbers are snapped to a member when within 1 %; otherwise ``ValueError``.
        """
        if isinstance(value, cls):
            return value
        allowed = cls.allowed_text()
        if isinstance(value, bool):
            raise ValueError(f"invalid volts_per_div {value!r}; allowed: {allowed}")
        if isinstance(value, int | float):
            return _snap(cls, float(value), "volts_per_div", allowed)
        if isinstance(value, str):
            s = value.strip()
            if s.upper() in cls.__members__:
                return cls.__members__[s.upper()]
            m = _NUM_UNIT_RE.match(s)
            if m:
                num = float(m.group(1))
                unit = m.group(2).lower().removesuffix("/div")
                scale = {"": 1.0, "v": 1.0, "mv": 1e-3}.get(unit)
                if scale is not None:
                    return _snap(cls, num * scale, "volts_per_div", allowed)
        raise ValueError(f"invalid volts_per_div {value!r}; allowed: {allowed}")


class Timebase(float, Enum):
    """Horizontal timebase in seconds per division."""

    MS500 = 500e-3
    MS200 = 200e-3
    MS100 = 100e-3
    MS50 = 50e-3
    MS20 = 20e-3
    MS10 = 10e-3
    MS5 = 5e-3
    MS2 = 2e-3
    MS1 = 1e-3
    US500 = 500e-6
    US200 = 200e-6
    US100 = 100e-6
    US50 = 50e-6
    US20 = 20e-6
    US10 = 10e-6

    @property
    def seconds(self) -> float:
        return float(self.value)

    @property
    def is_ets(self) -> bool:
        """Equivalent-time sampling (50/20/10 us/div): needs a trigger, no pre-trigger."""
        return self in _ETS_TIMEBASES

    @property
    def label(self) -> str:
        """Human label such as ``"2ms"`` or ``"50us"`` (accepted by :meth:`parse`)."""
        v = float(self.value)
        return f"{_fmt_num(v * 1e3)}ms" if v >= 1e-3 else f"{_fmt_num(v * 1e6)}us"

    @classmethod
    def allowed_text(cls) -> str:
        return ", ".join(m.label for m in cls)

    @classmethod
    def parse(cls, value: object) -> Timebase:
        """Accept a member, a name (``"MS2"``), seconds (``0.002``) or ``"2ms"``/``"500 us"``.

        Numbers are snapped to a member when within 1 %; otherwise ``ValueError``.
        """
        if isinstance(value, cls):
            return value
        allowed = cls.allowed_text() + " per div"
        if isinstance(value, bool):
            raise ValueError(f"invalid timebase {value!r}; allowed: {allowed}")
        if isinstance(value, int | float):
            return _snap(cls, float(value), "timebase", allowed)
        if isinstance(value, str):
            s = value.strip()
            if s.upper() in cls.__members__:
                return cls.__members__[s.upper()]
            m = _NUM_UNIT_RE.match(s)
            if m:
                num = float(m.group(1))
                unit = m.group(2).lower().replace("μ", "u").replace("µ", "u").removesuffix("/div")
                scale = {"": 1.0, "s": 1.0, "ms": 1e-3, "us": 1e-6}.get(unit)
                if scale is not None:
                    return _snap(cls, num * scale, "timebase", allowed)
        raise ValueError(f"invalid timebase {value!r}; allowed: {allowed}")


_ETS_TIMEBASES = frozenset({Timebase.US50, Timebase.US20, Timebase.US10})


class Coupling(StrEnum):
    AC = "ac"
    DC = "dc"
    GND = "gnd"

    @classmethod
    def parse(cls, value: object) -> Coupling:
        return _parse_str_enum(cls, value, "coupling", {"ground": cls.GND})


class TriggerEdge(StrEnum):
    RISING = "rising"
    FALLING = "falling"

    @classmethod
    def parse(cls, value: object) -> TriggerEdge:
        aliases = {
            "up": cls.RISING,
            "rise": cls.RISING,
            "pos": cls.RISING,
            "positive": cls.RISING,
            "down": cls.FALLING,
            "dn": cls.FALLING,
            "fall": cls.FALLING,
            "neg": cls.FALLING,
            "negative": cls.FALLING,
        }
        return _parse_str_enum(cls, value, "trigger_edge", aliases)


class TriggerMode(StrEnum):
    """AUTO: trigger, fall back to free run after a timeout. NORMAL: wait. NONE: hw trigger off."""

    AUTO = "auto"
    NORMAL = "normal"
    NONE = "none"

    @classmethod
    def parse(cls, value: object) -> TriggerMode:
        aliases = {"off": cls.NONE, "free": cls.NONE, "free_run": cls.NONE, "norm": cls.NORMAL}
        return _parse_str_enum(cls, value, "trigger_mode", aliases)


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------


def _finite_float(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"invalid {what} {value!r}; expected a number")
    try:
        f = float(value)
    except ValueError:
        raise ValueError(f"invalid {what} {value!r}; expected a number") from None
    if not math.isfinite(f):
        raise ValueError(f"invalid {what} {value!r}; expected a finite number")
    return f


@dataclass(frozen=True)
class ChannelSettings:
    """CH1 front-end settings. ``position_div`` moves the ground level, in divisions [-4, 4]."""

    volts_per_div: VoltsPerDiv = VoltsPerDiv.V2
    coupling: Coupling = Coupling.DC
    position_div: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "volts_per_div", VoltsPerDiv.parse(self.volts_per_div))
        object.__setattr__(self, "coupling", Coupling.parse(self.coupling))
        pos = _finite_float(self.position_div, "position_div")
        lo, hi = POSITION_DIV_RANGE
        if not lo <= pos <= hi:
            raise ValueError(f"position_div {pos!r} out of range [{lo:g}, {hi:g}]")
        object.__setattr__(self, "position_div", pos)

    def level_range_v(self) -> tuple[float, float]:
        """Voltage window visible on screen / reachable by the 8-bit trigger comparator."""
        half = SCREEN_DIVISIONS / 2
        vdiv = float(self.volts_per_div)
        return ((-half - self.position_div) * vdiv, (half - self.position_div) * vdiv)

    def to_dict(self) -> dict[str, Any]:
        return {
            "volts_per_div": float(self.volts_per_div),
            "coupling": self.coupling.value,
            "position_div": self.position_div,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ChannelSettings:
        _check_keys(d, ("volts_per_div", "coupling", "position_div"), "ch1")
        return cls(**dict(d))


@dataclass(frozen=True)
class TriggerSettings:
    mode: TriggerMode = TriggerMode.AUTO
    edge: TriggerEdge = TriggerEdge.RISING
    level_v: float = 0.0
    auto_timeout_s: float = 0.1

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", TriggerMode.parse(self.mode))
        object.__setattr__(self, "edge", TriggerEdge.parse(self.edge))
        object.__setattr__(self, "level_v", _finite_float(self.level_v, "trigger level_v"))
        t = _finite_float(self.auto_timeout_s, "auto_timeout_s")
        if not 0.0 < t <= 60.0:
            raise ValueError(f"auto_timeout_s {t!r} out of range (0, 60]")
        object.__setattr__(self, "auto_timeout_s", t)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "edge": self.edge.value,
            "level_v": self.level_v,
            "auto_timeout_s": self.auto_timeout_s,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TriggerSettings:
        _check_keys(d, ("mode", "edge", "level_v", "auto_timeout_s"), "trigger")
        return cls(**dict(d))


def _check_keys(d: Mapping[str, Any], allowed: tuple[str, ...], what: str) -> None:
    if not isinstance(d, Mapping):
        raise ValueError(f"{what} must be a mapping, got {type(d).__name__}")
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {what} key(s) {unknown}; allowed: {', '.join(allowed)}")


#: Flat keys accepted by :meth:`Settings.replace` (and the MCP ``configure`` tool).
SETTINGS_KEYS: tuple[str, ...] = (
    "volts_per_div",
    "coupling",
    "position_div",
    "timebase",
    "trigger_mode",
    "trigger_edge",
    "trigger_level_v",
    "trigger_auto_timeout_s",
)


@dataclass(frozen=True)
class Settings:
    """Complete user-facing scope settings (CH1 only; the EDU09 is single-channel).

    Invariants (raise ``ValueError``): ETS timebases need ``trigger.mode != NONE``; the trigger
    level must lie inside the on-screen voltage window of ``ch1``.
    """

    ch1: ChannelSettings = field(default_factory=ChannelSettings)
    timebase: Timebase = Timebase.MS2
    trigger: TriggerSettings = field(default_factory=TriggerSettings)

    def __post_init__(self) -> None:
        if not isinstance(self.ch1, ChannelSettings):
            raise ValueError(f"ch1 must be ChannelSettings, got {type(self.ch1).__name__}")
        if not isinstance(self.trigger, TriggerSettings):
            raise ValueError(f"trigger must be TriggerSettings, got {type(self.trigger).__name__}")
        tb = Timebase.parse(self.timebase)
        object.__setattr__(self, "timebase", tb)
        if tb.is_ets and self.trigger.mode is TriggerMode.NONE:
            raise ValueError(
                f"timebase {tb.label} uses equivalent-time sampling and needs a trigger; "
                "allowed trigger modes: 'auto', 'normal'"
            )
        lo, hi = self.ch1.level_range_v()
        lvl = self.trigger.level_v
        if not lo - _LEVEL_EPS <= lvl <= hi + _LEVEL_EPS:
            raise ValueError(
                f"trigger level_v {lvl:g} V out of range [{lo:g}, {hi:g}] V for "
                f"{self.ch1.volts_per_div.label}/div at position {self.ch1.position_div:g} div"
            )

    def level_range_v(self) -> tuple[float, float]:
        return self.ch1.level_range_v()

    # -- partial updates -------------------------------------------------------------------

    def replace(self, **partial: Any) -> Settings:
        """Return a copy with flat keys (see ``SETTINGS_KEYS``) updated. Warnings are dropped."""
        return self.replace_with_warnings(**partial)[0]

    def replace_with_warnings(self, **partial: Any) -> tuple[Settings, list[str]]:
        """Atomic partial update with flat keys; ``None`` values mean "unchanged".

        Explicitly invalid values raise ``ValueError``. Implicit conflicts are coerced with a
        warning: ``trigger_mode='none'`` on an ETS timebase becomes ``'auto'``, and a trigger level
        that a V/div or position change pushed off screen is clamped (unless the level itself was
        given, in which case it raises).
        """
        unknown = sorted(set(partial) - set(SETTINGS_KEYS))
        if unknown:
            raise ValueError(f"unknown setting(s) {unknown}; allowed: {', '.join(SETTINGS_KEYS)}")
        p = {k: v for k, v in partial.items() if v is not None}
        warnings: list[str] = []

        ch1 = ChannelSettings(
            volts_per_div=p.get("volts_per_div", self.ch1.volts_per_div),
            coupling=p.get("coupling", self.ch1.coupling),
            position_div=p.get("position_div", self.ch1.position_div),
        )
        tb = Timebase.parse(p.get("timebase", self.timebase))
        mode = TriggerMode.parse(p.get("trigger_mode", self.trigger.mode))
        if tb.is_ets and mode is TriggerMode.NONE:
            mode = TriggerMode.AUTO
            warnings.append(
                f"timebase {tb.label} uses equivalent-time sampling and needs a trigger; "
                "trigger_mode set to 'auto'"
            )
        level = _finite_float(p.get("trigger_level_v", self.trigger.level_v), "trigger_level_v")
        lo, hi = ch1.level_range_v()
        if not lo - _LEVEL_EPS <= level <= hi + _LEVEL_EPS:
            if "trigger_level_v" in p:
                raise ValueError(
                    f"trigger_level_v {level:g} V out of range [{lo:g}, {hi:g}] V for "
                    f"{ch1.volts_per_div.label}/div at position {ch1.position_div:g} div"
                )
            clamped = min(max(level, lo), hi)
            warnings.append(f"trigger_level_v clamped from {level:g} V to {clamped:g} V")
            level = clamped
        trigger = TriggerSettings(
            mode=mode,
            edge=p.get("trigger_edge", self.trigger.edge),
            level_v=level,
            auto_timeout_s=p.get("trigger_auto_timeout_s", self.trigger.auto_timeout_s),
        )
        return Settings(ch1=ch1, timebase=tb, trigger=trigger), warnings

    # -- serialisation ---------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly nested dict; ``timebase`` is seconds/div as a float."""
        return {
            "ch1": self.ch1.to_dict(),
            "timebase": float(self.timebase),
            "trigger": self.trigger.to_dict(),
        }

    def to_flat_dict(self) -> dict[str, Any]:
        """Flat form using the ``SETTINGS_KEYS`` names (round-trips through :meth:`replace`)."""
        return {
            "volts_per_div": float(self.ch1.volts_per_div),
            "coupling": self.ch1.coupling.value,
            "position_div": self.ch1.position_div,
            "timebase": float(self.timebase),
            "trigger_mode": self.trigger.mode.value,
            "trigger_edge": self.trigger.edge.value,
            "trigger_level_v": self.trigger.level_v,
            "trigger_auto_timeout_s": self.trigger.auto_timeout_s,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Self:
        """Inverse of :meth:`to_dict`. Missing keys take defaults; unknown keys raise."""
        _check_keys(d, ("ch1", "timebase", "trigger"), "settings")
        kwargs: dict[str, Any] = {}
        if "ch1" in d:
            kwargs["ch1"] = ChannelSettings.from_dict(d["ch1"])
        if "timebase" in d:
            kwargs["timebase"] = Timebase.parse(d["timebase"])
        if "trigger" in d:
            kwargs["trigger"] = TriggerSettings.from_dict(d["trigger"])
        return cls(**kwargs)


# --------------------------------------------------------------------------------------------
# Waveform
# --------------------------------------------------------------------------------------------


def _readonly(a: npt.ArrayLike, dtype: type[np.generic], what: str) -> npt.NDArray[Any]:
    arr = np.array(a, dtype=dtype, copy=True)
    if arr.ndim != 1:
        raise ValueError(f"{what} must be 1-D, got shape {arr.shape}")
    arr.flags.writeable = False
    return arr


@dataclass(frozen=True, eq=False)
class Waveform:
    """One decoded acquisition (1024 de-interleaved samples).

    ``raw`` and ``volts`` are private read-only copies. ``clipped`` is derived from ``raw``
    (any sample at 0 or 255). A naive ``timestamp`` is taken to be UTC.
    """

    seq: int
    timestamp: datetime
    raw: npt.NDArray[np.uint8]
    volts: npt.NDArray[np.float64]
    dt: float
    trigger_index: int | None
    settings: Settings
    calibration_id: str
    acq_mode: int = 1
    triggered: bool = True
    clipped: bool = field(init=False)

    def __post_init__(self) -> None:
        raw = _readonly(self.raw, np.uint8, "raw")
        volts = _readonly(self.volts, np.float64, "volts")
        if raw.shape != volts.shape:
            raise ValueError(f"raw {raw.shape} and volts {volts.shape} lengths differ")
        if not (math.isfinite(self.dt) and self.dt > 0):
            raise ValueError(f"dt must be a positive finite number, got {self.dt!r}")
        if self.trigger_index is not None and not 0 <= self.trigger_index < max(raw.size, 1):
            raise ValueError(f"trigger_index {self.trigger_index} outside 0..{raw.size - 1}")
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "volts", volts)
        object.__setattr__(self, "timestamp", ts)
        object.__setattr__(self, "clipped", bool(raw.size and (raw.min() == 0 or raw.max() == 255)))

    @property
    def waveform_id(self) -> str:
        return f"w{self.seq}"

    @property
    def n(self) -> int:
        return int(self.raw.size)

    @property
    def sample_rate_hz(self) -> float:
        return 1.0 / self.dt

    @property
    def duration_s(self) -> float:
        return self.n * self.dt

    @property
    def t0(self) -> float:
        """Time of sample 0 relative to the trigger (0.0 when the trigger index is unknown)."""
        return -(self.trigger_index or 0) * self.dt

    def t(self) -> npt.NDArray[np.float64]:
        """Time axis in seconds relative to the trigger: ``(arange(n) - trigger_index) * dt``."""
        return (np.arange(self.n, dtype=np.float64) - (self.trigger_index or 0)) * self.dt
