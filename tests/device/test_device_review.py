"""Review (concurrency audit): races, shutdown, thread ownership and stale-report edge cases.

Everything runs on ``SimTransport.virtual`` (never the real device).
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from edu09 import units
from edu09.device.lock import OwnerLock
from edu09.device.scope import Scope
from edu09.device.worker import Event, StateEvent, WorkerState
from edu09.errors import Cancelled, DeviceBusy, DeviceDisconnected, Edu09Error
from edu09.model import Waveform
from edu09.protocol import tables
from edu09.transport.base import TransportInfo
from edu09.transport.sim import SimTransport

WAIT_S = 10.0


def wait_until(pred: Callable[[], bool], timeout: float = WAIT_S, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.002)


class Probe:
    """Transport wrapper: records the thread of every transport call; optional hooks."""

    def __init__(self, inner: SimTransport, kind: str | None = None) -> None:
        self.inner = inner
        self.info = TransportInfo(kind=kind, path="/dev/fake") if kind else inner.info
        self.calls: list[tuple[str, threading.Thread]] = []
        self._lock = threading.Lock()
        self.close_delay_s = 0.0
        self.on_write: Callable[[bytes], None] | None = None

    def _rec(self, what: str) -> None:
        with self._lock:
            self.calls.append((what, threading.current_thread()))

    def open(self) -> None:
        self._rec("open")
        self.inner.open()

    def close(self) -> None:
        self._rec("close")
        if self.close_delay_s:
            time.sleep(self.close_delay_s)  # e.g. a slow hidapi close
        self.inner.close()

    def write(self, payload: bytes) -> None:
        self._rec("write")
        self.inner.write(payload)
        if self.on_write is not None:
            self.on_write(bytes(payload))

    def read(self, timeout_s: float) -> bytes | None:
        self._rec("read")
        return self.inner.read(timeout_s)

    def threads(self) -> set[threading.Thread]:
        with self._lock:
            return {t for _, t in self.calls}


# --------------------------------------------------------------------------------------------
# 1. only the worker thread touches the transport
# --------------------------------------------------------------------------------------------
def test_only_worker_thread_does_transport_io() -> None:
    sim = SimTransport.virtual("dc:1v", seed=4)
    p = Probe(sim)
    scope = Scope.open(p, reconnect_interval_s=0.01)
    worker = scope.worker.thread
    try:
        scope.configure(timebase="1ms", volts_per_div=1)
        scope.acquire()
        scope.read_version()
        scope.live_level(3)
        scope.run_exclusive(lambda io: io.drain())
        scope.run()
        scope.acquire()
        scope.configure(volts_per_div=2)
        sim.unplug()
        wait_until(lambda: not scope.connected, what="disconnect")
        sim.replug()
        wait_until(lambda: scope.connected, what="reconnect")
        scope.stop()
        scope.set_calibration(units.Calibration(base_ofs=0x81))
        scope.subscribe(lambda ev: scope.close() if isinstance(ev, Waveform) else None)
        scope.acquire()
        wait_until(lambda: not worker.is_alive(), what="worker exit")
    finally:
        scope.close()
    assert p.threads() == {worker}
    assert p.calls[-1][0] == "close"


# --------------------------------------------------------------------------------------------
# 2. futures: resolved exactly once on every path
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(5))
def test_submit_racing_close_never_leaves_a_future_pending(seed: int) -> None:
    scope = Scope.open(SimTransport.virtual(seed=seed), reconnect_interval_s=0.01)
    w = scope.worker
    submitters: list[Callable[[], Future[Any]]] = [
        lambda: w.single(1.0),
        w.run,
        w.stop,
        w.read_version,
        lambda: w.live_level(1),
    ]
    futs: list[Future[Any]] = []
    lock = threading.Lock()
    go = threading.Barrier(7)

    def client() -> None:
        go.wait(WAIT_S)
        for k in range(40):
            f = submitters[k % len(submitters)]()
            with lock:
                futs.append(f)

    threads = [threading.Thread(target=client) for _ in range(6)]
    for t in threads:
        t.start()
    go.wait(WAIT_S)
    time.sleep(0.002 * seed)
    scope.close()
    for t in threads:
        t.join(WAIT_S)
    for f in futs:  # every future resolves (result, Cancelled or another error); none hangs
        with contextlib.suppress(Edu09Error):
            f.result(WAIT_S)
    assert len(futs) == 240 and all(f.done() for f in futs)
    assert not w.thread.is_alive()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_exclusive_base_exception_resolves_future() -> None:
    """Fixed in review: SystemExit from an exclusive fn left its future pending forever."""
    scope = Scope.open(SimTransport.virtual(), reconnect_interval_s=0.01)

    def fn(io: Any) -> None:
        raise SystemExit(3)

    fut = scope.worker.exclusive(fn)
    with pytest.raises(SystemExit):
        fut.result(WAIT_S)
    wait_until(lambda: not scope.worker.thread.is_alive(), what="worker exit")
    with pytest.raises(Cancelled):
        scope.acquire()
    scope.close()


def test_callback_system_exit_does_not_kill_worker(sim_scope: Scope) -> None:
    """Fixed in review: a callback raising SystemExit ended the worker thread silently and the
    in-flight acquire future (already detached from ``_current``) never resolved."""

    def cb(ev: Event) -> None:
        if isinstance(ev, Waveform):
            raise SystemExit(1)

    sim_scope.subscribe(cb)
    fut = sim_scope.acquire_async()
    assert fut.result(WAIT_S).seq == 1
    assert sim_scope.acquire().seq == 2
    assert sim_scope.worker.thread.is_alive()


def test_close_during_readout_resolves_and_closes() -> None:
    sim = SimTransport.virtual(seed=5)
    p = Probe(sim)
    scope = Scope.open(p, reconnect_interval_s=0.01)
    closer: list[threading.Thread] = []

    def on_write(payload: bytes) -> None:
        if payload[0] == tables.OP_READOUT and not closer:
            t = threading.Thread(target=scope.close)
            closer.append(t)
            t.start()
            time.sleep(0.02)  # let close() queue its shutdown while we are READING

    p.on_write = on_write
    fut = scope.acquire_async()
    wf_or_exc: Any
    try:
        wf_or_exc = fut.result(WAIT_S)
    except Cancelled as exc:
        wf_or_exc = exc
    closer[0].join(WAIT_S)
    assert isinstance(wf_or_exc, Waveform)  # the frame in flight completes first
    assert not scope.worker.thread.is_alive() and not sim.is_open
    assert scope.status.state == "closed"


def test_cancelled_waiter_while_running_is_dropped(sim_scope: Scope) -> None:
    sim_scope.run()
    fut = sim_scope.acquire_async(timeout_s=5)
    fut.cancel()
    assert sim_scope.acquire().seq >= 1
    sim_scope.stop()
    assert fut.cancelled()


# --------------------------------------------------------------------------------------------
# 3. callbacks: subscribe/unsubscribe during dispatch
# --------------------------------------------------------------------------------------------
def test_subscribe_and_unsubscribe_inside_callbacks(sim_scope: Scope) -> None:
    seen: list[str] = []
    unsubs: list[Callable[[], None]] = []

    def late(ev: Event) -> None:
        if isinstance(ev, Waveform):
            seen.append(f"late{ev.seq}")

    def first(ev: Event) -> None:
        if isinstance(ev, Waveform):
            seen.append(f"first{ev.seq}")
            unsubs[0]()  # unsubscribe self during dispatch
            sim_scope.subscribe(late)  # subscribe during dispatch: effective from next event

    unsubs.append(sim_scope.subscribe(first))
    sim_scope.acquire()
    sim_scope.acquire()
    assert seen == ["first1", "late2"]


# --------------------------------------------------------------------------------------------
# 4. close / shutdown semantics
# --------------------------------------------------------------------------------------------
def test_close_after_close_from_callback_waits_for_release(tmp_path: Path) -> None:
    """Fixed in review: a second close() (here after close() from a callback) returned at once,
    while the worker was still closing the transport and the owner lock was still held, so an
    immediate re-open raised DeviceBusy."""
    p = Probe(SimTransport.virtual(), kind="hid")
    p.close_delay_s = 0.3
    scope = Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration())
    scope.subscribe(lambda ev: scope.close() if isinstance(ev, Waveform) else None)
    scope.acquire()
    scope.close()  # main thread: must wait for the worker
    assert not scope.worker.thread.is_alive()
    with OwnerLock(lock_dir=tmp_path):
        pass


def test_concurrent_close_all_callers_wait(tmp_path: Path) -> None:
    p = Probe(SimTransport.virtual(), kind="hid")
    p.close_delay_s = 0.3
    scope = Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration())
    returned_alive: list[bool] = []

    def close() -> None:
        scope.close()
        returned_alive.append(scope.worker.thread.is_alive())

    threads = [threading.Thread(target=close) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT_S)
    assert returned_alive == [False] * 4
    assert scope.owner_lock is not None and not scope.owner_lock.is_held


def test_close_keeps_lock_while_worker_is_stuck(tmp_path: Path) -> None:
    """If the worker does not stop in time it may still do I/O: the lock must stay held until
    the worker really finishes (then the final StateEvent releases it)."""
    p = Probe(SimTransport.virtual(), kind="hid")
    p.close_delay_s = 0.5
    scope = Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration())
    scope.close(timeout_s=0.05)
    assert scope.worker.thread.is_alive()
    with pytest.raises(DeviceBusy):
        OwnerLock(lock_dir=tmp_path).acquire()
    wait_until(lambda: not scope.worker.thread.is_alive(), what="worker exit")
    wait_until(lambda: scope.owner_lock is not None and not scope.owner_lock.is_held)
    with OwnerLock(lock_dir=tmp_path):
        pass


def test_final_state_event_is_closed(sim_scope: Scope) -> None:
    states: list[StateEvent] = []
    sim_scope.subscribe(lambda ev: states.append(ev) if isinstance(ev, StateEvent) else None)
    sim_scope.run()
    sim_scope.close()
    assert states[-1].worker_state is WorkerState.CLOSED and not states[-1].running


EXIT_SCRIPT = """
import sys, threading, time
from edu09.device.scope import Scope
from edu09.transport.sim import SimTransport

class Rec:  # prints every OUT opcode and the close, so the parent sees what happened at exit
    def __init__(self, inner):
        self.inner, self.info = inner, inner.info
    def open(self):
        self.inner.open()
    def close(self):
        print("CLOSE", flush=True)
        self.inner.close()
    def write(self, payload):
        if exiting:
            print("OUT %02x" % payload[0], flush=True)
        self.inner.write(payload)
    def read(self, timeout_s):
        return self.inner.read(timeout_s)

exiting = False
sim = SimTransport.virtual(seed=0)
mode = sys.argv[1]
s = Scope.open(Rec(sim), reconnect_interval_s=0.01, clock=sim.device.clock,
               use_lock=True, lock_dir=sys.argv[2])
if mode == "running":
    s.run()
    while s.status.last_seq < 2:
        time.sleep(0.01)
    sim.device.never_trigger = True  # keep it armed from now on
    s.configure(trigger_mode="normal")
    while sim.device.state != "armed":
        time.sleep(0.001)
elif mode == "armed":
    sim.device.never_trigger = True
    s.configure(trigger_mode="normal")
    s.acquire_async(timeout_s=1e6)  # simulated s: must stay armed (VirtualClock runs fast)
    while sim.device.state != "armed":
        time.sleep(0.001)
elif mode == "disconnected":
    sim.unplug()  # an idle worker does no I/O: an acquire is needed to notice the unplug
    try:
        s.acquire()
    except Exception:
        pass
    while s.connected:
        time.sleep(0.01)
exiting = True
print("exiting", s.worker.thread.daemon, flush=True)
# no close(): the interpreter must still exit, and the device must be stopped and closed
"""


@pytest.mark.parametrize("mode", ["idle", "running", "armed", "disconnected"])
def test_interpreter_exit_without_close(mode: str, tmp_path: Path) -> None:
    t0 = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-c", EXIT_SCRIPT, mode, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stderr, out.stderr
    assert time.monotonic() - t0 < 30
    lines = out.stdout.splitlines()
    assert "exiting True" in lines, lines  # the worker is a daemon thread
    after = lines[lines.index("exiting True") + 1 :]
    if mode == "disconnected":
        assert "OUT 08" not in after  # nothing to stop (transport already closed on unplug)
    else:
        assert after and after[-1] == "CLOSE", after  # closed by the atexit hook
        assert after.count("CLOSE") == 1
    if mode in ("running", "armed"):
        assert after[0] == "OUT 08", after  # the armed device is stopped before the close
    if mode == "idle":
        assert after == ["CLOSE"]
    # the owner lock was released by the process itself (it removes owner.json on release)
    assert not (tmp_path / "owner.json").exists()
    with OwnerLock(lock_dir=tmp_path):
        pass


def test_idle_unplug_is_only_noticed_on_next_io() -> None:
    """Documents current behaviour: while IDLE the worker does no I/O, so ``connected`` stays
    True after an unplug until the next command touches the device."""
    sim = SimTransport.virtual()
    with Scope.open(sim, reconnect_interval_s=0.01) as scope:
        sim.unplug()
        time.sleep(0.1)
        assert scope.connected  # not detected yet
        with pytest.raises(DeviceDisconnected):
            scope.acquire()
        assert not scope.connected


def test_scope_survives_main_thread_exit_while_other_thread_uses_it() -> None:
    script = textwrap.dedent(
        """
        import threading, time
        from edu09.device.scope import Scope
        from edu09.transport.sim import SimTransport
        s = Scope.open(SimTransport.virtual(seed=0), reconnect_interval_s=0.01)
        def user():
            time.sleep(0.5)  # main thread has returned by now
            try:
                print("OK", s.acquire().seq, flush=True)
            except Exception as exc:
                print("FAIL", type(exc).__name__, flush=True)
            s.close()
        threading.Thread(target=user).start()
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert "OK 1" in out.stdout, out.stdout + out.stderr


# --------------------------------------------------------------------------------------------
# 5. stale reports (R1): the HW post-stop report arrives ~22 ms after 0x08
# --------------------------------------------------------------------------------------------
class LateStopReport:
    """Models protocol.md 11 finding 7 timing: after 0x08 on an armed device the all-zero
    report arrives ~22 ms later (on the 33 ms tick grid), not immediately as in the simulator.
    Assumes (HW-unverified) the firmware still sends it after the following 0x09/0x0B."""

    def __init__(self, inner: SimTransport, delay_s: float = 0.022) -> None:
        self.inner = inner
        self.info = inner.info
        self.clock = inner.device.clock
        self.delay_s = delay_s
        self.inject_at: float | None = None
        self.swallow = False

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes) -> None:
        if payload[0] == tables.OP_STOP and self.inner.device.state != "idle":
            self.swallow = True
            self.inject_at = self.clock() + self.delay_s
        self.inner.write(payload)

    def read(self, timeout_s: float) -> bytes | None:
        if self.inject_at is not None and self.clock() >= self.inject_at:
            self.inject_at = None
            return tables.WAITING_REPORT
        r = self.inner.read(timeout_s)
        if self.swallow and r == tables.WAITING_REPORT:
            self.swallow = False
            return None
        return r


def longest_zero_run(raw: Any) -> int:
    best = cur = 0
    for v in raw.tolist():
        cur = cur + 1 if v == 0 else 0
        best = max(best, cur)
    return best


@pytest.mark.parametrize("tb", ["200us", "500us"])
def test_late_post_stop_report_not_in_auto_fallback_frame(tb: str) -> None:
    sim = SimTransport.virtual("sine:1khz:1vpp", seed=1)
    sim.device.never_trigger = True
    with Scope.open(LateStopReport(sim), reconnect_interval_s=0.01) as scope:
        scope.configure(timebase=tb, trigger_mode="auto", volts_per_div=1)
        wf = scope.acquire()
    assert not wf.triggered
    assert longest_zero_run(wf.raw) < 8 and not wf.clipped


@pytest.mark.parametrize("tb", ["200us", "500us", "1ms"])
@pytest.mark.parametrize("delay_s", [0.045, 0.05, 0.06, 0.07])
def test_post_stop_report_later_than_the_wait_is_dropped_from_the_readout(
    tb: str, delay_s: float
) -> None:
    """Defence in depth: a post-stop report even later than the wait window (STATUS_INTERVAL_S
    + margin) lands before/inside the read-out; it is recognised by the report count."""
    sim = SimTransport.virtual("sine:1khz:1vpp", seed=1)
    sim.device.never_trigger = True
    with Scope.open(LateStopReport(sim, delay_s), reconnect_interval_s=0.01) as scope:
        scope.configure(timebase=tb, trigger_mode="auto", volts_per_div=1)
        wf = scope.acquire()
        assert sim.device.pending_reports == 0
    assert not wf.triggered and len(wf.raw) == tables.FRAME_SIZE
    assert longest_zero_run(wf.raw) < 8 and not wf.clipped


@pytest.mark.parametrize("delay_s", [0.022, 0.06])
def test_stop_then_acquire_has_no_post_stop_zeros(delay_s: float) -> None:
    sim = SimTransport.virtual("sine:1khz:1vpp", seed=2)
    sim.device.never_trigger = True
    with Scope.open(LateStopReport(sim, delay_s), reconnect_interval_s=0.01) as scope:
        scope.configure(timebase="200us", trigger_mode="normal", volts_per_div=1)
        fut = scope.acquire_async(timeout_s=1e6)  # simulated s: stays armed until stop()
        wait_until(lambda: sim.device.state == "armed", what="armed")
        scope.stop()
        with pytest.raises(Cancelled):
            fut.result(WAIT_S)
        sim.device.never_trigger = False
        wf = scope.acquire(trigger_mode="none")
    assert longest_zero_run(wf.raw) < 8 and not wf.clipped


def test_genuine_all_zero_frame_is_kept_while_a_post_stop_report_could_be_pending() -> None:
    """Clipped-low data (raw 0) right after an unanswered 0x08 (the connect's, on an idle
    device) is not mistaken for the late post-stop report: the report count decides."""
    sim = SimTransport.virtual("dc:-20v", seed=1)
    with Scope.open(sim, reconnect_interval_s=0.01) as scope:
        assert scope.worker._post_stop_until is not None  # idle device: no post-stop report
        scope.configure(timebase="200us", trigger_mode="none", volts_per_div=0.1)
        assert sim.device.clock() < scope.worker._post_stop_until
        wf = scope.acquire()
        assert sim.device.pending_reports == 0
    assert len(wf.raw) == tables.FRAME_SIZE and int(wf.raw.max()) == 0


# --------------------------------------------------------------------------------------------
# 5b. Scope.open failure paths (F3): the owner lock outlives a worker that is still doing I/O
# --------------------------------------------------------------------------------------------
class SlowOpen(Probe):
    def __init__(self, inner: SimTransport, delay_s: float, fail: bool = False) -> None:
        super().__init__(inner, kind="hid")
        self.delay_s = delay_s
        self.fail = fail

    def open(self) -> None:
        time.sleep(self.delay_s)
        if self.fail:
            raise DeviceDisconnected("simulated open failure")
        super().open()


def test_open_timeout_keeps_lock_until_worker_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import edu09.device.scope as scope_mod

    seen: list[Scope] = []
    real_abort = scope_mod.Scope._abort_open

    def abort(self: Scope, timeout_s: float = 5.0) -> None:
        seen.append(self)
        real_abort(self, timeout_s)

    monkeypatch.setattr(scope_mod.Scope, "_abort_open", abort)
    p = SlowOpen(SimTransport.virtual(), delay_s=0.3)  # worker still in open() at the timeout
    with pytest.raises(TimeoutError):
        Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration(), open_timeout_s=0.05)
    scope = seen[0]
    # open() returned only after the worker had finished (it is told to shut down and joined)
    assert not scope.worker.thread.is_alive() and scope.closed
    assert p.calls[-1][0] == "close" and not p.inner.is_open
    with OwnerLock(lock_dir=tmp_path):
        pass


def test_open_timeout_with_stuck_worker_releases_lock_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import edu09.device.scope as scope_mod

    seen: list[Scope] = []
    real_abort = scope_mod.Scope._abort_open

    def abort(self: Scope, timeout_s: float = 5.0) -> None:
        seen.append(self)
        real_abort(self, timeout_s=0.05)  # the worker is still in open(): cannot join in time

    monkeypatch.setattr(scope_mod.Scope, "_abort_open", abort)
    p = SlowOpen(SimTransport.virtual(), delay_s=0.5)
    with pytest.raises(TimeoutError):
        Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration(), open_timeout_s=0.05)
    scope = seen[0]
    assert scope.worker.thread.is_alive()
    with pytest.raises(DeviceBusy):  # still held: the worker may still touch the device
        OwnerLock(lock_dir=tmp_path).acquire()
    wait_until(lambda: not scope.worker.thread.is_alive(), what="worker exit")
    wait_until(lambda: scope.owner_lock is not None and not scope.owner_lock.is_held)
    with OwnerLock(lock_dir=tmp_path):
        pass
    assert not p.inner.is_open and scope.closed


def test_open_failure_releases_lock(tmp_path: Path) -> None:
    p = SlowOpen(SimTransport.virtual(), delay_s=0.0, fail=True)
    with pytest.raises(DeviceDisconnected):
        Scope.open(p, lock_dir=tmp_path, calibration=units.Calibration())
    with OwnerLock(lock_dir=tmp_path):
        pass


# --------------------------------------------------------------------------------------------
# 6. reconnect
# --------------------------------------------------------------------------------------------
def test_unplug_during_reconnect_handshake_keeps_retrying() -> None:
    sim = SimTransport.virtual(seed=6)
    p = Probe(sim)
    fail_next_connect = [2]

    def on_write(payload: bytes) -> None:  # unplug right after the connect's 0x08
        if payload[0] == tables.OP_STOP and fail_next_connect[0] > 0 and not scope.connected:
            fail_next_connect[0] -= 1
            sim.unplug()
            sim.replug(power_cycle=False)

    scope = Scope.open(p, reconnect_interval_s=0.01)
    try:
        scope.configure(timebase="1ms", volts_per_div=0.5)
        scope.run()
        p.on_write = on_write
        sim.unplug()
        wait_until(lambda: not scope.connected, what="disconnect")
        sim.replug()
        wait_until(lambda: scope.connected, what="reconnect")
        assert fail_next_connect[0] == 0  # two handshakes failed mid-way, then success
        n = scope.status.last_seq
        wait_until(lambda: scope.status.last_seq > n + 2, what="running resumed")
        assert sim.device.volts_per_div == 0.5
    finally:
        scope.close()


def test_acquire_while_disconnected_fails_fast() -> None:
    sim = SimTransport.virtual()
    with Scope.open(sim, reconnect_interval_s=10.0) as scope:
        sim.unplug()
        with pytest.raises(DeviceDisconnected):
            scope.acquire()
        t0 = time.monotonic()
        with pytest.raises(DeviceDisconnected):
            scope.acquire()
        assert time.monotonic() - t0 < 1.0


# --------------------------------------------------------------------------------------------
# 9. calibration save never falls through to the real user config dir
# --------------------------------------------------------------------------------------------
def test_set_calibration_save_without_path_on_sim_refuses(sim_scope: Scope) -> None:
    """Fixed in review: a sim scope (no calibration file) saved to storage's default path,
    i.e. the real user calibration.json (e.g. MCP --simulate + calibrate)."""
    with pytest.raises(ValueError, match="path"):
        sim_scope.set_calibration(units.Calibration(base_ofs=0x81), save=True)
    assert sim_scope.calibration == units.Calibration()  # nothing applied either


# --------------------------------------------------------------------------------------------
# settings consistency when a blocking call times out
# --------------------------------------------------------------------------------------------
def test_timed_out_configure_is_still_applied(
    sim_scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed in review: ``Scope._wait`` cancels a timed-out future and the worker skipped
    cancelled commands, so a configure that timed out behind a long exclusive operation updated
    ``Scope.settings`` but never reached the worker/device (permanent divergence)."""
    import edu09.device.scope as scope_mod

    monkeypatch.setattr(scope_mod, "COMMAND_TIMEOUT_S", 0.1)
    gate = threading.Event()
    busy = sim_scope.worker.exclusive(lambda io: gate.wait(WAIT_S))
    with pytest.raises(TimeoutError):
        sim_scope.configure(volts_per_div=5)
    gate.set()
    busy.result(WAIT_S)
    wait_until(lambda: sim_scope.worker.settings == sim_scope.settings, what="settings applied")
    assert sim_scope.acquire().settings.ch1.volts_per_div == 5.0
