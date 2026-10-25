"""Hardware: acquisition without a known signal (input open or anything connected)."""

from __future__ import annotations

import time

import pytest
from hw_helpers import assert_clean

from edu09.device.scope import Scope
from edu09.errors import AcquisitionTimeout
from edu09.model import Timebase

pytestmark = pytest.mark.hardware

# very slow timebases take 0.5..5 s per frame: keep the suite fast
FAST_REALTIME = [tb for tb in Timebase if not tb.is_ets and tb.seconds <= 20e-3]


@pytest.mark.parametrize("tb", FAST_REALTIME, ids=lambda t: t.label)
def test_free_run_frame_is_sane(scope: Scope, tb: Timebase) -> None:
    scope.configure(timebase=tb, trigger_mode="none", volts_per_div=2.0)
    wf = scope.acquire(timeout_s=3.0)
    assert_clean(wf.raw, f"free run {tb.label}")
    assert 2 < float(wf.raw.mean()) < 253, f"mean raw {wf.raw.mean():.1f} looks saturated"
    assert wf.volts.shape == wf.raw.shape


def test_auto_fallback_with_unreachable_level(scope: Scope) -> None:
    """AUTO with a level outside the screen: untriggered but clean frames, promptly."""
    scope.configure(
        timebase="200us",
        volts_per_div=2.0,
        trigger_mode="auto",
        trigger_level_v=7.0,
        trigger_auto_timeout_s=0.1,
    )
    for _ in range(3):
        t0 = time.monotonic()
        wf = scope.acquire(timeout_s=2.0)
        assert time.monotonic() - t0 < 1.5
        assert not wf.triggered
        assert_clean(wf.raw, "AUTO fallback")


def test_normal_with_unreachable_level_times_out(scope: Scope) -> None:
    scope.configure(timebase="200us", volts_per_div=2.0, trigger_mode="normal", trigger_level_v=7.0)
    with pytest.raises(AcquisitionTimeout):
        scope.acquire(timeout_s=0.5)
    # and the scope is still usable afterwards
    scope.configure(trigger_mode="none")
    assert_clean(scope.acquire(timeout_s=2.0).raw, "after timeout")


def test_run_at_least_20_fps_for_2s(scope: Scope) -> None:
    scope.configure(timebase="200us", volts_per_div=2.0, trigger_mode="none")
    scope.run()
    try:
        time.sleep(0.5)  # warm up
        first = scope.latest()
        assert first is not None, "no frame after 0.5 s of running"
        t0 = time.monotonic()
        time.sleep(2.0)
        last = scope.latest()
        dt = time.monotonic() - t0
    finally:
        scope.stop()
    assert last is not None
    fps = (last.seq - first.seq) / dt
    assert fps >= 20, f"only {fps:.1f} frames/s"


def test_stop_then_acquire_is_clean(scope: Scope) -> None:
    scope.configure(timebase="1ms", volts_per_div=2.0, trigger_mode="none")
    scope.run()
    time.sleep(0.3)
    scope.stop()
    assert not scope.running
    for _ in range(3):
        assert_clean(scope.acquire(timeout_s=2.0).raw, "acquire after stop")
