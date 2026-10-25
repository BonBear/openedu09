"""Review edge cases for worker-level abort (simulator only, never the real device).

* an abort with nothing running (or of a finished op) never leaks into a later exclusive op;
* an abort_event already set aborts the op before ``fn`` runs;
* the calibration's thread-local default abort event is visible only on the calibrating
  thread and is restored afterwards;
* every aborted op leaves the device stopped and the scope usable.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from edu09 import units
from edu09.device import calibration as calmod
from edu09.device.scope import Scope
from edu09.device.worker import DeviceIO
from edu09.errors import JobAborted
from edu09.transport.sim import SimTransport

WAIT_S = 10.0


def test_abort_with_nothing_running_does_not_leak(sim_scope: Scope) -> None:
    assert sim_scope.abort_exclusive() is False
    assert sim_scope.run_exclusive(lambda io: io.read(0.001) or 42, timeout_s=WAIT_S) == 42


def test_abort_flag_cleared_after_aborted_op(sim_scope: Scope) -> None:
    def fn(io: DeviceIO) -> None:
        sim_scope.abort_exclusive()  # (on the worker thread: non-blocking)
        io.read(0.001)

    with pytest.raises(JobAborted):
        sim_scope.run_exclusive(fn, timeout_s=WAIT_S)
    # the next op is not affected (the per-op flag was reset)
    assert sim_scope.run_exclusive(lambda io: (io.drain(0.001), 7)[1], timeout_s=WAIT_S) == 7
    assert sim_scope.acquire(trigger_mode="none").raw.size > 0


def test_preset_abort_event_never_runs_fn(sim_scope: Scope) -> None:
    ev = threading.Event()
    ev.set()
    ran: list[bool] = []
    with pytest.raises(JobAborted):
        sim_scope.run_exclusive(lambda io: ran.append(True), timeout_s=WAIT_S, abort_event=ev)
    assert ran == []
    assert sim_scope.run_exclusive(lambda io: 1, timeout_s=WAIT_S) == 1


def test_fn_swallowing_abort_cannot_do_more_io(sim_scope: Scope) -> None:
    """Even if ``fn`` catches JobAborted, each later DeviceIO call raises again."""
    hits: list[str] = []

    def fn(io: DeviceIO) -> str:
        sim_scope.abort_exclusive()
        for name in ("read", "write", "drain"):
            try:
                if name == "read":
                    io.read(0.001)
                elif name == "write":
                    io.write(bytes(64))
                else:
                    io.drain(0.001)
            except JobAborted:
                hits.append(name)
        return "swallowed"

    assert sim_scope.run_exclusive(fn, timeout_s=WAIT_S) == "swallowed"
    assert hits == ["read", "write", "drain"]
    assert _sim(sim_scope).device.state != "armed"


def _sim(scope: Scope) -> SimTransport:
    t = scope._transport
    assert isinstance(t, SimTransport)
    return t


def test_calibration_default_abort_event_is_thread_local(
    sim_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    seen: dict[str, Any] = {}
    real_exclusive = sim_scope._worker.exclusive

    def spy(fn: Any, abort_event: threading.Event | None = None) -> Any:
        seen.setdefault(threading.current_thread().name, []).append(abort_event)
        return real_exclusive(fn, abort_event)

    monkeypatch.setattr(sim_scope._worker, "exclusive", spy)

    def measure(scope: Scope) -> units.Calibration:
        scope.run_exclusive(lambda io: None, timeout_s=WAIT_S)  # this thread: calibration's event
        t = threading.Thread(
            target=lambda: scope.run_exclusive(lambda io: None, timeout_s=WAIT_S), name="other"
        )
        t.start()
        t.join(WAIT_S)
        return units.Calibration.typical()

    monkeypatch.setattr(calmod, "measure_calibration", measure)
    ev = threading.Event()
    sim_scope.calibrate(
        confirm=True, input_disconnected=True, save=False, path=tmp_path / "c", abort_event=ev
    )
    me = threading.current_thread().name
    assert seen[me] == [ev]
    assert seen["other"] == [None]
    assert getattr(sim_scope._job_local, "abort_event", None) is None  # restored
    sim_scope.run_exclusive(lambda io: None, timeout_s=WAIT_S)
    assert seen[me] == [ev, None]


def test_abort_between_calibration_steps_aborts_next_step(
    sim_scope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Scope.abort_exclusive while no op is running still ends a multi-step calibration."""
    steps: list[int] = []

    def measure(scope: Scope) -> units.Calibration:
        scope.run_exclusive(lambda io: steps.append(1), timeout_s=WAIT_S)
        assert scope.abort_exclusive() is True  # calibration in progress (between ops)
        scope.run_exclusive(lambda io: steps.append(2), timeout_s=WAIT_S)
        return units.Calibration.typical()

    monkeypatch.setattr(calmod, "measure_calibration", measure)
    with pytest.raises(JobAborted):
        sim_scope.calibrate(
            confirm=True, input_disconnected=True, save=True, path=tmp_path / "c.json"
        )
    assert steps == [1]
    assert not (tmp_path / "c.json").exists()
    assert sim_scope.calibration.source != "measured"
