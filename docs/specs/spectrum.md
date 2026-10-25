# Spectrum analyzer: spec

> **Status: implemented** (historical design document; approved 2026-10-25). Code:
> `src/edu09/analysis.py` (`spectrum`, `spectrum_average`, `spectrum_waveform`),
> `src/edu09/gui/spectrum.py` and the MCP `spectrum` tool; user documentation in
> [docs/usage.md](../usage.md#spectrum-analyzer). Verified on hardware with a 555 square wave
> (levels within 1 % between realtime and ETS timebases).

The work was split into two tasks (S1 analysis and MCP tool, S2 GUI view).

## Goal

FFT spectrum of the acquired 1024-sample frame, like the vendor software's spectrum analyzer
(FFT, 512 lines, about 0-75 kHz at the fastest realtime timebase), available in the GUI and over MCP.

## Hardware constraints

- No anti-alias filter in front of the ADC. Input bandwidth is about 200 kHz (manual), so a signal
  above Nyquist (`fs/2`) folds back. The UI and MCP must warn when `fs/2 < 200 kHz`. That is every
  realtime timebase; the wording should say "components above fs/2 fold back".
- ETS frames (50/20/10 us/div) are equivalent-time samples of a repetitive signal. FFT is valid only
  for repetitive signals; label it so. `analysis.despike` may be applied first (opt-in, ETS only).
- 0.1 ms and 10 us/div are display zooms: same samples as 0.2 ms and 20 us/div. Use the frame's real `dt`.
- 8-bit ADC: the noise floor is about -50 dB below full scale. Clipped frames (`Waveform.clipped`)
  produce false harmonics, so warn.

## Task S1: `src/edu09/analysis.py` (+ MCP tool + docs)

Implemented in `src/edu09/analysis.py` and `src/edu09/mcp_server/tools.py` (tests:
`tests/units/test_spectrum.py`, `tests/mcp/test_mcp_spectrum.py`; docs: `docs/usage.md`).

```python
WINDOWS: Final[tuple[str, ...]] = ("hann", "hamming", "blackman", "flattop", "rect")


@dataclass(frozen=True)
class SpectrumPeak:
    freq_hz: float  # parabolic interpolation on the dB spectrum
    vrms: float  # window-corrected amplitude (coherent gain) in Vrms
    dbv: float  # 20*log10(vrms / 1 V)
    bin: int


@dataclass(frozen=True)
class Spectrum:
    freqs_hz: FloatArray  # length n//2 + 1, 0..fs/2
    vrms: FloatArray  # amplitude per bin in Vrms (single-sided, window coherent-gain corrected)
    dbv: FloatArray  # 20*log10(max(vrms, 1e-12))
    window: str
    fs_hz: float
    resolution_hz: float  # fs/n
    enbw_bins: float  # equivalent noise bandwidth of the window, in bins
    n: int  # samples used
    averages: int
    dc_v: float  # mean removed before FFT (reported separately)
    peaks: tuple[SpectrumPeak, ...]  # strongest first, DC bin excluded, at most max_peaks
    fundamental: SpectrumPeak | None  # strongest non-DC peak, if above the noise floor
    thd_pct: float | None  # from harmonics 2..10 of the fundamental below fs/2, else None
    warnings: tuple[str, ...]


def spectrum(
    samples: ArrayLike,
    dt: float,
    *,
    window: str = "hann",
    remove_dc: bool = True,
    max_peaks: int = 8,
    min_peak_db: float = -60.0,
) -> Spectrum: ...


def spectrum_average(
    frames: Sequence[ArrayLike],
    dt: float,
    *,
    window: str = "hann",
    mode: Literal["power", "peak_hold"] = "power",
    **kw,
) -> Spectrum:
    ...
    # power: average |X|^2 over frames (same dt and length required); peak_hold: max per bin


def spectrum_waveform(wf: SampledSignal, **kw) -> Spectrum: ...
```

Rules:
- Single-sided amplitude: `vrms[k] = sqrt(2) * |X[k]| / (n * cg)` for 0<k<n/2, `vrms[0] = |X[0]|/(n*cg)`
  (DC), Nyquist bin without sqrt(2), where `cg` is the window's coherent gain (sum(w)/n).
  A full-scale sine of amplitude A gives about A/sqrt(2) Vrms at its bin (flat-top within 0.1 %,
  Hann within 15 % worst case scalloping). Test this.
- Peaks are local maxima at least `min_peak_db` relative to the strongest bin. Merge peaks closer than
  `enbw_bins + 2` bins. Use parabolic interpolation in dB for frequency and the flat-top-like amplitude
  estimate. Document accuracy per window.
- THD = sqrt(sum(h_k^2))/h_1 * 100 for harmonics found within +-2 bins of k*f1; None if fewer than
  one harmonic is below fs/2 or the fundamental is below 6 bins.
- Warnings: "aliasing: components above fs/2 = X fold back (input bandwidth ~200 kHz)" if
  `fs/2 < 200e3`; "clipped" passed in by `spectrum_waveform` when `wf.clipped`; "too few periods" if
  f1 < 3 * resolution.
- Pure numpy, no new dependencies, strict mypy (module already under the strict override).

MCP tool `spectrum` (registered next to `measure`):
`spectrum(waveform_id="latest", window="hann", max_points=512 (16..1025), peaks=8 (0..32),
db_floor=-80.0, despike=False)` returns
`{waveform_id, window, fs_hz, resolution_hz, n, dc_v, fundamental:{freq_hz,vrms,dbv}|null, thd_pct,
peaks:[{freq_hz,vrms,dbv}], points:{f0_hz, df_hz, dbv:[...]} (max-decimated in dB so peaks survive;
include i[] if not evenly spaced), warnings}`. 4 significant digits, payload under 32 KB, errors as
`"<ErrorClass>: <hint>"`. `despike` only allowed for ETS frames (otherwise warning and ignored).

## Task S2: GUI spectrum view

Implemented in `src/edu09/gui/spectrum.py` with the integration in `src/edu09/gui/main_window.py`
(tests: `tests/gui/test_gui_spectrum.py`).

- View menu "Spectrum" (Ctrl+F) toggles a spectrum dock/panel under or beside the time plot (both
  visible at once; the time plot stays the main view). pyqtgraph PlotWidget: x = frequency (Hz/kHz
  axis, linear by default, log option), y = dBV (default range -80..+20 dBV, autoscale option).
- Controls in the panel: window (combo from `analysis.WINDOWS`), averaging (off, 4, 8, 16 frames,
  power), peak hold toggle, log-frequency toggle, "Despike ETS" follows the existing View option.
- Peak markers (top 5) with labels "f, dBV"; fundamental and THD shown in a small readout; a crosshair
  cursor readout (frequency, dBV, Vrms) on mouse move.
- Warnings line (aliasing, clipped, repetitive-signal note for ETS).
- Computed from the frames the window already receives, on the GUI thread only if cheap (1024-point FFT
  is cheap). Throttle to the existing redraw rate. Averaging resets on any settings change.

## Out of scope

Waterfall/spectrogram, zero padding beyond n, CH2.
