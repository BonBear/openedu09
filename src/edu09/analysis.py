"""Waveform measurements, decimation and FFT spectrum (pure numpy, no I/O, no threads).

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

import functools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

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
    "WINDOWS",
    "SpectrumPeak",
    "Spectrum",
    "spectrum",
    "spectrum_average",
    "spectrum_waveform",
    "INPUT_BANDWIDTH_HZ",
    "CLIPPED_WARNING",
    "ETS_WARNING",
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


# --------------------------------------------------------------------------- spectrum (FFT)

#: Window names accepted by :func:`spectrum` (first = default).
WINDOWS: Final[tuple[str, ...]] = ("hann", "hamming", "blackman", "flattop", "rect")

#: Analog input bandwidth of the EDU09 front end (manual); there is no anti-alias filter.
INPUT_BANDWIDTH_HZ: Final[float] = 200e3
#: A fundamental needs this many dB above the median bin (noise floor estimate).
_FUNDAMENTAL_SNR_DB: Final[float] = 20.0
#: THD uses harmonics 2.._THD_MAX_HARMONIC.
_THD_MAX_HARMONIC: Final[int] = 10
#: THD needs the fundamental at or above this bin.
_THD_MIN_BIN: Final[int] = 6
#: Harmonic search half-width in bins (THD).
_THD_SEARCH_BINS: Final[int] = 2
#: "too few periods" when f1 < this many resolution bins.
_MIN_PERIODS_BINS: Final[float] = 3.0
_DB_FLOOR_VRMS: Final[float] = 1e-12

# Periodic (DFT-even) cosine-sum windows: w[k] = sum_j (-1)^j a_j cos(2 pi j k / n).
_COSINE_COEFFS: Final[dict[str, tuple[float, ...]]] = {
    "hann": (0.5, 0.5),
    "hamming": (0.54, 0.46),
    "blackman": (0.42, 0.5, 0.08),
    # SciPy / Matlab "flattop" (5-term, about 0.02 dB scalloping)
    "flattop": (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368),
    "rect": (1.0,),
}


@dataclass(frozen=True)
class SpectrumPeak:
    """One spectral peak.

    ``freq_hz`` comes from parabolic interpolation of the dB spectrum over the three bins around
    the local maximum; ``vrms`` is the interpolated (vertex) amplitude, i.e. the scalloping loss
    of the window is largely removed. ``bin`` is the index of the local-maximum bin.
    """

    freq_hz: float
    vrms: float
    dbv: float
    bin: int


@dataclass(frozen=True)
class Spectrum:
    """Single-sided amplitude spectrum of one frame (or an average of frames).

    ``vrms[k]`` is the window-corrected (coherent gain) RMS amplitude of a sine centred on bin
    ``k``; ``dbv = 20*log10(max(vrms, 1e-12))``. ``freqs_hz[k] = k * resolution_hz`` for
    ``k = 0 .. n//2``.
    """

    freqs_hz: FloatArray
    vrms: FloatArray
    dbv: FloatArray
    window: str
    fs_hz: float
    resolution_hz: float
    enbw_bins: float
    n: int
    averages: int
    dc_v: float
    peaks: tuple[SpectrumPeak, ...]
    fundamental: SpectrumPeak | None
    thd_pct: float | None
    warnings: tuple[str, ...]


@functools.lru_cache(maxsize=32)
def _window_cached(key: str, n: int) -> FloatArray:
    k = np.arange(n, dtype=np.float64)
    w = np.zeros(n, dtype=np.float64)
    for j, a in enumerate(_COSINE_COEFFS[key]):
        w += (-1.0) ** j * a * np.cos(2.0 * math.pi * j * k / n)
    w.setflags(write=False)
    return w


def _window(key: str, n: int) -> FloatArray:
    """Periodic window ``key`` (already validated by :func:`_window_key`) of length ``n``."""
    return _window_cached(key, n)


#: Sub-bin offsets (0 .. 0.5 bin) of the tabulated window responses.
_RESP_GRID: Final[FloatArray] = np.linspace(0.0, 0.5, 257)


@functools.lru_cache(maxsize=32)
def _response_table(key: str, n: int) -> FloatArray:
    """``|W(d)| / W(0)`` of window ``key`` on :data:`_RESP_GRID` (symmetric in ``d``)."""
    w = _window_cached(key, n)
    k = np.arange(n, dtype=np.float64)
    phase = np.exp(-2j * math.pi * np.outer(_RESP_GRID, k) / n)
    table = np.abs(phase @ w) / float(w.sum())
    table.setflags(write=False)
    return np.asarray(table, dtype=np.float64)


def _window_key(name: str) -> str:
    key = name.strip().lower() if isinstance(name, str) else ""
    if key not in _COSINE_COEFFS:
        raise ValueError(f"unknown window {name!r}; expected one of {', '.join(WINDOWS)}")
    return key


def _fmt_hz(f: float) -> str:
    if f >= 1e6:
        return f"{f / 1e6:.4g} MHz"
    if f >= 1e3:
        return f"{f / 1e3:.4g} kHz"
    return f"{f:.4g} Hz"


def _amplitude_bins(x: FloatArray, w: FloatArray) -> FloatArray:
    """Single-sided, coherent-gain corrected RMS amplitude per bin (length n//2 + 1)."""
    n = x.size
    mag = np.abs(np.fft.rfft(x * w))
    scale = float(w.sum())  # n * coherent gain
    amp = mag / scale
    amp[1:] *= math.sqrt(2.0)
    if n % 2 == 0:
        amp[-1] /= math.sqrt(2.0)  # Nyquist bin is single (like DC)
    return np.asarray(amp, dtype=np.float64)


def _to_db(vrms: FloatArray) -> FloatArray:
    return np.asarray(20.0 * np.log10(np.maximum(vrms, _DB_FLOOR_VRMS)), dtype=np.float64)


def _interp_peak(
    dbv: FloatArray, vrms: FloatArray, resp: FloatArray, k: int, df: float
) -> SpectrumPeak:
    """Peak at local-maximum bin ``k``.

    Frequency: vertex of the parabola through the dB values of bins ``k-1, k, k+1`` (offset
    clamped to +-0.5 bin; the bin itself at the spectrum edges). Amplitude: the bin value divided
    by the window's response at that offset, which removes the scalloping loss.
    """
    last = dbv.size - 1
    p = 0.0
    if 0 < k < last:
        a, b, c = float(dbv[k - 1]), float(dbv[k]), float(dbv[k + 1])
        denom = a - 2.0 * b + c
        if denom < 0.0:
            p = max(-0.5, min(0.5, 0.5 * (a - c) / denom))
    r = float(np.interp(abs(p), _RESP_GRID, resp))
    amp = float(vrms[k]) / r if r > 0.0 else float(vrms[k])
    amp = max(amp, float(vrms[k]))
    return SpectrumPeak(
        freq_hz=(k + p) * df, vrms=amp, dbv=20.0 * math.log10(max(amp, _DB_FLOOR_VRMS)), bin=int(k)
    )


def _find_peaks(
    dbv: FloatArray,
    vrms: FloatArray,
    resp: FloatArray,
    df: float,
    enbw_bins: float,
    min_peak_db: float,
    first_bin: int = 1,
    limit: int = 1,
) -> list[SpectrumPeak]:
    """Up to ``limit`` merged local maxima at bins >= ``first_bin`` (DC excluded), strongest first.

    A local maximum is ``>=`` its left and ``>`` its right neighbour (``>`` the left one at the
    Nyquist bin), so flat runs such as an all-zero spectrum yield no peak. A weaker maximum closer
    than ``enbw_bins + 2`` bins to a stronger accepted one is dropped.
    """
    m = dbv.size
    first_bin = max(1, first_bin)
    if m <= first_bin or limit <= 0:
        return []
    thresh = float(dbv[first_bin:].max()) + float(min_peak_db)
    v = dbv[first_bin:]
    left = dbv[first_bin - 1 : m - 1]
    right = np.append(dbv[first_bin + 1 :], -np.inf)
    is_max = (v >= left) & (v > right)
    is_max[-1] = v[-1] > left[-1]  # Nyquist bin: strict on the only neighbour
    ok = is_max & (v >= thresh) & (vrms[first_bin:] > _DB_FLOOR_VRMS)
    cand = np.flatnonzero(ok) + first_bin
    cand = cand[np.lexsort((cand, -dbv[cand]))]  # strongest first, then lowest bin
    reach = math.ceil(enbw_bins + 2.0) - 1  # |k - j| < enbw + 2  <=>  |k - j| <= reach
    blocked = np.zeros(m, dtype=bool)
    kept: list[int] = []
    for k in cand.tolist():
        if blocked[k]:
            continue
        kept.append(k)
        if len(kept) >= limit:
            break
        blocked[max(0, k - reach) : k + reach + 1] = True
    return [_interp_peak(dbv, vrms, resp, k, df) for k in kept]


def _thd(
    dbv: FloatArray, vrms: FloatArray, resp: FloatArray, fund: SpectrumPeak, df: float
) -> float | None:
    m = dbv.size
    nyq_bin = m - 1
    if fund.bin < _THD_MIN_BIN:
        return None
    f1_bins = fund.freq_hz / df
    total = 0.0
    found = 0
    for h in range(2, _THD_MAX_HARMONIC + 1):
        centre = h * f1_bins
        if centre >= nyq_bin:
            break
        lo = max(1, int(math.floor(centre - _THD_SEARCH_BINS)))
        hi = min(nyq_bin, int(math.ceil(centre + _THD_SEARCH_BINS)))
        k = lo + int(np.argmax(dbv[lo : hi + 1]))
        pk = _interp_peak(dbv, vrms, resp, k, df)
        total += pk.vrms * pk.vrms
        found += 1
    if found < 1 or fund.vrms <= 0.0:
        return None
    return 100.0 * math.sqrt(total) / fund.vrms


def _finish(
    vrms: FloatArray,
    resp: FloatArray,
    *,
    window: str,
    fs: float,
    n: int,
    enbw_bins: float,
    averages: int,
    dc_v: float,
    max_peaks: int,
    min_peak_db: float,
    first_bin: int = 1,
    extra_warnings: Sequence[str] = (),
) -> Spectrum:
    if max_peaks < 0:
        raise ValueError(f"max_peaks must be >= 0, got {max_peaks}")
    if not math.isfinite(min_peak_db):
        raise ValueError(f"min_peak_db must be finite, got {min_peak_db!r}")
    df = fs / n
    dbv = _to_db(vrms)
    freqs = np.arange(vrms.size, dtype=np.float64) * df
    all_peaks = _find_peaks(
        dbv, vrms, resp, df, enbw_bins, min_peak_db, first_bin, max(1, max_peaks)
    )
    fundamental: SpectrumPeak | None = None
    if all_peaks and dbv.size > 1:
        floor = float(np.median(dbv[max(1, first_bin) :]))
        if all_peaks[0].dbv >= floor + _FUNDAMENTAL_SNR_DB:
            fundamental = all_peaks[0]
    thd = _thd(dbv, vrms, resp, fundamental, df) if fundamental is not None else None
    warnings: list[str] = []
    if fs / 2.0 < INPUT_BANDWIDTH_HZ:
        warnings.append(
            f"aliasing: components above fs/2 = {_fmt_hz(fs / 2.0)} fold back "
            "(input bandwidth ~200 kHz)"
        )
    warnings.extend(extra_warnings)
    if fundamental is not None and fundamental.freq_hz < _MIN_PERIODS_BINS * df:
        warnings.append(
            f"too few periods: fundamental {_fmt_hz(fundamental.freq_hz)} is below "
            f"{_MIN_PERIODS_BINS:g} x resolution ({_fmt_hz(df)}); use a slower timebase"
        )
    return Spectrum(
        freqs_hz=freqs,
        vrms=vrms,
        dbv=dbv,
        window=window,
        fs_hz=fs,
        resolution_hz=df,
        enbw_bins=enbw_bins,
        n=n,
        averages=averages,
        dc_v=dc_v,
        peaks=tuple(all_peaks[:max_peaks]),
        fundamental=fundamental,
        thd_pct=thd,
        warnings=tuple(warnings),
    )


def _first_peak_bin(key: str, remove_dc: bool) -> int:
    """Lowest bin searched for peaks: without DC removal skip the window's DC main lobe."""
    return 1 if remove_dc else len(_COSINE_COEFFS[key])


def _prepare(samples: npt.ArrayLike, remove_dc: bool) -> tuple[FloatArray, float]:
    x = _as_1d(samples)
    if x.size < 8:
        raise ValueError(f"spectrum needs at least 8 samples, got {x.size}")
    if not np.all(np.isfinite(x)):
        raise ValueError("spectrum needs finite samples (NaN/inf found)")
    dc = float(x.mean())
    if remove_dc:
        x = x - dc
    return x, dc


def _spectrum(
    samples: npt.ArrayLike,
    dt: float,
    *,
    window: str = "hann",
    remove_dc: bool = True,
    max_peaks: int = 8,
    min_peak_db: float = -60.0,
    extra_warnings: Sequence[str] = (),
) -> Spectrum:
    dt = _check_dt(dt)
    key = _window_key(window)
    x, dc = _prepare(samples, remove_dc)
    n = x.size
    w = _window(key, n)
    enbw = float(n * np.sum(w * w) / float(w.sum()) ** 2)
    return _finish(
        _amplitude_bins(x, w),
        _response_table(key, n),
        window=key,
        fs=1.0 / dt,
        n=n,
        enbw_bins=enbw,
        averages=1,
        dc_v=dc,
        max_peaks=int(max_peaks),
        min_peak_db=float(min_peak_db),
        first_bin=_first_peak_bin(key, remove_dc),
        extra_warnings=extra_warnings,
    )


def spectrum(
    samples: npt.ArrayLike,
    dt: float,
    *,
    window: str = "hann",
    remove_dc: bool = True,
    max_peaks: int = 8,
    min_peak_db: float = -60.0,
) -> Spectrum:
    """Single-sided FFT amplitude spectrum (Vrms per bin) of one frame sampled every ``dt`` s.

    The mean is removed first (``remove_dc``; reported as ``dc_v`` either way), the frame is
    multiplied by a periodic ``window`` (:data:`WINDOWS`) and transformed without zero padding,
    giving ``n//2 + 1`` bins from 0 to ``fs/2`` spaced ``fs/n``. Bin amplitudes are corrected by the
    window's coherent gain, so a sine of amplitude A centred on a bin reads A/sqrt(2) Vrms there.

    Peaks are local maxima within ``min_peak_db`` (<= 0) of the strongest non-DC bin (with
    ``remove_dc=False`` the bins of the window's DC main lobe are skipped too), merged when
    closer than ``enbw_bins + 2`` bins, strongest first, at most ``max_peaks``. A peak's frequency
    is the vertex of a parabola through the three dB values around the maximum; its amplitude is
    the bin value divided by the window's response at that offset (scalloping correction).
    Accuracy for a single noise-free sine anywhere between two bins (measured, n = 1024):

    ========  ===============  ==================  =====================
    window    bin value        peak ``vrms``       peak ``freq_hz``
    ========  ===============  ==================  =====================
    flattop   -0.11 %..+0.03 %  0..+0.07 %          within 0.15 bin
    blackman  up to 12 % low   0..+0.2 %           within 0.01 bin
    hann      up to 15 % low   0..+0.7 %           within 0.02 bin
    hamming   up to 18 % low   0..+0.9 %           within 0.02 bin
    rect      up to 37 % low   up to 16 % low      within 0.2 bin
    ========  ===============  ==================  =====================

    ``rect`` leaks strongly; use it only with a whole number of periods in the frame. Use
    ``flattop`` for amplitudes, ``hann``/``blackman`` to separate close components.

    The fundamental is the strongest peak if it is at least 20 dB above the median bin (the
    strongest bin of pure noise reaches about 14 dB). THD is ``sqrt(sum h_k^2) / h_1`` (in %) over
    harmonics 2..10 below fs/2, each the largest bin within +-2 bins of ``k * f1``; None if no
    harmonic is below fs/2 or ``f1`` is below bin 6.
    """
    return _spectrum(
        samples,
        dt,
        window=window,
        remove_dc=remove_dc,
        max_peaks=max_peaks,
        min_peak_db=min_peak_db,
    )


def spectrum_average(
    frames: Sequence[npt.ArrayLike],
    dt: float,
    *,
    window: str = "hann",
    mode: Literal["power", "peak_hold"] = "power",
    **kw: Any,
) -> Spectrum:
    """Spectrum of several frames of equal length and ``dt``.

    ``power``: RMS average of the bin amplitudes (mean of ``|X|^2``), which lowers the noise
    variance but not the noise floor's mean; ``peak_hold``: per-bin maximum. ``dc_v`` is the
    mean over all frames. ``kw`` takes ``remove_dc``, ``max_peaks`` and ``min_peak_db``.
    """
    if mode not in ("power", "peak_hold"):
        raise ValueError(f"unknown averaging mode {mode!r}; expected 'power' or 'peak_hold'")
    unknown = set(kw) - {"remove_dc", "max_peaks", "min_peak_db"}
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {', '.join(sorted(unknown))}")
    remove_dc = bool(kw.get("remove_dc", True))
    if len(frames) == 0:
        raise ValueError("spectrum_average needs at least one frame")
    dt = _check_dt(dt)
    key = _window_key(window)
    prepared = [_prepare(f, remove_dc) for f in frames]
    n = prepared[0][0].size
    if any(x.size != n for x, _ in prepared):
        raise ValueError("spectrum_average needs frames of equal length")
    w = _window(key, n)
    enbw = float(n * np.sum(w * w) / float(w.sum()) ** 2)
    amps = np.stack([_amplitude_bins(x, w) for x, _ in prepared])
    vrms = np.sqrt(np.mean(amps * amps, axis=0)) if mode == "power" else amps.max(axis=0)
    return _finish(
        np.asarray(vrms, dtype=np.float64),
        _response_table(key, n),
        window=key,
        fs=1.0 / dt,
        n=n,
        enbw_bins=enbw,
        averages=len(prepared),
        dc_v=float(np.mean([d for _, d in prepared])),
        max_peaks=int(kw.get("max_peaks", 8)),
        min_peak_db=float(kw.get("min_peak_db", -60.0)),
        first_bin=_first_peak_bin(key, remove_dc),
    )


#: Warning added by :func:`spectrum_waveform` for a clipped frame.
CLIPPED_WARNING: Final[str] = "clipped: the frame hits the ADC limits, harmonics are not real"
#: Warning added by :func:`spectrum_waveform` for an ETS frame.
ETS_WARNING: Final[str] = (
    "repetitive signals only: equivalent-time (ETS) frame, the spectrum is valid only for a "
    "stable repetitive signal"
)


def spectrum_waveform(wf: SampledSignal, **kw: Any) -> Spectrum:
    """:func:`spectrum` on a Waveform-like object, adding the ``clipped`` / ETS warnings.

    Uses ``wf.volts`` and ``wf.dt``; ``wf.clipped`` (if present and true) adds
    :data:`CLIPPED_WARNING`, an ETS timebase (``wf.settings.timebase.is_ets``) adds
    :data:`ETS_WARNING`. ``kw`` is passed to :func:`spectrum`.
    """
    unknown = set(kw) - {"window", "remove_dc", "max_peaks", "min_peak_db"}
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {', '.join(sorted(unknown))}")
    extra: list[str] = []
    if bool(getattr(wf, "clipped", False)):
        extra.append(CLIPPED_WARNING)
    tb = getattr(getattr(wf, "settings", None), "timebase", None)
    if bool(getattr(tb, "is_ets", False)):
        extra.append(ETS_WARNING)
    return _spectrum(wf.volts, wf.dt, extra_warnings=extra, **kw)
