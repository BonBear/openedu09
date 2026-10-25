"""Waveform measurements and decimation (pure numpy, no I/O, no threads).

All functions work on a 1-D sample array (volts) plus the sample interval ``dt`` in seconds, so
they depend on nothing but numpy. :func:`measure_waveform` / :func:`stats_waveform` accept any
object with ``volts`` and ``dt`` attributes (e.g. :class:`edu09.model.Waveform`).

Conventions (spec sec. 6):

* Amplitude measurements ignore non-finite samples; if no finite sample is left they are invalid.
* Timing measurements use mean crossings with a Schmitt trigger whose band is 10 % of Vpp
  (mean +/- 5 % Vpp). ``frequency``/``period``/``duty`` need at least 2 full periods.
  ``rise_time``/``fall_time`` use the 10 %-90 % levels between vmin and vmax.
  Any non-finite sample makes timing measurements invalid.
* Invalid measurements have ``value=None``, ``valid=False`` and a ``note``.
* Crossing times are linearly interpolated between samples, so sub-sample resolution is
  available but edges faster than one sample cannot be resolved.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import numpy as np
import numpy.typing as npt

__all__ = [
    "MEASUREMENTS",
    "UNITS",
    "HYSTERESIS_FRACTION",
    "MIN_PERIODS",
    "Measurement",
    "Decimated",
    "Edges",
    "SampledSignal",
    "find_edges",
    "measure",
    "measure_one",
    "measure_waveform",
    "stats",
    "stats_waveform",
    "decimate",
    "despike",
]

FloatArray = npt.NDArray[np.float64]

#: Measurement names in canonical order (also the MCP ``measure`` vocabulary).
MEASUREMENTS: Final[tuple[str, ...]] = (
    "vpp",
    "vmax",
    "vmin",
    "mean",
    "vrms",
    "ac_rms",
    "frequency",
    "period",
    "duty",
    "rise_time",
    "fall_time",
    "pos_width",
    "neg_width",
)

UNITS: Final[dict[str, str]] = {
    "vpp": "V",
    "vmax": "V",
    "vmin": "V",
    "mean": "V",
    "vrms": "V",
    "ac_rms": "V",
    "frequency": "Hz",
    "period": "s",
    "duty": "%",
    "rise_time": "s",
    "fall_time": "s",
    "pos_width": "s",
    "neg_width": "s",
}

#: Total Schmitt-trigger band around the mean, as a fraction of Vpp.
HYSTERESIS_FRACTION: Final[float] = 0.10
#: Minimum number of full periods for frequency/period/duty.
MIN_PERIODS: Final[int] = 2
_LOW_LEVEL: Final[float] = 0.10
_HIGH_LEVEL: Final[float] = 0.90


@dataclass(frozen=True)
class Measurement:
    """One measurement result; ``value`` is None exactly when ``valid`` is False."""

    value: float | None
    unit: str
    valid: bool
    note: str | None = None


@dataclass(frozen=True)
class Decimated:
    """Decimated samples.

    ``values[i]`` is ``samples[indices[i]]``; ``indices`` are strictly increasing, so the time of
    each point is ``t0 + indices[i] * dt``. ``factor`` is the number of source samples per bucket
    (1 when no decimation was needed).
    """

    values: FloatArray
    indices: npt.NDArray[np.intp]
    method: str
    factor: int


@dataclass(frozen=True)
class Edges:
    """Mean crossings found by :func:`find_edges`, in fractional sample positions."""

    rising: FloatArray
    falling: FloatArray
    mean: float
    vmin: float
    vmax: float


class SampledSignal(Protocol):
    """Anything carrying ``volts`` and ``dt`` (structurally matches ``edu09.model.Waveform``)."""

    @property
    def volts(self) -> npt.NDArray[np.floating]: ...  # pragma: no cover

    @property
    def dt(self) -> float: ...  # pragma: no cover


# --------------------------------------------------------------------------- helpers


def _as_1d(samples: npt.ArrayLike) -> FloatArray:
    arr = np.asarray(samples, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"samples must be 1-D, got shape {arr.shape}")
    return arr


def _check_dt(dt: float) -> float:
    dt = float(dt)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError(f"dt must be a finite positive number, got {dt!r}")
    return dt


def _ok(name: str, value: float) -> Measurement:
    return Measurement(value=float(value), unit=UNITS[name], valid=True)


def _bad(name: str, note: str) -> Measurement:
    return Measurement(value=None, unit=UNITS[name], valid=False, note=note)


def _schmitt_states(x: FloatArray, lo: float, hi: float) -> npt.NDArray[np.int8]:
    """Return +1/-1 state per sample (0 before the first band exit), forward-filled."""
    cls = np.zeros(x.shape, dtype=np.int8)
    cls[x >= hi] = 1
    cls[x <= lo] = -1
    idx = np.where(cls != 0, np.arange(x.size), 0)
    np.maximum.accumulate(idx, out=idx)
    state = cls[idx]
    # Samples before the first band exit stay unknown (0).
    first = np.flatnonzero(cls)
    if first.size:
        state[: first[0]] = 0
    else:
        state[:] = 0
    return state


def find_edges(samples: npt.ArrayLike) -> Edges | None:
    """Find mean crossings with 10 % Vpp hysteresis.

    Returns None if there are fewer than 2 samples, any sample is non-finite, or Vpp is zero.
    An edge is only reported when the state was known beforehand, so the start of the record
    never yields a spurious edge.
    """
    x = _as_1d(samples)
    if x.size < 2 or not np.all(np.isfinite(x)):
        return None
    vmin = float(x.min())
    vmax = float(x.max())
    vpp = vmax - vmin
    if not vpp > 0.0:
        return None
    mean = float(x.mean())
    half_band = 0.5 * HYSTERESIS_FRACTION * vpp
    state = _schmitt_states(x, mean - half_band, mean + half_band)
    change = np.flatnonzero((state[1:] != state[:-1]) & (state[:-1] != 0)) + 1
    rising: list[float] = []
    falling: list[float] = []
    for i in change:
        if state[i] > 0:
            # last sample below the mean before i (exists: some sample <= lo precedes i)
            j = int(np.flatnonzero(x[:i] < mean)[-1])
            rising.append(j + (mean - x[j]) / (x[j + 1] - x[j]))
        else:
            j = int(np.flatnonzero(x[:i] > mean)[-1])
            falling.append(j + (x[j] - mean) / (x[j] - x[j + 1]))
    return Edges(
        rising=np.asarray(rising, dtype=np.float64),
        falling=np.asarray(falling, dtype=np.float64),
        mean=mean,
        vmin=vmin,
        vmax=vmax,
    )


def _period_samples(edges: Edges) -> float | None:
    """Mean period in samples from whichever edge kind spans >= MIN_PERIODS full periods."""
    best = edges.rising if edges.rising.size >= edges.falling.size else edges.falling
    if best.size < MIN_PERIODS + 1:
        return None
    return float((best[-1] - best[0]) / (best.size - 1))


def _pulse_widths(edges: Edges) -> tuple[FloatArray, FloatArray]:
    """High widths (rise -> next fall) and low widths (fall -> next rise), in samples."""
    r, f = edges.rising, edges.falling
    high: list[float] = []
    low: list[float] = []
    if r.size and f.size:
        k = np.searchsorted(f, r, side="right")
        for ri, ki in zip(r, k, strict=True):
            if ki < f.size:
                high.append(float(f[ki] - ri))
        k = np.searchsorted(r, f, side="right")
        for fi, ki in zip(f, k, strict=True):
            if ki < r.size:
                low.append(float(r[ki] - fi))
    return np.asarray(high, dtype=np.float64), np.asarray(low, dtype=np.float64)


def _duty_percent(edges: Edges) -> float | None:
    """High-time fraction over the complete rising-to-rising periods (needs MIN_PERIODS)."""
    r, f = edges.rising, edges.falling
    if r.size < MIN_PERIODS + 1:
        # fall back to falling-referenced periods
        if f.size < MIN_PERIODS + 1:
            return None
        span = float(f[-1] - f[0])
        low = 0.0
        for a, b in zip(f[:-1], f[1:], strict=True):
            inside = r[(r > a) & (r < b)]
            if inside.size == 0:
                return None
            low += float(inside[0] - a)
        return 100.0 * (span - low) / span
    span = float(r[-1] - r[0])
    high = 0.0
    for a, b in zip(r[:-1], r[1:], strict=True):
        inside = f[(f > a) & (f < b)]
        if inside.size == 0:
            return None
        high += float(inside[0] - a)
    return 100.0 * high / span


def _transition_times(x: FloatArray, edges: Edges, rising: bool) -> FloatArray:
    """10 %-90 % transition durations (samples) for each complete rising/falling edge."""
    lo = edges.vmin + _LOW_LEVEL * (edges.vmax - edges.vmin)
    hi = edges.vmin + _HIGH_LEVEL * (edges.vmax - edges.vmin)
    all_edges = np.sort(np.concatenate([edges.rising, edges.falling]))
    own = edges.rising if rising else edges.falling
    out: list[float] = []
    n = x.size
    for c in own:
        pos = int(np.searchsorted(all_edges, c))
        start = 0 if pos == 0 else int(math.ceil(all_edges[pos - 1]))
        stop = n if pos + 1 >= all_edges.size else int(math.floor(all_edges[pos + 1])) + 1
        ci = int(math.floor(c))
        before = x[start : ci + 1]
        after = x[ci + 1 : stop]
        if rising:
            b = np.flatnonzero(before <= lo)
            a = np.flatnonzero(after >= hi)
        else:
            b = np.flatnonzero(before >= hi)
            a = np.flatnonzero(after <= lo)
        if b.size == 0 or a.size == 0:
            continue
        j = start + int(b[-1])  # last sample on the far side of the start level
        k = ci + 1 + int(a[0])  # first sample past the end level
        lvl_start, lvl_end = (lo, hi) if rising else (hi, lo)
        t_start = j + (lvl_start - x[j]) / (x[j + 1] - x[j]) if x[j + 1] != x[j] else float(j)
        t_end = (k - 1) + (lvl_end - x[k - 1]) / (x[k] - x[k - 1]) if x[k] != x[k - 1] else float(k)
        out.append(float(t_end - t_start))
    return np.asarray(out, dtype=np.float64)


# --------------------------------------------------------------------------- measurements


def _measure_all(x: FloatArray, dt: float, names: tuple[str, ...]) -> dict[str, Measurement]:
    results: dict[str, Measurement] = {}
    finite = x[np.isfinite(x)]
    need_amp = any(n in names for n in ("vpp", "vmax", "vmin", "mean", "vrms", "ac_rms"))
    if need_amp:
        if finite.size == 0:
            why = "no samples" if x.size == 0 else "no finite samples"
            amp = {n: _bad(n, why) for n in ("vpp", "vmax", "vmin", "mean", "vrms", "ac_rms")}
        else:
            vmax = float(finite.max())
            vmin = float(finite.min())
            mean = float(finite.mean())
            amp = {
                "vpp": _ok("vpp", vmax - vmin),
                "vmax": _ok("vmax", vmax),
                "vmin": _ok("vmin", vmin),
                "mean": _ok("mean", mean),
                "vrms": _ok("vrms", math.sqrt(float(np.mean(finite * finite)))),
                "ac_rms": _ok("ac_rms", float(np.std(finite))),
            }
            if finite.size != x.size:
                why = f"{x.size - finite.size} non-finite samples ignored"
                amp = {k: Measurement(v.value, v.unit, v.valid, why) for k, v in amp.items()}
        results.update({n: amp[n] for n in names if n in amp})

    timing = [n for n in names if n not in results]
    if not timing:
        return results

    note: str | None = None
    edges: Edges | None = None
    if x.size < 2:
        note = "too few samples"
    elif finite.size != x.size:
        note = "non-finite samples"
    else:
        edges = find_edges(x)
        if edges is None:
            note = "no signal swing (Vpp = 0)"
    if edges is None:
        for n in timing:
            results[n] = _bad(n, note or "invalid input")
        return results

    period_s = _period_samples(edges)
    few = f"fewer than {MIN_PERIODS} full periods (mean crossing, 10% Vpp hysteresis)"
    widths: tuple[FloatArray, FloatArray] | None = None
    for n in timing:
        if n in ("frequency", "period"):
            if period_s is None or period_s <= 0.0:
                results[n] = _bad(n, few)
            elif n == "period":
                results[n] = _ok(n, period_s * dt)
            else:
                results[n] = _ok(n, 1.0 / (period_s * dt))
        elif n == "duty":
            duty = _duty_percent(edges)
            results[n] = _bad(n, few) if duty is None else _ok(n, duty)
        elif n in ("pos_width", "neg_width"):
            if widths is None:
                widths = _pulse_widths(edges)
            w = widths[0] if n == "pos_width" else widths[1]
            if w.size == 0:
                results[n] = _bad(n, "no complete pulse")
            else:
                results[n] = _ok(n, float(w.mean()) * dt)
        elif n in ("rise_time", "fall_time"):
            tt = _transition_times(x, edges, rising=(n == "rise_time"))
            if tt.size == 0:
                kind = "rising" if n == "rise_time" else "falling"
                results[n] = _bad(n, f"no complete {kind} edge (10%-90%)")
            else:
                results[n] = _ok(n, float(tt.mean()) * dt)
        else:  # pragma: no cover - guarded by _normalize_names
            raise ValueError(f"unknown measurement {n!r}")
    return results


def _normalize_names(names: Iterable[str] | None) -> tuple[str, ...]:
    if names is None:
        return MEASUREMENTS
    if isinstance(names, str):
        names = (names,)
    out: list[str] = []
    for raw in names:
        n = raw.strip().lower()
        if n not in UNITS:
            expected = ", ".join(MEASUREMENTS)
            raise ValueError(f"unknown measurement {raw!r}; expected one of {expected}")
        if n not in out:
            out.append(n)
    return tuple(out)


def measure(
    samples: npt.ArrayLike, dt: float, names: Iterable[str] | None = None
) -> dict[str, Measurement]:
    """Compute the requested measurements (all of :data:`MEASUREMENTS` if ``names`` is None).

    Result keys follow the order of ``names`` (duplicates removed). Raises ValueError for an
    unknown name, non-1-D input, or a non-positive/non-finite ``dt``.
    """
    x = _as_1d(samples)
    dt = _check_dt(dt)
    wanted = _normalize_names(names)
    res = _measure_all(x, dt, wanted)
    return {n: res[n] for n in wanted}


def measure_one(samples: npt.ArrayLike, dt: float, name: str) -> Measurement:
    """Single measurement by name."""
    return measure(samples, dt, (name,))[_normalize_names((name,))[0]]


def stats(samples: npt.ArrayLike, dt: float) -> dict[str, float | None]:
    """MCP ``acquire`` stats: ``{min, max, mean, vpp, vrms, frequency_hz}`` (None when invalid)."""
    m = measure(samples, dt, ("vmin", "vmax", "mean", "vpp", "vrms", "frequency"))
    return {
        "min": m["vmin"].value,
        "max": m["vmax"].value,
        "mean": m["mean"].value,
        "vpp": m["vpp"].value,
        "vrms": m["vrms"].value,
        "frequency_hz": m["frequency"].value,
    }


def measure_waveform(
    wf: SampledSignal, names: Iterable[str] | None = None
) -> dict[str, Measurement]:
    """:func:`measure` on a Waveform-like object (uses ``wf.volts`` and ``wf.dt``)."""
    return measure(wf.volts, wf.dt, names)


def stats_waveform(wf: SampledSignal) -> dict[str, float | None]:
    """:func:`stats` on a Waveform-like object."""
    return stats(wf.volts, wf.dt)


# --------------------------------------------------------------------------- decimation


def decimate(
    samples: npt.ArrayLike,
    max_points: int,
    method: Literal["minmax", "stride"] = "minmax",
) -> Decimated:
    """Reduce ``samples`` to at most ``max_points`` points.

    * ``minmax``: split into ``max_points // 2`` contiguous buckets (all of equal size ``factor``
      except possibly the last, shorter one) and emit each bucket's min and max sample, ordered by
      their position in time (a single point if both are the same sample). Peaks and glitches
      survive. NaNs are skipped unless a whole bucket is NaN (then a NaN point is emitted).
    * ``stride``: every ``factor``-th sample, starting at index 0.

    If ``len(samples) <= max_points`` the samples are returned unchanged (``factor == 1``).
    """
    x = _as_1d(samples)
    if method not in ("minmax", "stride"):
        raise ValueError(f"unknown decimation method {method!r}; expected 'minmax' or 'stride'")
    max_points = int(max_points)
    min_needed = 2 if method == "minmax" else 1
    if max_points < min_needed:
        raise ValueError(f"max_points must be >= {min_needed} for {method!r}, got {max_points}")
    n = x.size
    if n <= max_points:
        return Decimated(
            values=x.copy(), indices=np.arange(n, dtype=np.intp), method=method, factor=1
        )
    if method == "stride":
        factor = -(-n // max_points)
        idx = np.arange(0, n, factor, dtype=np.intp)
        return Decimated(values=x[idx], indices=idx, method=method, factor=factor)

    buckets = max_points // 2
    factor = -(-n // buckets)
    nb = -(-n // factor)
    pad = nb * factor - n
    nan = np.isnan(x)
    lo_src = np.where(nan, np.inf, x)
    hi_src = np.where(nan, -np.inf, x)
    if pad:
        lo_src = np.concatenate([lo_src, np.full(pad, np.inf)])
        hi_src = np.concatenate([hi_src, np.full(pad, -np.inf)])
    base = np.arange(nb, dtype=np.intp) * factor
    imin = base + np.argmin(lo_src.reshape(nb, factor), axis=1)
    imax = base + np.argmax(hi_src.reshape(nb, factor), axis=1)
    first = np.minimum(imin, imax)
    second = np.maximum(imin, imax)
    pairs = np.stack([first, second], axis=1).reshape(-1)
    keep = np.ones(pairs.size, dtype=bool)
    keep[1::2] = second != first
    idx = pairs[keep].astype(np.intp)
    return Decimated(values=x[idx], indices=idx, method=method, factor=factor)


# --------------------------------------------------------------------------- despike (opt-in)


def despike(samples: npt.ArrayLike, threshold: float = 0.0) -> FloatArray:
    """Replace isolated one-sample outliers by the median of the sample and its two neighbours.

    A sample is replaced only if it lies above *both* neighbours (or below both) by more than
    ``threshold`` (same unit as ``samples``, >= 0); it then becomes the nearer neighbour, which is
    the median of the three. Steps and monotonic edges are unchanged; the first and last samples
    and NaN neighbourhoods are left alone. Returns a new float array.

    Opt-in, never applied by default: H1/H2 ETS frames (50/20/10 us/div) carry a few isolated
    outlier samples that make ``frequency`` read ~31-34 kHz on a 29.9 kHz square; despiking them
    restores 29.9 kHz. On undersampled realtime frames (a few samples per period) single-sample
    pulses are genuine signal and despiking corrupts the measurement.
    """
    if not (math.isfinite(threshold) and threshold >= 0):
        raise ValueError(f"threshold must be a finite number >= 0, got {threshold!r}")
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError("samples must be 1-D")
    y = x.copy()
    if x.size < 3:
        return y
    a, b, c = x[:-2], x[1:-1], x[2:]
    hi_n = np.maximum(a, c)
    lo_n = np.minimum(a, c)
    with np.errstate(invalid="ignore"):
        high = (b - hi_n) > threshold
        low = (lo_n - b) > threshold
    y[1:-1] = np.where(high, hi_n, np.where(low, lo_n, b))
    return y
