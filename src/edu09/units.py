"""Unit conversions: calibration, counts <-> volts, offset DAC, trigger byte, time axis.

``settings_to_hwconfig`` is the ONLY path from user ``Settings`` to configuration bytes.

Count domains (protocol.md 5.2/5.3):

* **raw**: the unsigned 8-bit sample as it arrives in the frame (0..255).
* **ad**: ``255 - raw``. The original calibration measures ``AD = 255 - mean(raw)`` and fits the
  ground level in this domain: ``gnd_ad = k * dac + b`` where ``dac = base_ofs + ypos`` (the
  range correction is *not* part of the fit, as in ``y1_viiva``). ``GndFit`` stores (k, b).

Volts are ``(raw - gnd_raw) * volts_per_div / counts_per_div``, negated when the ADC is inverted
(``Calibration.adc_inverted``, default ``tables.ADC_INVERTED``; open question Q3).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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

__all__ = [
    "Calibration",
    "CalibrationSource",
    "GndFit",
    "counts_to_volts",
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
    "volts_to_counts",
    "volts_to_raw",
]

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

    @classmethod
    def typical(cls) -> Calibration:
        """Measured typical values of a real EDU09 (``source="typical"``, protocol.md 11).

        Fallback for hardware without a calibration file: positions scale correctly (slope
        ~1.03 instead of the default model's 2) and 0 V is within a few counts. A per-unit
        calibration (``Scope.calibrate``) is still more accurate.
        """
        return cls(
            base_ofs=tables.TYPICAL_BASE_OFS,
            range_corr=dict(tables.TYPICAL_RANGE_CORR),
            gnd_fit=GndFit(tables.TYPICAL_GND_K, tables.TYPICAL_GND_B),
            source="typical",
        )

    def range_correction(self, vdiv: VoltsPerDiv | float) -> int:
        return self.range_corr.get(float(vdiv), 0)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (``range_corr`` keys become strings such as ``"0.1"``)."""
        return {
            "base_ofs": self.base_ofs,
            "range_corr": {f"{k:g}": v for k, v in sorted(self.range_corr.items())},
            "gnd_fit": {"k": self.gnd_fit.k, "b": self.gnd_fit.b},
            "counts_per_div": self.counts_per_div,
            "adc_inverted": self.adc_inverted,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Calibration:
        """Inverse of :meth:`to_dict`. Missing keys take defaults; bad data -> CalibrationError."""
        if not isinstance(d, Mapping):
            raise CalibrationError(f"calibration must be a mapping, got {type(d).__name__}")
        allowed = {"base_ofs", "range_corr", "gnd_fit", "counts_per_div", "adc_inverted", "source"}
        unknown = sorted(set(d) - allowed)
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
        """Stable id ``"<source>:<10 hex>"`` derived from the calibration values."""
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return f"{self.source}:{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


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
    raw shift = +position * counts_per_div (negated for an inverted ADC), and
    ``d gnd_raw / d dac = -k``.
    """
    raw_shift = ch.position_div * cal.counts_per_div * (-1.0 if cal.adc_inverted else 1.0)
    steps = round(-raw_shift / cal.gnd_fit.k)
    return int(min(max(steps, -128), 127))


def offset_dac(ch: ChannelSettings, cal: Calibration) -> int:
    """CH1OFS byte: ``base_ofs + range_corr + ypos`` clamped to 0..255 (protocol.md 5.3)."""
    return _clamp_byte(cal.base_ofs + cal.range_correction(ch.volts_per_div) + _ypos_steps(ch, cal))


def gnd_counts(ch: ChannelSettings, cal: Calibration) -> float:
    """Ground (0 V) level in the raw sample domain (float, not rounded)."""
    dac = _clamp_byte(cal.base_ofs + _ypos_steps(ch, cal))
    return _ADC_FULL - (cal.gnd_fit.k * dac + cal.gnd_fit.b)


def _volts_per_count(ch: ChannelSettings, cal: Calibration) -> float:
    vpc = float(ch.volts_per_div) / cal.counts_per_div
    return -vpc if cal.adc_inverted else vpc


def counts_to_volts(raw: npt.ArrayLike, ch: ChannelSettings, cal: Calibration) -> FloatArray:
    """Raw samples -> volts. GND coupling yields exact zeros (the hardware runs DC)."""
    arr = np.asarray(raw, dtype=np.float64)
    if ch.coupling is Coupling.GND:
        return np.zeros_like(arr)
    return (arr - gnd_counts(ch, cal)) * _volts_per_count(ch, cal)


def volts_to_counts(volts: npt.ArrayLike, ch: ChannelSettings, cal: Calibration) -> FloatArray:
    """Volts -> raw counts as float64, unrounded and unclipped (exact inverse of the DC path)."""
    v = np.asarray(volts, dtype=np.float64)
    return v / _volts_per_count(ch, cal) + gnd_counts(ch, cal)


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
