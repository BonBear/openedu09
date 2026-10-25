"""T9 auto set on the simulator (+ frequency detection on the H1 hardware frames)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import analysis, units
from edu09.device import AutoSetResult, Scope, auto_set
from edu09.device import autoset as asm
from edu09.errors import AcquisitionTimeout
from edu09.model import (
    ChannelSettings,
    Coupling,
    Settings,
    Timebase,
    TriggerMode,
    VoltsPerDiv,
    Waveform,
)
from edu09.protocol import FrameAssembler, deinterleave, tables
from edu09.transport.replay import load_records
from edu09.transport.sim import SimTransport

H1 = Path(__file__).resolve().parents[1] / "fixtures" / "h1"
SQUARE_555 = "square:30khz:4.16vpp:offset=2.08v:duty=63%:noise=20mv"  # the user's 555 (H1)


def _open(signal: str, seed: int = 3) -> Scope:
    return Scope.open(SimTransport.virtual(signal, seed=seed), reconnect_interval_s=0.01)


ScopeFactory = Callable[[str], Scope]


@pytest.fixture
def scope_for() -> Iterator[ScopeFactory]:
    opened: list[Scope] = []

    def make(signal: str) -> Scope:
        sc = _open(signal)
        opened.append(sc)
        return sc

    yield make
    for sc in opened:
        sc.close()


def _frequency(wf: Waveform) -> float:
    x = analysis.despike(wf.volts) if wf.settings.timebase.is_ets else wf.volts
    f = analysis.measure_one(x, wf.dt, "frequency").value
    assert f is not None
    return f


def _check_periodic(res: AutoSetResult, f_hz: float) -> None:
    s, wf = res.settings, res.waveform
    assert wf is not None and wf.settings == s
    assert s.ch1.coupling is Coupling.DC
    assert s.trigger.mode is TriggerMode.AUTO
    assert not wf.clipped
    assert _frequency(wf) == pytest.approx(f_hz, rel=0.02)
    periods = asm.frame_periods(s.timebase, f_hz)
    assert 4 <= periods <= 20, (s.timebase, periods)
    # Vpp < 6 div, signal fully on screen
    vdiv = float(s.ch1.volts_per_div)
    v = wf.volts
    assert (v.max() - v.min()) / vdiv < 6
    lo, hi = s.level_range_v()
    assert lo <= v.min() and v.max() <= hi
    # trigger level = signal mean, inside the swing
    assert v.min() < s.trigger.level_v < v.max()
    assert any("trigger AUTO" in line for line in res.rationale)
    assert any("periods per frame" in line for line in res.rationale)


@pytest.mark.parametrize(
    ("signal", "f_hz", "vdiv", "timebase"),
    [
        ("sine:50hz:2vpp:noise=10mv", 50.0, 0.5, Timebase.MS5),
        ("square:50hz:2vpp", 50.0, 0.5, Timebase.MS5),
        ("sine:1khz:2vpp:noise=10mv", 1e3, 0.5, Timebase.US200),
        ("square:1khz:3vpp", 1e3, 1.0, Timebase.US200),
        ("sine:30khz:2vpp:noise=10mv", 30e3, 0.5, Timebase.US20),
        ("square:30khz:2vpp", 30e3, 0.5, Timebase.US20),
        (SQUARE_555, 30e3, 1.0, Timebase.US20),
        ("sine:1khz:200mvpp:noise=2mv", 1e3, 0.1, Timebase.US200),
        ("sine:200hz:20vpp", 200.0, 5.0, Timebase.MS1),
    ],
)
def test_periodic_signals(
    scope_for: ScopeFactory, signal: str, f_hz: float, vdiv: float, timebase: Timebase
) -> None:
    sc = scope_for(signal)
    res = auto_set(sc)
    assert isinstance(res, AutoSetResult)
    _check_periodic(res, f_hz)
    assert float(res.settings.ch1.volts_per_div) == vdiv
    assert res.settings.timebase is timebase
    assert sc.settings == res.settings  # applied
    if timebase.is_ets:
        assert any("equivalent-time" in line for line in res.rationale)


def test_555_square_is_centred(scope_for: ScopeFactory) -> None:
    res = auto_set(scope_for(SQUARE_555))
    s = res.settings
    # 0..4.16 V at 1 V/div: ground moved down ~2 div so the signal is centred
    assert s.ch1.position_div == pytest.approx(-2.0, abs=0.2)
    assert s.trigger.level_v == pytest.approx(2.08 + 2.08 * (2 * 0.63 - 1), abs=0.15)
    assert any("position -2" in line for line in res.rationale)


def test_dc_only(scope_for: ScopeFactory) -> None:
    sc = scope_for("dc:1.2v:noise=5mv")
    res = auto_set(sc)
    s = res.settings
    assert res.waveform is not None and not res.waveform.clipped
    lo, hi = s.level_range_v()
    assert lo < 1.2 < hi
    assert s.ch1.volts_per_div is VoltsPerDiv.V0_2  # smallest range that still shows 1.2 V
    assert s.trigger.mode is TriggerMode.AUTO
    assert s.trigger.level_v == pytest.approx(1.2, abs=0.02)
    assert not s.timebase.is_ets
    assert any("no periodic signal" in line and "DC level" in line for line in res.rationale)


@pytest.mark.parametrize("signal", ["dc:0v", "dc:0v:noise=5mv"])
def test_no_signal(scope_for: ScopeFactory, signal: str) -> None:
    sc = scope_for(signal)
    sc.configure(timebase="500us")
    res = auto_set(sc)
    s = res.settings
    assert s.ch1.volts_per_div is asm.NO_SIGNAL_VDIV
    assert s.ch1.position_div == 0.0
    assert s.timebase is Timebase.US500  # user's (realtime) timebase kept
    assert s.trigger.mode is TriggerMode.AUTO
    assert any("no signal detected" in line for line in res.rationale)
    assert res.waveform is not None and not res.waveform.triggered  # AUTO fell back


def test_pure_noise_is_not_a_periodic_signal(scope_for: ScopeFactory) -> None:
    res = auto_set(scope_for("noise:300mvrms"))
    assert any("no periodic signal" in line for line in res.rationale)


def test_overrange_signal_keeps_5v(scope_for: ScopeFactory) -> None:
    res = auto_set(scope_for("sine:1khz:60vpp"))
    assert res.settings.ch1.volts_per_div is VoltsPerDiv.V5
    assert any("clipped" in line for line in res.rationale)


def test_mid_frequency_stays_realtime(scope_for: ScopeFactory) -> None:
    # 5 kHz: too fast for 20 periods at 200 us/div but below the ETS threshold (spec: ~10 kHz)
    res = auto_set(scope_for("sine:5khz:1vpp"))
    assert res.settings.timebase is Timebase.US200
    assert any("more than 20" in line for line in res.rationale)


def test_preserves_stopped_state(scope_for: ScopeFactory) -> None:
    sc = scope_for("sine:1khz:2vpp")
    auto_set(sc)
    assert not sc.running
    assert sc.status.state == "idle"


def test_preserves_running_state(scope_for: ScopeFactory) -> None:
    sc = scope_for("sine:1khz:2vpp")
    sc.run()
    res = auto_set(sc)
    assert sc.running
    wf = sc.acquire(timeout_s=2.0)
    assert wf.settings == res.settings
    sc.stop()


def test_restores_settings_on_error(
    scope_for: ScopeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    sc = scope_for("sine:1khz:2vpp")
    sc.configure(volts_per_div=0.2, timebase="5ms", coupling="ac", trigger_mode="normal")
    before = sc.settings
    calls = {"n": 0}
    real = sc.acquire

    def flaky(*a: Any, **k: Any) -> Waveform:
        calls["n"] += 1
        if calls["n"] == 3:
            raise AcquisitionTimeout("injected")
        return real(*a, **k)

    monkeypatch.setattr(sc, "acquire", flaky)
    with pytest.raises(AcquisitionTimeout, match="injected"):
        auto_set(sc)
    assert sc.settings == before
    assert sc.worker.settings == before


def test_timeout_argument_validated(scope_for: ScopeFactory) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        auto_set(scope_for("dc:0v"), timeout_s=0)


def test_ets_failure_falls_back_to_realtime(scope_for: ScopeFactory) -> None:
    sc = scope_for("sine:30khz:2vpp")
    t = sc._transport
    assert isinstance(t, SimTransport)
    t.device.never_trigger = True  # realtime free-run still completes
    res = auto_set(sc, timeout_s=3.0)
    assert res.settings.timebase is Timebase.US200
    assert any("falling back to realtime" in line for line in res.rationale)


# --------------------------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------------------------
def test_pick_timebase() -> None:
    rt = asm._realtime_choices()
    assert Timebase.US100 not in rt and Timebase.US200 in rt
    assert not any(tb.is_ets for tb in rt)
    for f in (5.0, 50.0, 60.0, 333.0, 1e3, 2.5e3):
        tb, p = asm.pick_timebase(f, rt)
        assert 4 <= p <= 20, (f, tb, p)
    tb, p = asm.pick_timebase(30e3, list(asm.ETS_CHOICES))
    assert tb is Timebase.US20 and p == pytest.approx(19.66, abs=0.01)
    tb, p = asm.pick_timebase(12e3, list(asm.ETS_CHOICES))
    assert tb is Timebase.US20 and 4 <= p <= 20


def test_fit_position() -> None:
    assert asm.fit_position(-1.0, 1.0, 0.5) == 0.0
    assert asm.fit_position(-1.0, 1.0, 0.2) is None  # 10 div
    assert asm.fit_position(0.0, 4.16, 1.0) == pytest.approx(-2.1)
    assert asm.fit_position(1.5, 1.5, 0.2) == -4.0  # clamped; 1.5 V still 0.5 div below top
    assert asm.fit_position(1.5, 1.5, 0.1) is None
    assert asm.fit_position(0.0, 0.0, 0.1) == 0.0


def _h1_frames(name: str) -> list[tuple[np.ndarray, tables.TimebaseEntry]]:
    recs = load_records(H1 / name)
    tb = next(e for e in tables.TIMEBASES.values() if e.hw_code == recs[0].data[tables.CFG_IDX_TB])
    out: list[tuple[np.ndarray, tables.TimebaseEntry]] = []
    fa: FrameAssembler | None = None
    for rec in recs:
        if rec.dir == "out":
            if rec.data[0] == tables.OP_READOUT:
                fa = FrameAssembler(expected_mode=tb.mode)
            continue
        if fa is None:  # status reports before 0x0A, trailing report after the frame
            continue
        if fa.feed(rec.data):
            out.append((np.frombuffer(deinterleave(tb.mode, fa.frame), dtype=np.uint8), tb))
            fa = None
    return out


@pytest.mark.parametrize(
    "name",
    [
        "square_freerun_200us_2v.jsonl",
        "square_trig_200us_rise.jsonl",
        "square_trig_50us_rise.jsonl",
        "square_trig_20us_rise.jsonl",
        "square_trig_10us_rise.jsonl",
    ],
)
def test_periodic_frequency_on_hardware_frames(name: str) -> None:
    """The real 555 (~30 kHz): 5 samples/period realtime frames and spiky ETS frames."""
    frames = _h1_frames(name)
    assert frames
    for raw, entry in frames:
        tb = Timebase.parse(entry.s_per_div)
        s = Settings(
            ch1=ChannelSettings(VoltsPerDiv.V2),
            timebase=tb,
        ).replace(trigger_mode="normal")
        volts = units.counts_to_volts(raw, s.ch1, units.Calibration())
        wf = Waveform(1, datetime.now(UTC), raw, volts, entry.dt, None, s, "t", entry.mode)
        f = asm.periodic_frequency(wf, despike=entry.is_ets)
        assert f == pytest.approx(30e3, rel=0.02), name
        # 63 % of 4.16 V = 2.62 V, + 0.31 V: uncalibrated ground (raw ~132 instead of 127)
        assert asm.whole_period_mean(wf) == pytest.approx(2.93, abs=0.15)
