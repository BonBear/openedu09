"""Hardware: triggering and measurements with a known signal on CH1 (opt-in).

Set ``EDU09_HW_SIGNAL`` to describe it, ``KIND:FREQ:LO..HI``, e.g. ``square:30khz:0..4.16v``
(FREQ in hz/khz/mhz; LO/HI in volts at the probe tip, DC coupled, fast edges).
"""

from __future__ import annotations

import time

import pytest
from hw_helpers import (
    SignalSpec,
    assert_clean,
    crossing_near,
    fit_signal,
    signal_from_env,
    timebases_for,
)

from edu09 import analysis, units
from edu09.device.scope import Scope
from edu09.device.worker import ETS_TRIGGER_SEARCH
from edu09.errors import AcquisitionTimeout
from edu09.model import Timebase

pytestmark = pytest.mark.hardware

TIMEBASES = timebases_for(signal_from_env())
EDGE_CHECK_MIN_SAMPLES_PER_PERIOD = 11


_SIGNAL_SEEN: dict[str, float] = {}


@pytest.fixture(autouse=True)
def _signal_present(scope: Scope, signal: SignalSpec) -> None:
    """Fail every signal test with one clear message when CH1 shows no swing (source unpowered,
    probe not connected, x10 probe...), instead of 17 trigger timeouts."""
    if "ok" not in _SIGNAL_SEEN:
        vd, pos = fit_signal(signal)
        scope.configure(
            volts_per_div=vd,
            position_div=pos,
            coupling="dc",
            timebase="200us",
            trigger_mode="none",
        )
        wf = scope.acquire(timeout_s=3.0)
        vpp = float(wf.volts.max() - wf.volts.min())
        _SIGNAL_SEEN["ok"] = float(vpp >= 0.5 * signal.vpp)
        _SIGNAL_SEEN["vpp"] = vpp
    if not _SIGNAL_SEEN["ok"]:
        pytest.fail(
            f"no signal on CH1: measured {_SIGNAL_SEEN['vpp']:.2f} Vpp, expected about "
            f"{signal.vpp:.2f} Vpp from EDU09_HW_SIGNAL - is the source powered and the probe "
            "connected (x1)?",
            pytrace=False,
        )


def _setup(scope: Scope, sig: SignalSpec, tb: Timebase, edge: str, mode: str = "normal") -> None:
    vd, pos = fit_signal(sig)
    scope.configure(
        volts_per_div=vd,
        position_div=pos,
        coupling="dc",
        timebase=tb,
        trigger_mode=mode,
        trigger_edge=edge,
        trigger_level_v=sig.mid_v,
    )


def _first_realtime() -> Timebase | None:
    return next((t for t in TIMEBASES if not t.is_ets), None)


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("tb", TIMEBASES, ids=lambda t: t.label)
def test_normal_trigger_edge_position(
    scope: Scope, signal: SignalSpec, tb: Timebase, edge: str
) -> None:
    _setup(scope, signal, tb, edge)
    table = units.trigger_index(tb)
    # With few samples per period an edge lies within +-2 of ANY index, so the position check
    # would pass even for an untriggered frame: only check it with >= 2 * 5 + 1 samples/period.
    per_period = 1.0 / (signal.frequency_hz * units.sample_interval(tb))
    for _ in range(3):
        wf = scope.acquire(timeout_s=2.0)
        assert_clean(wf.raw, f"{edge} {tb.label}")
        assert wf.triggered
        if per_period < EDGE_CHECK_MIN_SAMPLES_PER_PERIOD:
            continue
        # ETS: the worker detects the edge per frame (the position varies by a few samples
        # between sessions, protocol.md 11.4); realtime frames carry the table index.
        if tb.is_ets:
            assert wf.trigger_index is not None
            assert abs(wf.trigger_index - table) <= ETS_TRIGGER_SEARCH, (
                f"detected {wf.trigger_index}, table {table}"
            )
            expected, tol = wf.trigger_index, 1
        else:
            assert wf.trigger_index == table
            expected, tol = table, 2
        got = crossing_near(wf.volts, signal.mid_v, edge == "rising", expected)
        assert got is not None, "no edge in the frame"
        assert abs(got - expected) <= tol, f"{edge} edge at {got}, expected {expected} +-{tol}"


@pytest.mark.parametrize("tb", TIMEBASES, ids=lambda t: t.label)
def test_frequency_within_3_percent(scope: Scope, signal: SignalSpec, tb: Timebase) -> None:
    _setup(scope, signal, tb, "rising")
    wf = scope.acquire(timeout_s=2.0)
    volts = analysis.despike(wf.volts) if tb.is_ets else wf.volts  # ETS has outlier samples
    f = analysis.measure_one(volts, wf.dt, "frequency")
    assert f.valid and f.value is not None, f"frequency not measurable: {f.note}"
    assert f.value == pytest.approx(signal.frequency_hz, rel=0.03)


def test_levels_are_plausible(scope: Scope, signal: SignalSpec) -> None:
    """Loose amplitude check only: the vertical gain is not calibrated (about +-10 % is known)."""
    tb = _first_realtime()
    if tb is None:
        pytest.skip("no realtime timebase suits this signal frequency")
    _setup(scope, signal, tb, "rising")
    wf = scope.acquire(timeout_s=2.0)
    vpp = analysis.measure_one(wf.volts, wf.dt, "vpp").value
    assert vpp is not None
    assert 0.8 * signal.vpp <= vpp <= 1.25 * signal.vpp, f"Vpp {vpp:.2f} V vs {signal.vpp:.2f} V"


def test_unreachable_trigger_level(scope: Scope, signal: SignalSpec) -> None:
    tb = _first_realtime() or Timebase.US200
    _setup(scope, signal, tb, "rising")
    top = scope.settings.level_range_v()[1] - 0.01
    if top < signal.high_v + 0.3:
        pytest.skip("signal fills the screen: no unreachable trigger level")
    scope.configure(trigger_level_v=top)
    with pytest.raises(AcquisitionTimeout):
        scope.acquire(timeout_s=0.5)
    scope.configure(trigger_mode="auto", trigger_auto_timeout_s=0.1)
    wf = scope.acquire(timeout_s=2.0)
    assert not wf.triggered
    assert_clean(wf.raw, "AUTO fallback")


def test_run_normal_trigger_20_fps_then_stop(scope: Scope, signal: SignalSpec) -> None:
    tb = _first_realtime()
    if tb is None:
        pytest.skip("no realtime timebase suits this signal frequency")
    _setup(scope, signal, tb, "rising")
    scope.run()
    try:
        time.sleep(0.5)
        first = scope.latest()
        assert first is not None
        t0 = time.monotonic()
        time.sleep(2.0)
        last = scope.latest()
        dt = time.monotonic() - t0
    finally:
        scope.stop()
    assert last is not None
    assert (last.seq - first.seq) / dt >= 20
    assert_clean(last.raw, "running frame")
    assert_clean(scope.acquire(timeout_s=2.0).raw, "stop -> acquire")
