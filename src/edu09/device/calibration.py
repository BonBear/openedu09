"""Offset/ground calibration (task T9; protocol.md 5.3/5.4 adapted to firmware 1.01).

The procedure runs on the worker thread through :meth:`Scope.run_exclusive`, so acquisition is
paused while it measures and the user's configuration is re-sent afterwards (``DeviceIO`` does
that after every live capture). The input must be disconnected (open).

Levels come from free-run frames (``DeviceIO.live_samples``), never from ``0x0D`` (H2: firmware
1.01 answers ``0x0D`` with status reports). Every level is the mean of :data:`CAL_WINDOW` samples
from each of :data:`CAL_FRAMES` frames at :data:`CAL_TIMEBASE` (5 ms/div: 625 samples = exactly
100 ms = 5 periods of 50 Hz and 6 of 60 Hz, so mains hum averages out).

Steps (count domains as in ``units``: raw = sample, ad = 255 - raw):

1. **Base offset**, 2 V/div, DC, trigger off, position 0: find the offset DAC byte whose ground
   reads ad ~128 (raw ~127). The DAC slope (raw counts per DAC step) and its sign are measured
   rather than assumed; a Newton step is followed by +/-1 steps to the closest byte. The result
   must satisfy ``CAL_BASE_OFS_MIN < base_ofs < CAL_BASE_OFS_MAX`` (0x5A..0xA6, exclusive), else
   ``CalibrationError("CALIBRATION FAILED ...")`` (a DC voltage on the input or a broken front end).
   The 2 V/div levels must also be quiet (std <= :data:`MAX_NOISE_COUNTS`), else a signal is
   connected. Only the very first reading of a run gets one retry (after an extra
   :data:`RETRY_SETTLE_S` settle) when it is too noisy: on the hardware it once read 6 counts rms
   (normal ~0.7) right after ``Scope.open``, the front end still settling from its previous DAC
   state (every capture also waits ``worker.OFFSET_SETTLE_S`` after an offset change).
2. **Ground fit**: ad at three DAC bytes ``base_ofs + CAL_FIT_OFFSETS`` -> ``GndFit.from_points``.
3. **Range corrections** for ``tables.RANGE_CORR_VDIVS`` (1 V and 0.1 V/div): the DAC
   correction that puts the ground at the fitted 2 V/div ground level (``units.gnd_counts``), at
   most :data:`RANGE_CORR_LIMIT` steps (a larger one means a DC voltage on the input).

``measure_calibration`` returns ``Calibration(source="measured")``; ``Scope.calibrate`` installs
and saves it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

import numpy as np

from edu09 import units
from edu09.device.scope import Scope
from edu09.device.worker import DeviceIO
from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, Coupling, Settings, Timebase, TriggerMode, VoltsPerDiv
from edu09.protocol import tables

__all__ = [
    "CAL_FIT_OFFSETS",
    "CAL_FRAMES",
    "CAL_TIMEBASE",
    "CAL_WINDOW",
    "LevelReading",
    "measure_calibration",
]

log = logging.getLogger(__name__)

CAL_VDIV: Final = VoltsPerDiv.V2
"""Range of the base offset search and the ground fit (as in the original)."""
CAL_TIMEBASE: Final = Timebase.MS5
"""6250 S/s: a 1024-sample frame is 164 ms; the first 625 samples are exactly 100 ms."""
CAL_WINDOW: Final = 625
"""Samples per frame averaged (100 ms at 5 ms/div: an integer number of 50 and 60 Hz periods)."""
CAL_FRAMES: Final = 2
"""Free-run frames per level reading."""
TARGET_AD: Final = 128.0
"""Ground target of the base offset search in the ad domain (raw 127; protocol.md 5.4)."""
CAL_FIT_OFFSETS: Final = (-20, 0, 20)
"""DAC offsets from ``base_ofs`` of the three ground-fit points (original: CH1_DAC0/1/2)."""
SLOPE_PROBE_STEPS: Final = 8
"""DAC distance of the second reading that measures the slope before the Newton step."""
MIN_SLOPE: Final = 0.25
"""Minimum |raw counts per DAC step|; flatter means the offset DAC has no effect."""
MAX_ROUNDS: Final = 50
"""Bound on +/-1 refinement steps per search (the original's limit)."""
RANGE_CORR_LIMIT: Final = 40
"""Maximum |range correction| in DAC steps."""
MAX_NOISE_COUNTS: Final = 4.0
"""Maximum sample std (raw counts) at 2 V/div with an open input (HW: ~1). More = signal present."""
RETRY_SETTLE_S: Final = 0.1
"""Extra settle before re-reading a too-noisy first reading (HW: front end settles in ~20 ms)."""
CLIP_MARGIN: Final = 4.0
"""A mean within this many counts of 0/255 counts as saturated."""
MAX_CLIPPED_FRACTION: Final = 0.01


@dataclass(frozen=True)
class LevelReading:
    """One averaged level: mean/std of the raw samples, fraction of samples at 0 or 255."""

    dac: int
    vdiv: float
    mean: float
    std: float
    clipped: float


def _cal_settings(vdiv: VoltsPerDiv) -> Settings:
    return Settings(
        ch1=ChannelSettings(volts_per_div=vdiv, coupling=Coupling.DC, position_div=0.0),
        timebase=CAL_TIMEBASE,
    ).replace(trigger_mode=TriggerMode.NONE, trigger_level_v=0.0)


def _probe_calibration(dac: int, cal: units.Calibration) -> units.Calibration:
    """A calibration whose offset DAC byte at position 0 is exactly ``dac`` for every range."""
    return units.Calibration(
        base_ofs=dac,
        range_corr={},
        gnd_fit=cal.gnd_fit,
        counts_per_div=cal.counts_per_div,
        adc_inverted=cal.adc_inverted,
    )


def _read_level(
    io: DeviceIO, dac: int, vdiv: VoltsPerDiv, settle_s: float | None = None
) -> LevelReading:
    if not tables.OFFSET_DAC_MIN <= dac <= tables.OFFSET_DAC_MAX:
        raise CalibrationError(f"CALIBRATION FAILED: offset DAC byte {dac} out of 0..255")
    extra: dict[str, float] = {} if settle_s is None else {"settle_s": settle_s}
    data = io.live_samples(
        CAL_FRAMES * tables.FRAME_SIZE,
        timebase=CAL_TIMEBASE,
        settings=_cal_settings(vdiv),
        calibration=_probe_calibration(dac, io.calibration),
        **extra,
    )
    frames = np.frombuffer(data, dtype=np.uint8).reshape(CAL_FRAMES, tables.FRAME_SIZE)
    x = frames[:, :CAL_WINDOW].astype(np.float64)
    # std per frame (frames are not contiguous in time), averaged
    std = float(np.mean(np.std(x, axis=1)))
    clipped = float(np.mean((x <= tables.ADC_MIN) | (x >= tables.ADC_MAX)))
    r = LevelReading(dac, float(vdiv), float(x.mean()), std, clipped)
    log.debug(
        "cal level %g V/div dac=0x%02X: mean=%.2f std=%.2f clip=%.3f",
        r.vdiv,
        dac,
        r.mean,
        r.std,
        r.clipped,
    )
    return r


def _too_noisy(r: LevelReading) -> bool:
    return r.std > MAX_NOISE_COUNTS


def _check_reading(r: LevelReading, *, quiet: bool) -> None:
    if r.clipped > MAX_CLIPPED_FRACTION or not (
        tables.ADC_MIN + CLIP_MARGIN < r.mean < tables.ADC_MAX - CLIP_MARGIN
    ):
        raise CalibrationError(
            f"CALIBRATION FAILED: input saturated at {r.vdiv:g} V/div, offset DAC 0x{r.dac:02X} "
            f"(mean raw {r.mean:.1f}); disconnect the input and retry"
        )
    if quiet and _too_noisy(r):
        raise CalibrationError(
            f"CALIBRATION FAILED: a signal seems to be connected (noise {r.std:.1f} counts rms "
            f"at {r.vdiv:g} V/div, limit {MAX_NOISE_COUNTS:g}); disconnect the input and retry"
        )


def _search_dac(
    read: Callable[[int], LevelReading],
    start: int,
    target_raw: float,
    allowed: Callable[[int], bool],
    what: str,
) -> tuple[int, LevelReading]:
    """DAC byte whose mean raw level is closest to ``target_raw``.

    ``read(dac)`` measures; ``allowed(dac)`` bounds the search (outside -> CalibrationError).
    """
    cache: dict[int, LevelReading] = {}

    def level(dac: int) -> LevelReading:
        if not allowed(dac):
            raise CalibrationError(
                f"CALIBRATION FAILED: {what} needs offset DAC byte {dac} (0x{dac & 0xFF:02X}), "
                "outside the allowed range; is a voltage connected to the input?"
            )
        if dac not in cache:
            cache[dac] = read(dac)
        return cache[dac]

    r0 = level(start)
    probe = (
        start + SLOPE_PROBE_STEPS
        if allowed(start + SLOPE_PROBE_STEPS)
        else start - SLOPE_PROBE_STEPS
    )
    r1 = level(probe)
    slope = (r1.mean - r0.mean) / (probe - start)
    if abs(slope) < MIN_SLOPE:
        raise CalibrationError(
            f"CALIBRATION FAILED: the offset DAC has no effect on the level ({what}: "
            f"{slope:+.3f} counts per step)"
        )
    dac = round(start + (target_raw - r0.mean) / slope)
    best = level(dac)
    for _ in range(MAX_ROUNDS):
        err = best.mean - target_raw
        step = -1 if err / slope > 0 else 1
        nxt = dac + step
        if abs(err) <= abs(slope) / 2 or not allowed(nxt):
            break
        r = level(nxt)
        if abs(r.mean - target_raw) >= abs(err):
            break
        dac, best = nxt, r
    else:
        raise CalibrationError(f"CALIBRATION FAILED: {what} did not converge in {MAX_ROUNDS} steps")
    log.info(
        "calibration %s: DAC 0x%02X -> raw %.2f (target %.2f)", what, dac, best.mean, target_raw
    )
    return dac, best


def _base_allowed(dac: int) -> bool:
    return tables.OFFSET_DAC_MIN <= dac <= tables.OFFSET_DAC_MAX


def _fit_dacs(base: int) -> Iterable[int]:
    return (base + d for d in CAL_FIT_OFFSETS)


def _measure(io: DeviceIO) -> units.Calibration:
    cur = io.calibration
    full = float(tables.ADC_MAX)

    first = True

    def read_quiet(dac: int, vdiv: VoltsPerDiv = CAL_VDIV) -> LevelReading:
        nonlocal first
        r = _read_level(io, dac, vdiv)
        if first:
            first = False
            if _too_noisy(r):  # HW: once 6 counts rms right after open (front end settling)
                log.warning(
                    "calibration: first reading noisy (%.1f counts rms); retrying once after "
                    "%.0f ms",
                    r.std,
                    RETRY_SETTLE_S * 1e3,
                )
                r = _read_level(io, dac, vdiv, settle_s=RETRY_SETTLE_S)
        _check_reading(r, quiet=True)
        return r

    # 1. base offset at 2 V/div: ground at ad 128
    base, _ = _search_dac(
        read_quiet, tables.OFFSET_DAC_DEFAULT, full - TARGET_AD, _base_allowed, "base offset"
    )
    if not tables.CAL_BASE_OFS_MIN < base < tables.CAL_BASE_OFS_MAX:
        raise CalibrationError(
            f"CALIBRATION FAILED: base offset 0x{base:02X} outside "
            f"0x{tables.CAL_BASE_OFS_MIN:02X}..0x{tables.CAL_BASE_OFS_MAX:02X} (exclusive); "
            "is a voltage connected to the input?"
        )

    # 2. three-point ground fit (ad domain, absolute DAC byte)
    points: list[tuple[float, float]] = []
    for dac in _fit_dacs(base):
        r = read_quiet(dac)
        points.append((float(dac), full - r.mean))
    fit = units.GndFit.from_points(points)
    log.info("calibration ground fit: k=%.4f b=%.3f points=%s", fit.k, fit.b, points)
    if abs(fit.k) < MIN_SLOPE:
        raise CalibrationError(f"CALIBRATION FAILED: degenerate ground fit k={fit.k:.4f}")
    gnd_raw = full - (fit.k * base + fit.b)  # == units.gnd_counts at position 0

    # 3. range corrections: same ground level at the other ranges
    corr: dict[float, int] = {}
    for vd in sorted(tables.RANGE_CORR_VDIVS, reverse=True):
        vdiv = VoltsPerDiv.parse(vd)

        def read_range(dac: int, _v: VoltsPerDiv = vdiv) -> LevelReading:
            r = _read_level(io, dac, _v)
            _check_reading(r, quiet=False)
            return r

        def range_allowed(dac: int) -> bool:
            return abs(dac - base) <= RANGE_CORR_LIMIT and _base_allowed(dac)

        dac, _ = _search_dac(read_range, base, gnd_raw, range_allowed, f"{vdiv.label}/div range")
        corr[float(vdiv)] = dac - base

    cal = units.Calibration(
        base_ofs=base,
        range_corr=corr,
        gnd_fit=fit,
        counts_per_div=cur.counts_per_div,
        adc_inverted=cur.adc_inverted,
        source="measured",
    )
    log.info("calibration result: %s", cal.to_dict())
    return cal


def measure_calibration(scope: Scope, *, timeout_s: float = 300.0) -> units.Calibration:
    """Measure base offset, ground fit and range corrections with the input disconnected.

    Runs exclusively on the worker (acquisition paused, then resumed with the user's settings).
    Does not install or save the result: use ``Scope.calibrate`` for that (it requires the
    confirmation flags). Raises ``CalibrationError`` (message starts with
    ``"CALIBRATION FAILED"``), ``AcquisitionTimeout``, ``DeviceDisconnected``.
    """
    return scope.run_exclusive(_measure, timeout_s=timeout_s)
