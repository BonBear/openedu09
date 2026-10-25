"""Worker-level abort of long exclusive operations (calibration, live captures).

``Scope.close`` / ``Scope.abort_exclusive`` / an ``abort_event`` end the running exclusive
operation at its next device call with ``JobAborted``; the worker stops and resets the device;
an aborted calibration never saves. Simulator only (never the real device).
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from edu09 import units
from edu09.device import calibration as calmod
from edu09.device.scope import Scope
from edu09.device.worker import DeviceIO
from edu09.errors import Cancelled, JobAborted
from edu09.transport.sim import SimTransport

OPEN_INPUT = "dc:0v:noise=40mv"
WAIT_S = 10.0


def _sim(scope: Scope) -> SimTransport:
    t = scope._transport
    assert isinstance(t, SimTransport)
    return t


@pytest.fixture
def rt_scope(tmp_path: Path) -> Iterator[Scope]:
    """Real-time simulator with an open input: a real calibration takes seconds."""
    scope = Scope.open(SimTransport(OPEN_INPUT, seed=0), reconnect_interval_s=0.01)
    scope._calibration_path = tmp_path / "cal.json"
    try:
        yield scope
    finally:
        scope.close()


def _spy_measure(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, list[Any]]:
    """Wrap the T9 procedure: ``entered`` is set when it starts on the worker thread."""
    entered = threading.Event()
    outcome: list[Any] = []
    orig = calmod._measure

    def spy(io: DeviceIO) -> Any:
        entered.set()
        try:
            res = orig(io)
        except BaseException as exc:
            outcome.append(exc)
            raise
        outcome.append(res)
        return res

    monkeypatch.setattr(calmod, "_measure", spy)
    return entered, outcome


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as exc:
            out.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def test_close_during_calibration_returns_fast_and_stops_device(
    rt_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, outcome = _spy_measure(monkeypatch)
    rt_scope.configure(timebase="200us")
    rt_scope.run()
    t, res = _in_thread(
        lambda: rt_scope.calibrate(confirm=True, input_disconnected=True, save=True)
    )
    assert entered.wait(WAIT_S)
    t0 = time.monotonic()
    rt_scope.close()
    assert time.monotonic() - t0 < 2.0
    assert not rt_scope.worker.thread.is_alive()
    sim = _sim(rt_scope)
    assert not sim.is_open
    assert sim.device.state != "armed"
    t.join(WAIT_S)
    assert isinstance(outcome[0], JobAborted)
    assert isinstance(res[0], Cancelled)  # JobAborted (or Cancelled if close won the race)
    assert not (tmp_path / "cal.json").exists()  # an aborted calibration never saves
    assert rt_scope.owner_lock is None or not rt_scope.owner_lock.held


def test_abort_exclusive_aborts_calibration_and_scope_keeps_working(
    rt_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, _outcome = _spy_measure(monkeypatch)
    rt_scope.configure(timebase="200us")
    rt_scope.run()
    t, res = _in_thread(
        lambda: rt_scope.calibrate(confirm=True, input_disconnected=True, save=True)
    )
    assert entered.wait(WAIT_S)
    t0 = time.monotonic()
    assert rt_scope.abort_exclusive() is True
    t.join(WAIT_S)
    assert time.monotonic() - t0 < 2.0
    assert isinstance(res[0], JobAborted)
    assert not (tmp_path / "cal.json").exists()
    assert rt_scope.calibration.source != "measured"
    # the device was resynced; the paused continuous acquisition resumes, singles work
    assert rt_scope.running
    seq = rt_scope.status.last_seq
    deadline = time.monotonic() + WAIT_S
    while rt_scope.status.last_seq <= seq and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rt_scope.status.last_seq > seq
    rt_scope.stop()
    assert rt_scope.acquire(trigger_mode="none").raw.size > 0
    assert rt_scope.abort_exclusive() is False  # nothing running any more


def test_abort_event_aborts_calibration(
    rt_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, _outcome = _spy_measure(monkeypatch)
    ev = threading.Event()
    t, res = _in_thread(
        lambda: rt_scope.calibrate(confirm=True, input_disconnected=True, save=True, abort_event=ev)
    )
    assert entered.wait(WAIT_S)
    ev.set()
    t.join(WAIT_S)
    assert isinstance(res[0], JobAborted)
    assert not (tmp_path / "cal.json").exists()
    with pytest.raises(JobAborted):  # already set: does not even start
        rt_scope.calibrate(confirm=True, input_disconnected=True, save=False, abort_event=ev)


def test_abort_after_measurement_never_saves(
    sim_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An abort that arrives after the procedure returned but before the save: no save."""

    def measure_calibration(scope: Scope) -> units.Calibration:
        assert scope.abort_exclusive() is True  # the calibration's abort event is registered
        return units.Calibration(source="measured")

    mod = types.ModuleType("edu09.device.calibration")
    mod.measure_calibration = measure_calibration  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edu09.device.calibration", mod)
    with pytest.raises(JobAborted):
        sim_scope.calibrate(confirm=True, input_disconnected=True, path=tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()
    assert sim_scope.calibration.source != "measured"


def test_run_exclusive_abort_mid_read_loop_and_after(sim_scope: Scope) -> None:
    """A transport read in progress raises once; every later DeviceIO call raises too."""
    started = threading.Event()
    seen: list[str] = []

    def fn(io: DeviceIO) -> None:
        started.set()
        try:
            while True:  # bounded by the abort (the test fails on the outer timeout)
                io.read(0.01)
        except JobAborted:
            seen.append("read")
        assert io.aborted
        try:
            io.drain()
        except JobAborted:
            seen.append("drain")
        io.live_samples(16)  # raises again

    t, res = _in_thread(lambda: sim_scope.run_exclusive(fn, timeout_s=WAIT_S))
    assert started.wait(WAIT_S)
    assert sim_scope.abort_exclusive() is True
    t.join(WAIT_S)
    assert isinstance(res[0], JobAborted)
    assert seen == ["read", "drain"]
    assert sim_scope.acquire(trigger_mode="none").raw.size > 0  # scope still usable


def test_abort_during_free_run_capture_stops_device(sim_scope: Scope) -> None:
    """Abort while a live capture is armed: the capture's own cleanup stops the device."""
    sim = _sim(sim_scope)
    armed = threading.Event()
    real_write = sim.write

    def write(payload: bytes) -> None:
        real_write(payload)
        if sim.device.state == "armed":
            armed.set()
            sim_scope.abort_exclusive()  # (worker thread; non-blocking)

    sim.write = write  # type: ignore[method-assign]
    sim_scope.configure(timebase="50ms")  # a slow frame: the abort hits while armed
    with pytest.raises(JobAborted):
        sim_scope.run_exclusive(lambda io: io.live_samples(1024, timebase="50ms"))
    assert armed.is_set()
    assert sim.device.state != "armed"
    sim.write = real_write  # type: ignore[method-assign]
    sim_scope.configure(timebase="200us")
    assert sim_scope.acquire(trigger_mode="none").raw.size > 0


def test_close_aborts_queued_and_running_live_level(rt_scope: Scope) -> None:
    started = threading.Event()

    def long_fn(io: DeviceIO) -> None:
        started.set()
        while True:
            io.live_samples(1024, timebase="2ms")

    t, res = _in_thread(lambda: rt_scope.run_exclusive(long_fn, timeout_s=WAIT_S))
    assert started.wait(WAIT_S)
    t0 = time.monotonic()
    rt_scope.close()
    assert time.monotonic() - t0 < 2.0
    t.join(WAIT_S)
    assert isinstance(res[0], Cancelled)
    assert _sim(rt_scope).device.state != "armed"
