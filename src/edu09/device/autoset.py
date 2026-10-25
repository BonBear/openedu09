"""Auto set (task T9; architecture.md 6 ``auto_set``): pick V/div, position, timebase and trigger.

Uses only the public ``Scope`` API (``configure``/``set_settings``/``acquire``) and pure helpers
(``analysis``, ``tables``). Algorithm:

1. **Coupling DC.** Search acquisitions run free (trigger off) at :data:`VERTICAL_TIMEBASE`.
2. **Vertical**: start at 5 V/div, position 0. From the latest unclipped frame predict the smallest
   range whose Vpp is < :data:`MAX_VPP_DIV` divisions and whose window (8 div, ground moved by the
   position, which centres the signal within +/-4 div so ground stays on screen) holds the signal
   with :data:`MARGIN_DIV` to spare; verify it with an acquisition (unclipped and fitting) and
   repeat from the better reading. A failed verification excludes that range and everything below.
3. **Frequency**: sweep realtime timebases from fast to slow (200 us/div ... 20 ms/div) and take the
   first frame with >= 2 regular periods (mean crossings, 10 % Vpp hysteresis). Sweeping from fast
   to slow means the first valid frame is not aliased: the previous, 2-2.5x faster one held fewer
   than 2 periods, so this one has > 50 samples per period. Periods must be regular (coefficient
   of variation <= :data:`MAX_PERIOD_CV`) so that noise is not mistaken for a signal, and Vpp
   must be >= :data:`MIN_SIGNAL_COUNTS` counts.
4. **Timebase**: the realtime timebase that puts 4..20 periods into the 1024-sample frame, closest
   to :data:`TARGET_PERIODS` (100 us/div shares 200 us/div's sampling and is never chosen). Above
   :data:`ETS_MIN_FREQUENCY_HZ` and too fast for 200 us/div, an ETS timebase (50/20 us/div, needs
   the trigger; 10 us/div shares 20 us/div's sampling) is tried; its frequency is re-measured on
   the despiked ETS frame (``analysis.despike``, ETS only) and the timebase re-picked once.
   No usable ETS frame -> 200 us/div.
5. **Trigger** AUTO, rising, level = signal mean. A final acquisition with the chosen settings is
   returned as ``waveform``.

The running state is preserved (stopped during auto set, restarted afterwards if it was running);
on an exception the previous settings are restored.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from edu09 import analysis
from edu09.device.scope import Scope
from edu09.errors import AcquisitionTimeout, Edu09Error
from edu09.model import (
    POSITION_DIV_RANGE,
    SCREEN_DIVISIONS,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    VoltsPerDiv,
    Waveform,
)
from edu09.protocol import tables

__all__ = [
    "AutoSetResult",
    "auto_set",
    "fit_position",
    "frame_periods",
    "periodic_frequency",
    "pick_timebase",
    "whole_period_mean",
]

log = logging.getLogger(__name__)

VERTICAL_TIMEBASE: Final = Timebase.MS2
"""Timebase of the vertical search (65 ms frame: >= 3 periods of 50 Hz; min/max of faster
signals are still caught by the 1024 samples)."""
MAX_VPP_DIV: Final = 6.0
MARGIN_DIV: Final = 0.5
"""Free divisions kept above vmax and below vmin."""
POSITION_STEP_DIV: Final = 0.1
MIN_PERIODS: Final = 4.0
MAX_PERIODS: Final = 20.0
TARGET_PERIODS: Final = 8.0
ETS_MIN_FREQUENCY_HZ: Final = 10e3
MAX_PERIOD_CV: Final = 0.25
MIN_SIGNAL_COUNTS: Final = 6.0
"""Minimum Vpp (raw counts) for a periodic signal (noise on an open input is ~4 counts pp)."""
NO_SIGNAL_V: Final = 0.05
"""Below this Vpp and |mean| there is no signal at all (help: 100 mV minimum)."""
NO_SIGNAL_VDIV: Final = VoltsPerDiv.V1
REALTIME_SWEEP: Final[tuple[Timebase, ...]] = (
    Timebase.US200,
    Timebase.US500,
    Timebase.MS1,
    Timebase.MS2,
    Timebase.MS5,
    Timebase.MS10,
    Timebase.MS20,
)
"""Frequency sweep, fast to slow (lowest detectable ~5 Hz)."""
ETS_CHOICES: Final[tuple[Timebase, ...]] = (Timebase.US50, Timebase.US20)
_VDIVS_DESC: Final[tuple[VoltsPerDiv, ...]] = tuple(sorted(VoltsPerDiv, key=float, reverse=True))
MIN_ACQ_TIMEOUT_S: Final = 0.2


@dataclass(frozen=True)
class AutoSetResult:
    """Outcome of :func:`auto_set`: the applied settings, one line per decision, last frame."""

    settings: Settings
    rationale: list[str] = field(default_factory=list)
    waveform: Waveform | None = None


# --------------------------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------------------------
def frame_periods(tb: Timebase, frequency_hz: float) -> float:
    """Periods of ``frequency_hz`` in one 1024-sample frame at ``tb``."""
    entry = tables.TIMEBASES[float(tb)]
    return frequency_hz * tables.FRAME_SIZE * entry.dt


def _realtime_choices() -> list[Timebase]:
    """Realtime timebases with distinct sampling (drops 100 us/div, which equals 200 us/div)."""
    seen: set[float] = set()
    out: list[Timebase] = []
    for tb in sorted(Timebase, key=float):
        e = tables.TIMEBASES[float(tb)]
        if e.is_ets or e.sw_zoom != 1 or e.sample_rate_hz in seen:
            continue
        seen.add(e.sample_rate_hz)
        out.append(tb)
    return out


def pick_timebase(frequency_hz: float, candidates: list[Timebase]) -> tuple[Timebase, float]:
    """Candidate whose periods per frame are in [4, 20] and closest to 8 (log scale); if none is
    in range, the one closest to the range. Returns (timebase, periods)."""

    def cost(tb: Timebase) -> tuple[int, float]:
        p = frame_periods(tb, frequency_hz)
        inside = MIN_PERIODS <= p <= MAX_PERIODS
        if inside:
            return (0, abs(math.log(p / TARGET_PERIODS)))
        edge = MIN_PERIODS if p < MIN_PERIODS else MAX_PERIODS
        return (1, abs(math.log(p / edge)))

    best = min(candidates, key=cost)
    return best, frame_periods(best, frequency_hz)


def fit_position(vmin: float, vmax: float, vdiv: float) -> float | None:
    """Position (div) that centres [vmin, vmax] at ``vdiv`` within the screen, or None if the
    signal does not fit with :data:`MARGIN_DIV` or has Vpp >= :data:`MAX_VPP_DIV` div."""
    if (vmax - vmin) / vdiv >= MAX_VPP_DIV:
        return None
    lo, hi = POSITION_DIV_RANGE
    mid = 0.5 * (vmax + vmin) / vdiv
    pos = min(max(-mid, lo), hi)
    pos = round(pos / POSITION_STEP_DIV) * POSITION_STEP_DIV
    pos = min(max(pos, lo), hi)
    half = SCREEN_DIVISIONS / 2
    top = (half - pos - MARGIN_DIV) * vdiv
    bottom = (-half - pos + MARGIN_DIV) * vdiv
    if vmax > top + 1e-12 or vmin < bottom - 1e-12:
        return None
    return float(round(pos, 3)) + 0.0  # + 0.0: no "-0.0"


def _fmt_v(v: float) -> str:
    a = abs(v)
    if a >= 1 or a == 0:
        return f"{v:.3g} V"
    return f"{v * 1e3:.3g} mV"


def _fmt_hz(f: float) -> str:
    if f >= 1e3:
        return f"{f / 1e3:.4g} kHz"
    return f"{f:.4g} Hz"


@dataclass(frozen=True)
class _Reading:
    wf: Waveform
    vmin: float
    vmax: float
    mean: float

    @property
    def vpp(self) -> float:
        return self.vmax - self.vmin


def _reading(wf: Waveform) -> _Reading:
    v = wf.volts
    return _Reading(wf, float(v.min()), float(v.max()), float(v.mean()))


def periodic_frequency(wf: Waveform, *, despike: bool = False) -> float | None:
    """Frequency of a regular periodic signal in ``wf`` (None if < 2 regular periods)."""
    x = analysis.despike(wf.volts) if despike else np.asarray(wf.volts, dtype=np.float64)
    vdiv = float(wf.settings.ch1.volts_per_div)
    counts_per_v = tables.COUNTS_PER_DIV / vdiv
    if (float(x.max()) - float(x.min())) * counts_per_v < MIN_SIGNAL_COUNTS:
        return None
    edges = analysis.find_edges(x)
    if edges is None:
        return None
    best = edges.rising if edges.rising.size >= edges.falling.size else edges.falling
    if best.size < analysis.MIN_PERIODS + 1:
        return None
    d = np.diff(best)
    mean = float(d.mean())
    if not mean > 0 or float(d.std()) / mean > MAX_PERIOD_CV:
        return None
    return 1.0 / (mean * wf.dt)


def whole_period_mean(wf: Waveform) -> float:
    """Mean of ``wf.volts`` over whole periods (first to last rising/falling mean crossing), so a
    frame holding a fractional number of periods does not bias the trigger level."""
    x = np.asarray(wf.volts, dtype=np.float64)
    edges = analysis.find_edges(x)
    if edges is None:
        return float(x.mean())
    best = edges.rising if edges.rising.size >= edges.falling.size else edges.falling
    if best.size < 2:
        return float(x.mean())
    i0, i1 = round(float(best[0])), round(float(best[-1]))
    return float(x[i0:i1].mean()) if i1 > i0 else float(x.mean())


# --------------------------------------------------------------------------------------------
# auto set
# --------------------------------------------------------------------------------------------
class _Session:
    def __init__(self, scope: Scope, timeout_s: float) -> None:
        self.scope = scope
        self.deadline = time.monotonic() + timeout_s
        self.rationale: list[str] = []
        self.last_ets: Waveform | None = None

    def note(self, msg: str) -> None:
        log.info("auto_set: %s", msg)
        self.rationale.append(msg)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def acquire(self, settings: Settings, trigger_mode: TriggerMode | None = None) -> Waveform:
        self.scope.set_settings(settings, source="auto_set")
        timeout = max(self.remaining(), MIN_ACQ_TIMEOUT_S)
        return self.scope.acquire(timeout_s=timeout, trigger_mode=trigger_mode)

    def free(self, settings: Settings) -> _Reading:
        """Free-run acquisition (trigger off) with ``settings``."""
        s = settings.replace(trigger_mode=TriggerMode.NONE)
        return _reading(self.acquire(s))


def _fits(r: _Reading) -> bool:
    if r.wf.clipped:
        return False
    vdiv = float(r.wf.settings.ch1.volts_per_div)
    pos = r.wf.settings.ch1.position_div
    half = SCREEN_DIVISIONS / 2
    return r.vmax <= (half - pos) * vdiv and r.vmin >= (-half - pos) * vdiv


def _vertical(ses: _Session, base: Settings) -> tuple[Settings, _Reading]:
    s = base.replace(volts_per_div=VoltsPerDiv.V5, position_div=0.0, trigger_level_v=0.0)
    r = ses.free(s)
    if r.wf.clipped:
        ses.note(
            f"5 V/div: signal clipped (seen {_fmt_v(r.vmin)}..{_fmt_v(r.vmax)}); it exceeds "
            "the input range, keeping 5 V/div"
        )
        pos = fit_position(r.vmin, r.vmax, 5.0)
        if pos is None:
            mid = 0.5 * (r.vmin + r.vmax) / 5.0
            pos = round(min(max(-mid, POSITION_DIV_RANGE[0]), POSITION_DIV_RANGE[1]), 1) + 0.0
        if pos != 0.0:
            s = s.replace(position_div=pos)
            r = ses.free(s)
        return s, r
    ses.note(f"5 V/div: {_fmt_v(r.vmin)}..{_fmt_v(r.vmax)} (Vpp {_fmt_v(r.vpp)}), not clipped")
    floor = len(_VDIVS_DESC)  # index of the largest range known NOT to fit (exclusive bound)
    cur = 0
    while not ses.expired():
        cand = cur
        cand_pos = s.ch1.position_div
        for i in range(cur + 1, floor):
            pos = fit_position(r.vmin, r.vmax, float(_VDIVS_DESC[i]))
            if pos is not None:
                cand, cand_pos = i, pos
        if cand == cur:
            break
        vd = _VDIVS_DESC[cand]
        s2 = s.replace(volts_per_div=vd, position_div=cand_pos, trigger_level_v=0.0)
        r2 = ses.free(s2)
        if _fits(r2):
            ses.note(
                f"{vd.label}/div, position {cand_pos:+g} div: {_fmt_v(r2.vmin)}..{_fmt_v(r2.vmax)}"
                f" (Vpp {r2.vpp / float(vd):.2f} div < {MAX_VPP_DIV:g}), not clipped"
            )
            s, r, cur = s2, r2, cand
        else:
            ses.note(f"{vd.label}/div: clipped or off screen, rejected")
            floor = cand
    # re-centre with the most precise reading (it may move a little)
    pos = fit_position(r.vmin, r.vmax, float(s.ch1.volts_per_div))
    if pos is not None and abs(pos - s.ch1.position_div) >= 0.25:
        s2 = s.replace(position_div=pos, trigger_level_v=0.0)
        r2 = ses.free(s2)
        if _fits(r2):
            s, r = s2, r2
    ses.note(
        f"volts/div {s.ch1.volts_per_div.label}, position {s.ch1.position_div:+g} div "
        f"(smallest range with Vpp < {MAX_VPP_DIV:g} div and no clipping)"
    )
    return s, r


def _frequency_sweep(ses: _Session, s: Settings) -> tuple[float | None, _Reading | None]:
    last: _Reading | None = None
    for tb in REALTIME_SWEEP:
        if ses.expired():
            ses.note("time budget exhausted during the frequency sweep")
            break
        r = ses.free(s.replace(timebase=tb))
        last = r
        f = periodic_frequency(r.wf)
        if f is not None:
            ses.note(
                f"frequency {_fmt_hz(f)} measured at {tb.label}/div "
                f"({frame_periods(tb, f):.1f} periods per frame)"
            )
            return f, r
    return None, last


def _try_ets(
    ses: _Session, s: Settings, f: float, level: float
) -> tuple[Settings, Waveform] | None:
    tb, periods = pick_timebase(f, list(ETS_CHOICES))
    for attempt in range(2):
        cand = s.replace(timebase=tb, trigger_mode=TriggerMode.AUTO, trigger_level_v=level)
        try:
            wf = ses.acquire(cand)
        except AcquisitionTimeout:
            ses.note(f"ETS {tb.label}/div: no triggered frame")
            return None
        if not wf.triggered:
            ses.note(f"ETS {tb.label}/div: no trigger")
            return None
        f2 = periodic_frequency(wf, despike=True)
        if f2 is None:
            ses.note(f"ETS {tb.label}/div: no regular periods in the frame")
            return None
        if attempt == 0 and abs(f2 - f) > 0.1 * f:
            ses.note(f"ETS frame measures {_fmt_hz(f2)} (realtime estimate {_fmt_hz(f)})")
            f = f2
            tb2, periods = pick_timebase(f, list(ETS_CHOICES))
            if tb2 is not tb:
                tb = tb2
                continue
        periods = frame_periods(tb, f2)
        ses.note(
            f"timebase {tb.label}/div (equivalent-time sampling, f > "
            f"{_fmt_hz(ETS_MIN_FREQUENCY_HZ)}): {periods:.1f} periods per frame"
        )
        return cand, wf
    return None


def auto_set(scope: Scope, timeout_s: float = 5.0) -> AutoSetResult:
    """Choose and apply settings for the connected signal (architecture.md 6 ``auto_set``).

    Leaves the scope stopped if it was stopped, running if it was running. On an error the
    previous settings are restored and the error propagates (``AcquisitionTimeout``,
    ``DeviceDisconnected``, ...). ``timeout_s`` bounds the search; when it runs out the best
    settings found so far are used.
    """
    if not (isinstance(timeout_s, int | float) and timeout_s > 0):
        raise ValueError(f"timeout_s must be > 0, got {timeout_s!r}")
    original = scope.settings
    was_running = scope.running
    ses = _Session(scope, float(timeout_s))
    if was_running:
        scope.stop()
    ok = False
    try:
        result = _auto_set(ses, original)
        ok = True
        return result
    finally:
        if not ok:
            try:
                scope.set_settings(original, source="auto_set")
            except (Edu09Error, TimeoutError):  # TimeoutError: worker did not answer
                log.warning("auto_set: could not restore the previous settings", exc_info=True)
        if was_running:
            try:
                scope.run()
            except (Edu09Error, TimeoutError):
                log.warning("auto_set: could not restart continuous acquisition", exc_info=True)


def _auto_set(ses: _Session, original: Settings) -> AutoSetResult:
    base = original.replace(
        coupling=Coupling.DC,
        timebase=VERTICAL_TIMEBASE,
        trigger_mode=TriggerMode.NONE,
        trigger_edge=TriggerEdge.RISING,
        trigger_level_v=0.0,
    )
    ses.note("coupling DC, trigger AUTO (rising edge)")
    s, r = _vertical(ses, base)

    f, fr = _frequency_sweep(ses, s)
    mean = r.mean
    if f is not None and fr is not None and not fr.wf.clipped:
        r = fr
        mean = whole_period_mean(fr.wf)
    final: Settings
    wf: Waveform | None = None
    if f is None:
        timebase = original.timebase if not original.timebase.is_ets else Timebase.MS2
        if r.vpp < NO_SIGNAL_V and abs(r.mean) < NO_SIGNAL_V:
            ses.note(
                f"no signal detected (Vpp {_fmt_v(r.vpp)}, mean {_fmt_v(r.mean)}): "
                f"{NO_SIGNAL_VDIV.label}/div, position 0, timebase {timebase.label}/div"
            )
            s = s.replace(volts_per_div=NO_SIGNAL_VDIV, position_div=0.0, trigger_level_v=0.0)
        else:
            ses.note(
                f"no periodic signal (DC level {_fmt_v(r.mean)}, Vpp {_fmt_v(r.vpp)}): "
                f"timebase {timebase.label}/div"
            )
        final = s.replace(timebase=timebase)
    else:
        final = _horizontal(ses, s, f, _trigger_level(s, mean))
        if final.timebase.is_ets:
            wf = ses.last_ets
    level = _trigger_level(final, mean)
    final = final.replace(
        trigger_mode=TriggerMode.AUTO, trigger_edge=TriggerEdge.RISING, trigger_level_v=level
    )
    ses.note(f"trigger AUTO, rising, level {_fmt_v(level)} (signal mean)")
    try:
        wf = ses.acquire(final)
    except AcquisitionTimeout:
        if final.timebase.is_ets:
            ses.note("final ETS acquisition got no trigger")
        else:
            raise
    if wf is not None and wf.clipped:
        ses.note("warning: final frame is clipped")
    return AutoSetResult(final, ses.rationale, wf)


def _trigger_level(s: Settings, mean: float) -> float:
    lo, hi = s.level_range_v()
    lvl = min(max(mean, lo), hi)
    return round(lvl, 4) + 0.0


def _horizontal(ses: _Session, s: Settings, f: float, level: float) -> Settings:
    rt = _realtime_choices()
    tb, periods = pick_timebase(f, rt)
    if periods > MAX_PERIODS and f > ETS_MIN_FREQUENCY_HZ:
        ets = _try_ets(ses, s, f, level)
        if ets is not None:
            ses.last_ets = ets[1]
            return ets[0]
        ses.note(f"falling back to realtime {tb.label}/div")
    if periods > MAX_PERIODS:
        ses.note(
            f"timebase {tb.label}/div (fastest realtime): {periods:.1f} periods per frame "
            f"(more than {MAX_PERIODS:g}; ETS only above {_fmt_hz(ETS_MIN_FREQUENCY_HZ)})"
        )
    elif periods < MIN_PERIODS:
        ses.note(f"timebase {tb.label}/div: only {periods:.1f} periods per frame (slowest)")
    else:
        ses.note(
            f"timebase {tb.label}/div: {periods:.1f} periods per frame "
            f"(target {MIN_PERIODS:g}-{MAX_PERIODS:g})"
        )
    return s.replace(timebase=tb)
