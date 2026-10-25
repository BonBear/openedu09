"""AcquisitionWorker: sequencing, trigger modes, ETS, errors, unplug/reconnect, shutdown.

Driven by ``SimTransport.virtual`` (VirtualClock: deadlines in simulated time, no real sleeps)
and by scripted replays of the H1 hardware captures in ``tests/fixtures/h1``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import analysis, units
from edu09.device.scope import Scope
from edu09.device.worker import (
    AcquisitionWorker,
    ConnectionEvent,
    ErrorEvent,
    Event,
    SettingsEvent,
    StateEvent,
    WorkerState,
    decode_frame,
    frame_duration_s,
)
from edu09.errors import (
    AcquisitionTimeout,
    Cancelled,
    DeviceDisconnected,
    DeviceNotFound,
    ProtocolError,
)
from edu09.model import Coupling, Settings, Timebase, TriggerMode, Waveform
from edu09.protocol import commands, ets, reports, tables
from edu09.transport.base import TransportInfo
from edu09.transport.sim import SimTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "h1"
WAIT_S = 10.0
# Single timeout (simulated seconds) for tests that need a capture to STAY armed. A VirtualClock
# sim advances ~33 ms per waiting report without sleeping, so a 30 s timeout elapses in ~50 ms of
# real time and a real-time poll (coarse on Windows, GIL hand-off) can miss the ARMED state
# entirely (CI: "timed out waiting for armed"). 1e6 s keeps it armed for minutes of real time.
HOLD_ARMED_S = 1e6


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def wait_until(pred: Callable[[], bool], timeout: float = WAIT_S, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.002)


class Recorder:
    """Thread-safe event collector."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()

    def __call__(self, ev: Event) -> None:
        with self._lock:
            self.events.append(ev)

    def of(self, cls: type[Any]) -> list[Any]:
        with self._lock:
            return [e for e in self.events if isinstance(e, cls)]


def open_scope(transport: Any, **kw: Any) -> Scope:
    kw.setdefault("reconnect_interval_s", 0.01)
    return Scope.open(transport, **kw)


@pytest.fixture
def sim() -> SimTransport:
    return SimTransport.virtual("sine:1khz:2vpp", seed=1)


@pytest.fixture
def scope(sim: SimTransport) -> Iterator[Scope]:
    s = open_scope(sim)
    try:
        yield s
    finally:
        s.close()


class Wrapper:
    """Transport wrapper with hooks (fault injection / report mutation)."""

    def __init__(self, inner: Any, kind: str | None = None) -> None:
        self.inner = inner
        self.info = TransportInfo(kind=kind) if kind else inner.info
        self.on_write: Callable[[bytes], None] | None = None
        self.mutate: Callable[[bytes], bytes] | None = None

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes) -> None:
        self.inner.write(payload)
        if self.on_write is not None:
            self.on_write(bytes(payload))

    def read(self, timeout_s: float) -> bytes | None:
        r = self.inner.read(timeout_s)
        if r is not None and self.mutate is not None:
            r = self.mutate(r)
        return r


def trig_byte(wf: Waveform) -> int:
    return units.trigger_byte(wf.settings.trigger, wf.settings.ch1, units.Calibration())


def assert_aligned(wf: Waveform) -> None:
    """Rising trigger crossing exactly at the per-mode trigger index (sim places it there)."""
    ti = wf.trigger_index
    assert ti == units.trigger_index(wf.settings.timebase)
    level = trig_byte(wf)
    # the sim triggers on unrounded counts (c[ti-1] < level <= c[ti]); rounding to raw can make
    # raw[ti-1] == level (e.g. 126.6 -> 127), so only "<=" holds before the crossing
    assert wf.raw[ti - 1] <= level <= wf.raw[ti], (wf.raw[ti - 2 : ti + 2], level)


# --------------------------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------------------------
def test_frame_duration() -> None:
    assert frame_duration_s(Timebase.MS2) == pytest.approx(1024 / 15625)
    # ETS needs one pass per sub-record at the real ADC rate
    assert frame_duration_s(Timebase.US50) == pytest.approx(1024 / 625_000 * 8)
    assert frame_duration_s(Timebase.US10) == pytest.approx(1024 / 1_562_500 * 16)


@pytest.mark.parametrize("tb", [Timebase.MS1, Timebase.US50, Timebase.US20, Timebase.US10])
def test_decode_frame_deinterleaves_by_status_mode(tb: Timebase) -> None:
    mode = units.expected_acq_mode(tb)
    ordered = bytes((i * 7) % 256 for i in range(tables.FRAME_SIZE))
    wire = ets.interleave(mode, ordered)
    s = Settings(timebase=tb)
    raw, volts = decode_frame(wire, mode, s, units.Calibration())
    assert raw.tobytes() == ordered
    np.testing.assert_allclose(volts, units.counts_to_volts(raw, s.ch1, units.Calibration()))


def test_decode_frame_gnd_fill_and_length_check() -> None:
    s = Settings().replace(coupling=Coupling.GND)
    raw, volts = decode_frame(bytes(range(256)) * 4, 1, s, units.Calibration())
    assert set(raw.tolist()) == {127}  # calibrated ground count (default cal)
    assert not volts.any()
    with pytest.raises(ProtocolError):
        decode_frame(b"\x00" * 100, 1, s, units.Calibration())


# --------------------------------------------------------------------------------------------
# sequencing
# --------------------------------------------------------------------------------------------
def test_connect_and_single_sequence(scope: Scope, sim: SimTransport) -> None:
    assert scope.connected and scope.firmware == "1.01"
    # connect: stop, reset, version, config
    assert sim.device.received[:4] == [0x08, 0x09, 0x0F, 0x0E]
    del sim.device.received[:]
    scope.configure(timebase="1ms", volts_per_div=0.5)
    wf = scope.acquire()
    # HW-verified arm sequence: 0x0E (new config) -> 0x09 -> 0x0B, then 0x0A read-out
    assert sim.device.received == [0x0E, 0x09, 0x0B, 0x0A]
    assert wf.seq == 1 and wf.triggered and wf.acq_mode == 1
    assert wf.settings == scope.settings
    assert_aligned(wf)
    assert wf.dt == pytest.approx(1 / 31_250)
    st = analysis.stats(wf.volts, wf.dt)
    assert st["frequency_hz"] == pytest.approx(1000, rel=0.01)
    assert st["vpp"] == pytest.approx(2.0, rel=0.03)
    # config unchanged -> not re-sent
    del sim.device.received[:]
    scope.acquire()
    assert sim.device.received == [0x09, 0x0B, 0x0A]


def test_trailing_report_drained_consecutive_frames_aligned(sim: SimTransport) -> None:
    sim.device.trailing_reports = 3  # more stale reports than the HW's one: drained before arm
    with open_scope(sim) as scope:
        scope.configure(timebase="500us", volts_per_div=0.5)
        frames = [scope.acquire() for _ in range(4)]
        for wf in frames:
            assert_aligned(wf)
        assert [w.seq for w in frames] == [1, 2, 3, 4]
        assert sim.device.frames_sent == 4
        # the worker reads the HW's single trailing report right away; extra ones are left in
        # the IN queue and flushed after the next 0x09, before 0x0B
        assert sim.device.pending_reports == 2


def test_stale_non_status_report_while_armed_is_ignored(sim: SimTransport) -> None:
    junk = bytes([0x85] * 64)
    w = Wrapper(sim)

    def after_arm(p: bytes) -> None:
        if p[0] == tables.OP_ARM:
            sim.device._queue.appendleft(junk)  # like the HW's undrained trailing report

    with open_scope(w) as scope:
        w.on_write = after_arm
        scope.configure(timebase="1ms", volts_per_div=0.5)
        for _ in range(2):
            assert_aligned(scope.acquire())


# --------------------------------------------------------------------------------------------
# trigger modes
# --------------------------------------------------------------------------------------------
def configs_sent(sim: SimTransport) -> list[commands.HwConfig]:
    return [commands.decode_config(p) for p in sim_payloads(sim) if p[0] == tables.OP_CONFIG]


def sim_payloads(sim: SimTransport) -> list[bytes]:
    return getattr(sim, "_payloads", [])


def record_payloads(sim: SimTransport) -> None:
    orig = sim.write
    sim._payloads = []  # type: ignore[attr-defined]

    def write(payload: bytes) -> None:
        sim._payloads.append(bytes(payload))  # type: ignore[attr-defined]
        orig(payload)

    sim.write = write  # type: ignore[method-assign]


def test_auto_falls_back_to_free_run(scope: Scope, sim: SimTransport) -> None:
    record_payloads(sim)
    sim.device.never_trigger = True
    scope.configure(timebase="1ms", trigger_mode="auto")
    wf = scope.acquire(timeout_s=1.0)
    assert not wf.triggered
    assert wf.settings.trigger.mode is TriggerMode.AUTO
    ops = [p[0] for p in sim_payloads(sim)]
    # trigger armed, timed out -> 0x08 stop, config with trigger off, re-arm, read-out
    i = ops.index(tables.OP_STOP)
    assert ops[i:] == [0x08, 0x0E, 0x09, 0x0B, 0x0A]
    cfgs = configs_sent(sim)
    assert cfgs[-2].flags & tables.FLAG_TRIG_ENABLE
    assert not cfgs[-1].flags & tables.FLAG_TRIG_ENABLE
    # the next AUTO capture re-enables the trigger first
    sim.device.never_trigger = False
    wf2 = scope.acquire()
    assert wf2.triggered
    assert configs_sent(sim)[-1].flags & tables.FLAG_TRIG_ENABLE
    assert_aligned(wf2)


def test_normal_times_out_and_device_is_stopped(scope: Scope, sim: SimTransport) -> None:
    sim.device.never_trigger = True
    scope.configure(timebase="1ms", trigger_mode="normal")
    t0 = time.monotonic()
    with pytest.raises(AcquisitionTimeout, match="no trigger"):
        scope.acquire(timeout_s=0.5)
    assert time.monotonic() - t0 < 5  # virtual time: no real 0.5 s wait needed
    assert sim.device.state == "idle"
    assert scope.status.state == "idle"
    # recovers
    sim.device.never_trigger = False
    assert scope.acquire().triggered


def test_trigger_mode_override_and_none(scope: Scope, sim: SimTransport) -> None:
    record_payloads(sim)
    scope.configure(timebase="1ms", trigger_mode="normal")
    wf = scope.acquire(trigger_mode="none")
    assert not wf.triggered
    assert wf.settings.trigger.mode is TriggerMode.NONE
    assert not configs_sent(sim)[-1].flags & tables.FLAG_TRIG_ENABLE
    assert scope.settings.trigger.mode is TriggerMode.NORMAL  # override is per call
    sim.device.never_trigger = True
    with pytest.raises(AcquisitionTimeout):
        scope.acquire(timeout_s=0.2)  # normal again
    wf = scope.acquire(trigger_mode="auto")
    assert not wf.triggered


@pytest.mark.parametrize(
    ("tb", "mode", "freq"),
    [("50us", 2, 20_000), ("20us", 3, 50_000), ("10us", 4, 50_000)],
)
def test_ets_capture(tb: str, mode: int, freq: int) -> None:
    sim = SimTransport.virtual(f"sine:{freq}hz:2vpp", seed=3)
    with open_scope(sim) as scope:
        scope.configure(timebase=tb, volts_per_div=0.5, trigger_mode="normal")
        wf = scope.acquire()
        assert wf.acq_mode == mode and wf.triggered
        assert wf.trigger_index == tables.TRIGGER_INDEX_BY_MODE[mode]
        assert_aligned(wf)
        st = analysis.stats(wf.volts, wf.dt)
        assert st["frequency_hz"] == pytest.approx(freq, rel=0.01)


def test_ets_auto_has_no_free_run_fallback(scope: Scope, sim: SimTransport) -> None:
    sim.device.never_trigger = True
    scope.configure(timebase="20us", trigger_mode="auto")
    with pytest.raises(AcquisitionTimeout, match="ETS"):
        scope.acquire(timeout_s=0.3)
    with pytest.raises(ValueError, match="trigger"):
        scope.acquire(trigger_mode="none")


# --------------------------------------------------------------------------------------------
# protocol errors
# --------------------------------------------------------------------------------------------
def wrong_mode(r: bytes) -> bytes:
    if r[:2] == bytes((tables.STATUS_MARKER, tables.STATUS_COMPLETE)):
        return r[:2] + bytes((2,)) + r[3:]
    return r


def test_wrong_status_mode_raises_protocol_error(sim: SimTransport) -> None:
    w = Wrapper(sim)
    with open_scope(w) as scope:
        scope.configure(timebase="1ms")
        w.mutate = wrong_mode
        with pytest.raises(ProtocolError, match="mode 2, expected 1"):
            scope.acquire()
        assert scope.status.state == "idle"
        w.mutate = None
        assert_aligned(scope.acquire())  # resynced


def test_wrong_status_mode_while_running_stops_after_retries(sim: SimTransport) -> None:
    w = Wrapper(sim)
    rec = Recorder()
    with open_scope(w) as scope:
        scope.subscribe(rec)
        w.mutate = wrong_mode
        scope.run()
        wait_until(lambda: not scope.running, what="running to stop")
        # running=False is published just before the final ErrorEvent is emitted (worker
        # _acq_failed), so wait for the event itself (was flaky under parallel load)
        wait_until(lambda: len(rec.of(ErrorEvent)) >= 3, what="3 error events")
        errs = rec.of(ErrorEvent)
        assert len(errs) == 3
        assert all(isinstance(e.error, ProtocolError) for e in errs)
        assert errs[-1].running_stopped and not errs[0].running_stopped
        assert "ProtocolError" in errs[0].message
        assert not rec.of(Waveform)


def test_frame_read_timeout_resyncs(sim: SimTransport) -> None:
    sim.device.trailing_reports = 0
    with open_scope(sim) as scope:
        scope.configure(timebase="1ms")
        sim.device.drop_frame_report = 5
        with pytest.raises(ProtocolError, match="15/16"):
            scope.acquire()
        assert_aligned(scope.acquire())


# --------------------------------------------------------------------------------------------
# run / stop / settings changes
# --------------------------------------------------------------------------------------------
def test_run_stop_and_settings_change_mid_run(scope: Scope, sim: SimTransport) -> None:
    rec = Recorder()
    scope.subscribe(rec)
    scope.configure(timebase="1ms", volts_per_div=1)
    scope.run()
    assert scope.status.state == "running"
    wait_until(lambda: len(rec.of(Waveform)) >= 5, what="5 frames")
    new = scope.configure(timebase="500us", volts_per_div=0.5, source="test")
    wf = scope.acquire()  # served from the running stream, newest settings
    assert wf.settings == new
    wait_until(lambda: rec.of(Waveform)[-1].seq > wf.seq + 2, what="more frames")
    scope.stop()
    assert scope.status.state == "idle" and not scope.running
    frames = rec.of(Waveform)
    seqs = [f.seq for f in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # all frames after the settings change use the new settings; none mixes old/new
    idx = next(i for i, f in enumerate(frames) if f.settings == new)
    assert all(f.settings == new for f in frames[idx:])
    for f in frames:
        assert_aligned(f)
    sev = [e for e in rec.of(SettingsEvent) if e.source == "test"]
    assert sev and sev[0].settings == new
    states = [e.state for e in rec.of(StateEvent)]
    assert "running" in states and states[-1] == "idle"
    n = len(rec.of(Waveform))
    time.sleep(0.05)
    assert len(rec.of(Waveform)) == n  # really stopped


def test_stop_cancels_pending_single_quickly(scope: Scope, sim: SimTransport) -> None:
    sim.device.never_trigger = True
    scope.configure(trigger_mode="normal")
    fut = scope.acquire_async(timeout_s=HOLD_ARMED_S)
    wait_until(lambda: scope.status.state == "armed", what="armed")
    t0 = time.monotonic()
    scope.stop()
    with pytest.raises(Cancelled):
        fut.result(WAIT_S)
    assert time.monotonic() - t0 < 1.0
    assert sim.device.state == "idle"


def test_future_cancel_aborts_armed_single(scope: Scope, sim: SimTransport) -> None:
    sim.device.never_trigger = True
    scope.configure(trigger_mode="normal")
    fut = scope.acquire_async(timeout_s=HOLD_ARMED_S)
    wait_until(lambda: scope.status.state == "armed", what="armed")
    assert fut.cancel()
    wait_until(lambda: scope.status.state == "idle", what="idle after cancel")
    assert sim.device.received[-1] == tables.OP_STOP
    sim.device.never_trigger = False
    assert scope.acquire().triggered


def test_singles_queue_in_order(scope: Scope) -> None:
    futs = [scope.acquire_async() for _ in range(5)]
    seqs = [f.result(WAIT_S).seq for f in futs]
    assert seqs == [1, 2, 3, 4, 5]


def test_single_while_running_normal_requires_triggered(scope: Scope, sim: SimTransport) -> None:
    scope.configure(timebase="1ms", trigger_mode="auto")
    sim.device.never_trigger = True
    scope.run()
    assert not scope.acquire().triggered
    with pytest.raises(AcquisitionTimeout):
        scope.acquire(timeout_s=0.2, trigger_mode="normal")
    sim.device.never_trigger = False
    assert scope.acquire(trigger_mode="normal").triggered
    scope.stop()


def test_run_errors_while_disconnected_and_configure_allowed(sim: SimTransport) -> None:
    rec = Recorder()
    with open_scope(sim, auto_reconnect=False) as scope:
        scope.subscribe(rec)
        sim.unplug()
        with pytest.raises(DeviceDisconnected):
            scope.acquire()
        wait_until(lambda: not scope.connected, what="disconnect")
        with pytest.raises(DeviceDisconnected):
            scope.acquire()
        with pytest.raises(DeviceDisconnected):
            scope.run()
        with pytest.raises(DeviceDisconnected):
            scope.read_version()
        s = scope.configure(volts_per_div=5)  # only stored
        scope.stop()  # allowed (clears the resume flag)
        assert scope.status.state == "disconnected"
        with pytest.raises(DeviceNotFound):
            scope.reconnect()
        sim.replug()
        assert scope.reconnect() == "1.01"
        assert sim.device.volts_per_div == 5.0  # stored settings re-applied
        assert scope.acquire().settings == s
        conn = rec.of(ConnectionEvent)
        assert [c.connected for c in conn] == [False, True]


# --------------------------------------------------------------------------------------------
# unplug / reconnect
# --------------------------------------------------------------------------------------------
def test_unplug_mid_frame_reconnects_and_resumes(sim: SimTransport) -> None:
    w = Wrapper(sim)
    rec = Recorder()
    with open_scope(w) as scope:
        scope.subscribe(rec)
        scope.configure(timebase="1ms", volts_per_div=0.5, trigger_mode="normal")
        scope.run()
        wait_until(lambda: len(rec.of(Waveform)) >= 3, what="frames")
        single = scope.acquire_async(timeout_s=30)

        def unplug_during_readout(p: bytes) -> None:
            if p[0] == tables.OP_READOUT:
                w.on_write = None
                sim.schedule_unplug(after_reads=5)  # 5 reports into the read-out

        w.on_write = unplug_during_readout
        wait_until(lambda: not scope.connected, what="disconnect")
        with pytest.raises(DeviceDisconnected):
            single.result(WAIT_S)
        assert scope.status.state == "disconnected"
        assert scope.running  # resume flag kept
        n_before = len(rec.of(Waveform))
        time.sleep(0.05)  # several reconnect attempts fail while unplugged
        assert not scope.connected
        sim.device.power_cycle()
        sim.replug(power_cycle=False)
        wait_until(lambda: scope.connected, what="reconnect")
        wait_until(lambda: len(rec.of(Waveform)) >= n_before + 3, what="frames after replug")
        assert sim.device.volts_per_div == 0.5  # settings re-applied
        assert sim.device.timebase.s_per_div == 1e-3
        for f in rec.of(Waveform):
            assert_aligned(f)
        conn = rec.of(ConnectionEvent)
        assert [c.connected for c in conn] == [False, True]
        assert conn[0].error and conn[1].firmware == "1.01"
        seqs = [f.seq for f in rec.of(Waveform)]
        assert seqs == sorted(set(seqs))
        scope.stop()


def test_open_fails_when_unplugged() -> None:
    sim = SimTransport.virtual()
    sim.unplug()
    before = threading.active_count()
    with pytest.raises(DeviceNotFound):
        Scope.open(sim)
    wait_until(lambda: threading.active_count() <= before, what="worker exit")


# --------------------------------------------------------------------------------------------
# shutdown
# --------------------------------------------------------------------------------------------
def test_shutdown_cancels_pending_futures(sim: SimTransport) -> None:
    sim.device.never_trigger = True
    scope = open_scope(sim)
    scope.configure(trigger_mode="normal")
    futs = [scope.acquire_async(timeout_s=HOLD_ARMED_S) for _ in range(4)]
    wait_until(lambda: scope.status.state == "armed", what="armed")
    t0 = time.monotonic()
    scope.close()
    assert time.monotonic() - t0 < 2.0
    for f in futs:
        with pytest.raises(Cancelled):
            f.result(WAIT_S)
    assert not scope.worker.thread.is_alive()
    assert scope.status.state == "closed"
    assert sim.device.received[-1] == tables.OP_STOP  # stopped the armed device
    assert not sim.is_open
    with pytest.raises(Cancelled):
        scope.acquire()
    scope.close()  # idempotent


def test_worker_direct_events_and_shutdown(sim: SimTransport) -> None:
    rec = Recorder()
    w = AcquisitionWorker(sim, on_event=rec, clock=sim.device.clock)
    assert w.start().result(WAIT_S) == "1.01"
    assert w.state is WorkerState.IDLE
    fut: Future[Waveform] = w.single(1.0)
    assert fut.result(WAIT_S).seq == 1
    assert w.last_seq == 1
    assert rec.of(ConnectionEvent)[0].connected
    w.shutdown()
    assert w.state is WorkerState.CLOSED
    assert rec.of(StateEvent)[-1].state == "closed"
    with pytest.raises(Cancelled):
        w.run().result(1)


def test_bad_callback_does_not_kill_worker(scope: Scope) -> None:
    def boom(ev: Event) -> None:
        raise RuntimeError("subscriber bug")

    scope.subscribe(boom)
    assert scope.acquire().seq == 1
    assert scope.acquire().seq == 2


# --------------------------------------------------------------------------------------------
# live level / exclusive hook
# --------------------------------------------------------------------------------------------
def test_live_level_and_exclusive(sim: SimTransport) -> None:
    sim.device.signal = sim.device.signal.parse("dc:1v")
    with open_scope(sim) as scope:
        scope.configure(volts_per_div=1)
        expected = units.volts_to_counts(1.0, scope.settings.ch1, units.Calibration())
        assert scope.live_level(10) == pytest.approx(float(expected), abs=1)
        scope.run()
        threads: list[threading.Thread] = []

        def op(io: Any) -> str:
            threads.append(threading.current_thread())
            io.write(commands.encode_version_query())
            r = io.read(0.1)
            assert r is not None
            return reports.parse_version(r)

        assert scope.run_exclusive(op) == "1.01"
        assert threads == [scope.worker.thread]
        n = scope.status.last_seq
        wait_until(lambda: scope.status.last_seq > n + 2, what="running resumed")
        scope.stop()


def test_exclusive_error_propagates_and_recovers(scope: Scope) -> None:
    def bad(io: Any) -> None:
        raise ValueError("calibration failed")

    with pytest.raises(ValueError, match="calibration failed"):
        scope.run_exclusive(bad)
    assert scope.acquire().seq == 1


# --------------------------------------------------------------------------------------------
# H1 hardware captures replayed through the worker
# --------------------------------------------------------------------------------------------
class ScriptedDevice:
    """Plays the device side of an H1 capture: on 0x0B / 0x0A it queues the reports the real
    scope sent after that command in the capture (including the undrained trailing report the
    probe left at the start of the next arm segment)."""

    VERSION = b"1.01\r" + bytes(range(59))

    def __init__(self, path: Path) -> None:
        self.arm_segments: list[list[bytes]] = []
        self.readout_segments: list[list[bytes]] = []
        seg: list[bytes] | None = None
        for line in path.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            rec = json.loads(line)
            data = bytes.fromhex(rec["hex"])
            if rec["dir"] == "out":
                if data[0] == tables.OP_ARM:
                    seg = []
                    self.arm_segments.append(seg)
                elif data[0] == tables.OP_READOUT:
                    seg = []
                    self.readout_segments.append(seg)
            elif seg is not None:
                seg.append(data)
        self.info = TransportInfo(kind="replay", path=str(path))
        self._queue: list[bytes] = []
        self._arms = 0
        self._readouts = 0
        self._lock = threading.Lock()

    def expected_frames(self, mode: int) -> list[bytes]:
        out = []
        for seg in self.readout_segments:
            asm = reports.FrameAssembler(expected_mode=mode)
            for r in seg:
                if asm.feed(r):
                    break
            out.append(ets.deinterleave(mode, asm.frame))
        return out

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, payload: bytes) -> None:
        op = payload[0]
        with self._lock:
            if op == tables.OP_VERSION:
                self._queue.append(self.VERSION)
            elif op == tables.OP_ARM and self._arms < len(self.arm_segments):
                self._queue += self.arm_segments[self._arms]
                self._arms += 1
            elif op == tables.OP_READOUT and self._readouts < len(self.readout_segments):
                self._queue += self.readout_segments[self._readouts]
                self._readouts += 1

    def read(self, timeout_s: float) -> bytes | None:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        time.sleep(min(timeout_s, 0.001))
        return None


@pytest.mark.parametrize(
    ("name", "tb", "mode", "n"),
    [
        ("square_trig_200us_rise.jsonl", "200us", 1, 3),
        ("square_trig_200us_fall.jsonl", "200us", 1, 3),
        ("square_trig_50us_rise.jsonl", "50us", 2, 2),
        ("square_trig_20us_rise.jsonl", "20us", 3, 2),
        ("square_trig_10us_rise.jsonl", "10us", 4, 2),
    ],
)
def test_replay_h1_triggered_captures(name: str, tb: str, mode: int, n: int) -> None:
    dev = ScriptedDevice(FIXTURES / name)
    assert len(dev.readout_segments) == n
    expected = dev.expected_frames(mode)
    with open_scope(dev) as scope:
        scope.configure(timebase=tb, volts_per_div=2, trigger_mode="normal")
        frames = [scope.acquire(timeout_s=2.0) for _ in range(n)]
    for wf, exp in zip(frames, expected, strict=True):
        assert wf.acq_mode == mode and wf.triggered
        assert wf.raw.tobytes() == exp
        # 0..4.16 V square: raw ~132 (0 V) .. ~197 (4.16 V)
        assert 125 <= np.median(wf.raw[wf.raw < 160]) <= 140
        assert 190 <= np.median(wf.raw[wf.raw >= 160]) <= 205


def test_replay_h1_free_run() -> None:
    dev = ScriptedDevice(FIXTURES / "freerun_2ms.jsonl")
    with open_scope(dev) as scope:
        scope.configure(timebase="2ms", volts_per_div=1, trigger_mode="none")
        wf = scope.acquire()
    assert not wf.triggered and wf.raw.tobytes() == dev.expected_frames(1)[0]
    assert 131 <= wf.raw.mean() <= 135  # open input (protocol.md 11, finding 8)


def test_replay_h1_trigger_timeout() -> None:
    dev = ScriptedDevice(FIXTURES / "trigger_timeout_2ms.jsonl")
    assert dev.arm_segments and all(reports.is_waiting_report(r) for r in dev.arm_segments[0][:-1])
    with open_scope(dev) as scope:
        scope.configure(timebase="2ms", trigger_mode="normal")
        with pytest.raises(AcquisitionTimeout):
            scope.acquire(timeout_s=0.1)
        assert scope.status.state == "idle"
