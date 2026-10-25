"""analysis.spectrum / spectrum_average / spectrum_waveform (docs/specs/spectrum.md)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from edu09 import analysis
from edu09.model import Settings, Timebase
from edu09.protocol import FrameAssembler, deinterleave, tables
from edu09.transport.replay import load_records

H1 = Path(__file__).resolve().parents[1] / "fixtures" / "h1"
N = 1024
FS = 156_250.0  # 200 us/div realtime
DT = 1.0 / FS
DF = FS / N


def _tone(bins: float, amp: float = 1.0, phase: float = 0.3, n: int = N) -> npt.NDArray[np.float64]:
    k = np.arange(n)
    return amp * np.sin(2 * math.pi * bins * k / n + phase)


def _frames(name: str, mode: int) -> list[npt.NDArray[np.float64]]:
    frames: list[npt.NDArray[np.float64]] = []
    asm: FrameAssembler | None = None
    for r in load_records(H1 / name):
        if r.dir == "out" and r.data[0] == tables.OP_READOUT:
            asm = FrameAssembler(expected_mode=mode)
        elif r.dir == "in" and asm is not None and asm.feed(r.data):
            raw = np.frombuffer(deinterleave(mode, asm.frame), dtype=np.uint8)
            # 2 V/div, ~32 counts/div, ground ~132 counts (H1 settings)
            frames.append((raw.astype(np.float64) - 132.0) * 2.0 / 32.0)
            asm = None
    return frames


# --------------------------------------------------------------------------- shape / scaling


def test_shape_and_metadata() -> None:
    s = analysis.spectrum(_tone(100), DT)
    assert s.n == N
    assert s.freqs_hz.shape == s.vrms.shape == s.dbv.shape == (N // 2 + 1,)
    assert s.freqs_hz[0] == 0.0
    assert s.freqs_hz[-1] == pytest.approx(FS / 2)
    assert s.resolution_hz == pytest.approx(DF)
    assert s.fs_hz == pytest.approx(FS)
    assert s.window == "hann"
    assert s.averages == 1
    assert s.enbw_bins == pytest.approx(1.5)
    assert np.allclose(s.dbv, 20 * np.log10(np.maximum(s.vrms, 1e-12)))


@pytest.mark.parametrize(
    ("window", "enbw"),
    [("hann", 1.5), ("hamming", 1.3628), ("blackman", 1.7268), ("flattop", 3.7702), ("rect", 1.0)],
)
def test_enbw_per_window(window: str, enbw: float) -> None:
    assert analysis.spectrum(_tone(100), DT, window=window).enbw_bins == pytest.approx(enbw, 1e-3)


@pytest.mark.parametrize("window", analysis.WINDOWS)
def test_on_bin_sine_reads_amplitude_over_sqrt2(window: str) -> None:
    a = 1.7
    s = analysis.spectrum(_tone(100, a), DT, window=window)
    assert s.vrms[100] == pytest.approx(a / math.sqrt(2), rel=1e-3)
    assert s.fundamental is not None
    assert s.fundamental.bin == 100
    assert s.fundamental.vrms == pytest.approx(a / math.sqrt(2), rel=1e-3)
    assert s.fundamental.freq_hz == pytest.approx(100 * DF, abs=0.01 * DF)
    assert s.fundamental.dbv == pytest.approx(20 * math.log10(a / math.sqrt(2)), abs=0.01)


OFFSETS = np.linspace(0.0, 0.5, 11)


def _worst(window: str) -> tuple[float, float, float]:
    """(worst bin error, worst peak error, worst |freq error| in bins) over sub-bin offsets."""
    a = 1.0
    ref = a / math.sqrt(2)
    worst_bin = worst_pk = worst_f = 0.0
    for off in OFFSETS:
        s = analysis.spectrum(_tone(150 + off, a), DT, window=window)
        k = int(round(150 + off))
        assert s.fundamental is not None
        worst_bin = max(worst_bin, abs(s.vrms[k] / ref - 1))
        worst_pk = max(worst_pk, abs(s.fundamental.vrms / ref - 1))
        worst_f = max(worst_f, abs(s.fundamental.freq_hz / DF - (150 + off)))
    return worst_bin, worst_pk, worst_f


def test_flattop_amplitude_accuracy() -> None:
    worst_bin, worst_pk, worst_f = _worst("flattop")
    assert worst_bin < 0.0012  # spec: ~0.1 % scalloping
    assert worst_pk < 0.001
    assert worst_f < 0.16


def test_hann_scalloping_and_correction() -> None:
    worst_bin, worst_pk, worst_f = _worst("hann")
    assert 0.14 < worst_bin < 0.152  # 1.42 dB worst case scalloping at half a bin
    assert worst_pk < 0.01
    assert worst_f < 0.02


@pytest.mark.parametrize(("window", "tol"), [("blackman", 0.003), ("hamming", 0.01)])
def test_other_windows_peak_accuracy(window: str, tol: float) -> None:
    _, worst_pk, worst_f = _worst(window)
    assert worst_pk < tol
    assert worst_f < 0.02


def test_rect_documented_accuracy() -> None:
    worst_bin, worst_pk, worst_f = _worst("rect")
    assert worst_bin < 0.37
    assert worst_pk < 0.17
    assert worst_f < 0.2


def test_dc_removed_and_reported() -> None:
    x = _tone(100) + 2.5
    s = analysis.spectrum(x, DT)
    assert s.dc_v == pytest.approx(2.5, abs=1e-9)
    assert s.vrms[0] == pytest.approx(0.0, abs=1e-9)
    kept = analysis.spectrum(x, DT, remove_dc=False)
    assert kept.dc_v == pytest.approx(2.5, abs=1e-9)
    assert kept.vrms[0] == pytest.approx(2.5, rel=1e-6)  # DC bin without sqrt(2)
    assert kept.fundamental is not None and kept.fundamental.bin == 100  # DC never a peak
    for window in analysis.WINDOWS:  # nor its leakage into the first bins
        sp = analysis.spectrum(_tone(20) + 5.0, DT, window=window, remove_dc=False)
        assert sp.peaks[0].bin == 20, window


def test_nyquist_bin_without_sqrt2() -> None:
    x = 0.8 * np.cos(math.pi * np.arange(N))  # +-0.8 alternating: rms 0.8
    s = analysis.spectrum(x, DT, window="rect")
    assert s.vrms[-1] == pytest.approx(0.8, rel=1e-9)
    assert s.peaks and s.peaks[0].bin == N // 2
    assert s.peaks[0].freq_hz == pytest.approx(FS / 2)


def test_odd_length() -> None:
    s = analysis.spectrum(_tone(40, n=1001), DT)
    assert s.n == 1001
    assert s.vrms.size == 501
    assert s.fundamental is not None
    assert s.fundamental.freq_hz == pytest.approx(40 * FS / 1001, rel=1e-3)


def test_parseval_like_noise_floor() -> None:
    """White noise of rms sigma: mean bin power = sigma^2 * enbw * 2 / n (single sided)."""
    rng = np.random.default_rng(1)
    sigma = 0.1
    frames = [rng.normal(0, sigma, N) for _ in range(64)]
    s = analysis.spectrum_average(frames, DT, window="hann")
    expect = sigma**2 * s.enbw_bins * 2 / N
    assert float(np.mean(s.vrms[1:-1] ** 2)) == pytest.approx(expect, rel=0.05)


# --------------------------------------------------------------------------- peaks


def test_peaks_order_limit_and_threshold() -> None:
    x = _tone(100, 1.0) + _tone(200, 0.1, 1.0) + _tone(300, 0.01, 2.0) + _tone(400, 1e-4, 0.5)
    s = analysis.spectrum(x, DT, window="blackman")
    assert [p.bin for p in s.peaks[:3]] == [100, 200, 300]
    dbs = [p.dbv for p in s.peaks]
    assert dbs == sorted(dbs, reverse=True)
    assert all(p.bin != 400 for p in s.peaks)  # -80 dB below the strongest
    deep = analysis.spectrum(x, DT, window="blackman", min_peak_db=-100)
    assert 400 in [p.bin for p in deep.peaks]
    top2 = analysis.spectrum(x, DT, window="blackman", max_peaks=2)
    assert [p.bin for p in top2.peaks] == [100, 200]
    none = analysis.spectrum(x, DT, max_peaks=0)
    assert none.peaks == ()
    assert none.fundamental is not None and none.fundamental.bin == 100


def test_close_peaks_merged() -> None:
    near = analysis.spectrum(_tone(100) + _tone(102, 0.5, 1.1), DT, window="flattop")
    assert len([p for p in near.peaks if 95 <= p.bin <= 107]) == 1  # 2 < enbw+2 bins
    apart = analysis.spectrum(_tone(100) + _tone(110, 0.5, 1.1), DT, window="hann")
    assert {100, 110} <= {p.bin for p in apart.peaks}


def test_silence_and_noise_have_no_fundamental() -> None:
    s = analysis.spectrum(np.zeros(N), DT)
    assert s.peaks == ()
    assert s.fundamental is None and s.thd_pct is None
    rng = np.random.default_rng(3)
    noise = analysis.spectrum(rng.normal(0, 1, N), DT)
    assert noise.fundamental is None


# --------------------------------------------------------------------------- THD


def test_thd_of_sine_with_harmonics() -> None:
    x = _tone(50, 1.0) + _tone(100, 0.1, 0.7) + _tone(150, 0.05, 1.3)
    s = analysis.spectrum(x, DT, window="flattop")
    assert s.thd_pct == pytest.approx(100 * math.hypot(0.1, 0.05), rel=0.01)
    clean = analysis.spectrum(_tone(50.37), DT, window="blackman")
    assert clean.thd_pct is not None and clean.thd_pct < 0.1


def test_thd_of_ideal_square_matches_fourier_series() -> None:
    """50 % square, harmonics up to 9 below fs/2: odd terms 1/k."""
    k = np.arange(N)
    x = np.where(np.sin(2 * math.pi * 16 * k / N + 0.01) >= 0, 1.0, -1.0)
    s = analysis.spectrum(x, DT, window="flattop")
    expect = 100 * math.sqrt(sum((1 / h) ** 2 for h in (3, 5, 7, 9)))
    assert s.thd_pct == pytest.approx(expect, rel=0.03)
    assert s.fundamental is not None
    assert s.fundamental.vrms == pytest.approx(4 / math.pi / math.sqrt(2), rel=0.01)


def test_thd_none_when_fundamental_low_or_no_harmonic() -> None:
    assert analysis.spectrum(_tone(5), DT, window="rect").thd_pct is None  # below bin 6
    high = analysis.spectrum(_tone(300), DT)  # 2*f1 above fs/2
    assert high.fundamental is not None and high.thd_pct is None


# --------------------------------------------------------------------------- warnings


def test_aliasing_warning_below_400k_sample_rate() -> None:
    s = analysis.spectrum(_tone(100), DT)
    assert len(s.warnings) == 1
    assert s.warnings[0] == (
        "aliasing: components above fs/2 = 78.12 kHz fold back (input bandwidth ~200 kHz)"
    )
    fast = analysis.spectrum(_tone(100), 1 / 1_562_500)
    assert fast.warnings == ()
    assert analysis.spectrum(_tone(100), 1 / 500e3).warnings == ()


def test_too_few_periods_warning() -> None:
    s = analysis.spectrum(_tone(2.4), 1 / 1e6, window="rect")
    assert s.fundamental is not None
    assert any(w.startswith("too few periods") for w in s.warnings)
    ok = analysis.spectrum(_tone(20), 1 / 1e6)
    assert not any(w.startswith("too few periods") for w in ok.warnings)


# --------------------------------------------------------------------------- averaging


def test_power_average_and_peak_hold() -> None:
    a = _tone(100, 1.0)
    b = _tone(100, 0.5)
    p = analysis.spectrum_average([a, b], DT, window="flattop")
    assert p.averages == 2
    assert p.vrms[100] == pytest.approx(math.sqrt((0.5 + 0.125) / 2), rel=2e-3)
    h = analysis.spectrum_average([b, _tone(200, 0.3)], DT, mode="peak_hold", window="flattop")
    assert h.vrms[100] == pytest.approx(0.5 / math.sqrt(2), rel=2e-3)
    assert h.vrms[200] == pytest.approx(0.3 / math.sqrt(2), rel=2e-3)
    assert h.fundamental is not None and h.fundamental.bin == 100
    one = analysis.spectrum_average([a], DT)
    single = analysis.spectrum(a, DT)
    assert np.allclose(one.vrms, single.vrms)
    assert one.peaks == single.peaks


def test_average_dc_and_kwargs() -> None:
    s = analysis.spectrum_average(
        [_tone(100) + 1.0, _tone(100) + 3.0], DT, max_peaks=1, min_peak_db=-20, remove_dc=True
    )
    assert s.dc_v == pytest.approx(2.0)
    assert len(s.peaks) == 1


def test_average_errors() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        analysis.spectrum_average([], DT)
    with pytest.raises(ValueError, match="equal length"):
        analysis.spectrum_average([np.zeros(64), np.zeros(32)], DT)
    with pytest.raises(ValueError, match="mode"):
        analysis.spectrum_average([np.zeros(64)], DT, mode="max")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        analysis.spectrum_average([np.zeros(64)], DT, bogus=1)


# --------------------------------------------------------------------------- input validation


def test_input_errors() -> None:
    with pytest.raises(ValueError, match="unknown window"):
        analysis.spectrum(_tone(10), DT, window="kaiser")
    with pytest.raises(ValueError, match="at least 8"):
        analysis.spectrum(np.zeros(7), DT)
    with pytest.raises(ValueError, match="finite"):
        analysis.spectrum(np.array([0.0] * 9 + [np.nan]), DT)
    with pytest.raises(ValueError, match="dt"):
        analysis.spectrum(_tone(10), 0.0)
    with pytest.raises(ValueError, match="1-D"):
        analysis.spectrum(np.zeros((4, 4)), DT)
    with pytest.raises(ValueError, match="max_peaks"):
        analysis.spectrum(_tone(10), DT, max_peaks=-1)
    assert analysis.spectrum(_tone(10), DT, window=" Hann ").window == "hann"


# --------------------------------------------------------------------------- spectrum_waveform


@dataclass
class _FakeWaveform:
    volts: npt.NDArray[np.float64]
    dt: float
    clipped: bool = False
    settings: Settings = field(default_factory=Settings)


def test_spectrum_waveform_warnings() -> None:
    wf = _FakeWaveform(_tone(100), DT)
    s = analysis.spectrum_waveform(wf, window="flattop")
    assert s.window == "flattop"
    assert analysis.CLIPPED_WARNING not in s.warnings
    clipped = analysis.spectrum_waveform(_FakeWaveform(_tone(100), DT, clipped=True))
    assert analysis.CLIPPED_WARNING in clipped.warnings
    assert analysis.CLIPPED_WARNING.startswith("clipped")
    ets = analysis.spectrum_waveform(
        _FakeWaveform(_tone(100), 1 / 625e3, settings=Settings(timebase=Timebase.US50))
    )
    assert ets.warnings == (analysis.ETS_WARNING,)
    with pytest.raises(TypeError):
        analysis.spectrum_waveform(wf, mode="power")


def test_spectrum_waveform_minimal_object() -> None:
    @dataclass
    class Bare:
        volts: npt.NDArray[np.float64]
        dt: float

    s = analysis.spectrum_waveform(Bare(_tone(100), DT))
    assert s.fundamental is not None and s.fundamental.bin == 100


# --------------------------------------------------------------------------- H1 hardware frames


def test_hw_realtime_200us_square_with_aliased_harmonics() -> None:
    """555 square (~30 kHz, ~58 % duty) at 156.25 kHz: harmonics 3..7 fold back below fs/2."""
    frames = _frames("square_trig_200us_rise.jsonl", 1)
    assert frames
    for x in frames:
        s = analysis.spectrum(x, DT, window="flattop")
        assert s.fundamental is not None
        f1 = s.fundamental.freq_hz
        assert f1 == pytest.approx(30_000, rel=0.005)
        assert s.dc_v == pytest.approx(2.58, abs=0.05)
        # fundamental of a 0..4.25 V square: 2*A/pi*sin(pi*D)/sqrt(2)
        assert s.fundamental.vrms == pytest.approx(1.65, rel=0.05)
        found = [p.freq_hz for p in s.peaks]
        aliases = [2 * f1, FS - 3 * f1, FS - 4 * f1, FS - 5 * f1]  # 60, 66, 36, 6 kHz
        for fa in aliases:
            assert min(abs(f - fa) for f in found) < 2 * DF, fa
        assert s.thd_pct is not None and 30 < s.thd_pct < 50  # only h2 is below fs/2
        assert s.warnings[0].startswith("aliasing")


@pytest.mark.parametrize(
    ("name", "mode", "rate"),
    [
        ("square_trig_50us_rise.jsonl", 2, 625_000.0),
        ("square_trig_20us_rise.jsonl", 3, 1_562_500.0),
    ],
)
def test_hw_ets_square_harmonic_series(name: str, mode: int, rate: float) -> None:
    frames = [analysis.despike(x, 5 * 2 / 32) for x in _frames(name, mode)]
    s = analysis.spectrum_average(frames, 1 / rate, window="hann")
    assert s.warnings == ()
    assert s.fundamental is not None
    f1 = s.fundamental.freq_hz
    assert f1 == pytest.approx(29_900, rel=0.005)
    ratios = {round(p.freq_hz / f1): p.vrms / s.fundamental.vrms for p in s.peaks}
    # ~62 % duty: |sin(pi k D)| / k -> 2nd and 4th strong, 3rd and 5th weaker
    assert set(ratios) >= {1, 2, 3, 4, 5, 6, 7}
    assert ratios[2] == pytest.approx(0.37, abs=0.05)
    assert ratios[4] == pytest.approx(0.27, abs=0.04)
    assert ratios[3] < ratios[2]
    assert s.thd_pct is not None and 45 < s.thd_pct < 65


def test_pure_noise_has_no_fundamental_review() -> None:
    rng = np.random.default_rng(0)
    for window in ("hann", "flattop", "rect"):
        for _ in range(100):
            s = analysis.spectrum(rng.normal(0.0, 0.1, 1024), 1e-6, window=window)
            assert s.fundamental is None
            assert s.thd_pct is None
