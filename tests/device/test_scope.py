"""Scope facade: settings, events, store, calibration hooks, ownership and concurrency."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from edu09 import Scope as TopLevelScope
from edu09 import storage, units
from edu09.device.lock import OwnerLock, read_owner
from edu09.device.scope import Scope, ScopeStatus
from edu09.device.worker import ConnectionEvent, Event, SettingsEvent, StateEvent
from edu09.errors import (
    AcquisitionTimeout,
    Cancelled,
    DeviceBusy,
    UnsafeOperation,
)
from edu09.model import Settings, Timebase, TriggerMode, VoltsPerDiv, Waveform
from edu09.transport.base import TransportInfo
from edu09.transport.sim import SimTransport

WAIT_S = 10.0


def wait_until(pred: Callable[[], bool], timeout: float = WAIT_S, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.002)


class HidLike:
    """A SimTransport that claims to be hardware (so Scope takes the owner lock)."""

    def __init__(self) -> None:
        self.inner = SimTransport.virtual(seed=0)
        self.info = TransportInfo(kind="hid", path="/dev/fake-edu09")

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes) -> None:
        self.inner.write(payload)

    def read(self, timeout_s: float) -> bytes | None:
        return self.inner.read(timeout_s)


# --------------------------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------------------------
def test_top_level_reexport() -> None:
    assert TopLevelScope is Scope


def test_status_and_defaults(sim_scope: Scope) -> None:
    st = sim_scope.status
    assert isinstance(st, ScopeStatus)
    assert st.connected and st.firmware == "1.01"
    assert st.state == "idle" and not st.running
    assert st.settings == Settings()
    assert st.last_seq == 0 and st.last_waveform_id is None
    assert st.calibration_source == "default"
    assert st.simulated and st.transport_kind == "sim" and st.owner is None
    d = st.to_dict()
    assert d["state"] == "idle" and d["settings"] == Settings().to_dict()
    assert d["owner"] is None and d["simulated"] is True
    wf = sim_scope.acquire()
    st = sim_scope.status
    assert st.last_seq == wf.seq and st.last_waveform_id == "w1"


def test_configure_validation_and_warnings(sim_scope: Scope) -> None:
    with pytest.raises(ValueError, match="allowed"):
        sim_scope.configure(volts_per_div=0.3)
    with pytest.raises(ValueError, match="unknown setting"):
        sim_scope.configure(bogus=1)
    assert sim_scope.settings == Settings()  # atomic: nothing applied
    sim_scope.configure(trigger_mode="none")
    s, warnings = sim_scope.configure_with_warnings(timebase="20us")
    assert s.trigger.mode is TriggerMode.AUTO and warnings
    assert sim_scope.settings == s
    full = Settings().replace(timebase="1ms", volts_per_div=1)
    assert sim_scope.set_settings(full) == full
    assert sim_scope.worker.settings == full
    assert sim_scope.acquire().settings == full


def test_settings_event_carries_source_and_warnings(sim_scope: Scope) -> None:
    events: list[Event] = []
    sim_scope.subscribe(events.append)
    sim_scope.configure(trigger_mode="none", source="gui")
    sim_scope.configure(timebase="50us", source="mcp")
    sev = [e for e in events if isinstance(e, SettingsEvent)]
    assert [e.source for e in sev] == ["gui", "mcp"]
    assert sev[1].warnings and sev[1].settings.timebase is Timebase.US50


def test_store_and_subscribe_unsubscribe(sim_scope: Scope) -> None:
    got: list[Event] = []
    unsub = sim_scope.subscribe(got.append)
    wf = sim_scope.acquire()
    assert sim_scope.store.get("latest") is wf and sim_scope.latest() is wf
    assert sim_scope.store.get("w1") is wf
    assert wf in got
    unsub()
    unsub()  # idempotent
    wf2 = sim_scope.acquire()
    assert wf2 not in got
    assert sim_scope.store.ids() == ["w1", "w2"]


def test_callback_restrictions(sim_scope: Scope) -> None:
    """Blocking calls from a callback raise; queued calls (configure/stop) work."""
    errors: list[BaseException] = []
    done = threading.Event()

    def cb(ev: Event) -> None:
        if isinstance(ev, Waveform) and not done.is_set():
            done.set()
            try:
                sim_scope.acquire()
            except RuntimeError as exc:
                errors.append(exc)
            sim_scope.configure(volts_per_div=5, source="callback")
            sim_scope.stop()

    sim_scope.subscribe(cb)
    sim_scope.run()
    assert done.wait(WAIT_S)
    wait_until(lambda: not sim_scope.running, what="stop from callback")
    assert errors and "deadlock" in str(errors[0])
    wait_until(lambda: sim_scope.worker.settings.ch1.volts_per_div is VoltsPerDiv.V5)


def test_acquire_argument_validation(sim_scope: Scope) -> None:
    with pytest.raises(ValueError):
        sim_scope.acquire(timeout_s=0)
    with pytest.raises(ValueError):
        sim_scope.acquire(trigger_mode="sometimes")
    sim_scope.configure(timebase="10us")
    with pytest.raises(ValueError, match="needs a trigger"):
        sim_scope.acquire(trigger_mode="off")


def test_set_running_and_states(sim_scope: Scope) -> None:
    states: list[str] = []
    sim_scope.subscribe(lambda e: states.append(e.state) if isinstance(e, StateEvent) else None)
    sim_scope.set_running(True)
    assert sim_scope.running and sim_scope.status.state == "running"
    sim_scope.set_running(False)
    assert not sim_scope.running
    assert states[0] == "running" and states[-1] == "idle"


def test_read_version(sim_scope: Scope) -> None:
    assert sim_scope.read_version() == "1.01"
    sim_scope.run()
    assert sim_scope.read_version() == "1.01"  # cached while acquiring
    sim_scope.stop()


# --------------------------------------------------------------------------------------------
# calibration hooks
# --------------------------------------------------------------------------------------------
def test_set_calibration_saves_with_backup(sim_scope: Scope, tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    cal1 = units.Calibration(base_ofs=0x82, source="measured")
    assert sim_scope.set_calibration(cal1, save=True, path=path) is None  # no previous file
    assert storage.load_calibration(path) == cal1
    assert sim_scope.calibration == cal1
    assert sim_scope.status.calibration_source == "measured"
    wf = sim_scope.acquire()
    assert wf.calibration_id == cal1.calibration_id
    cal2 = units.Calibration(base_ofs=0x7E, source="measured")
    backup = sim_scope.set_calibration(cal2, save=True, path=path)
    assert backup is not None and backup.exists()
    assert storage.load_calibration(backup) == cal1
    assert storage.load_calibration(path) == cal2
    # config re-sent with the new offset DAC
    assert sim_scope.worker._last_cfg is not None
    assert sim_scope.worker._last_cfg.ofs1 == units.offset_dac(sim_scope.settings.ch1, cal2)


def test_calibrate_requires_confirmation(sim_scope: Scope) -> None:
    with pytest.raises(UnsafeOperation):
        sim_scope.calibrate(confirm=True, input_disconnected=False)
    with pytest.raises(UnsafeOperation):
        sim_scope.calibrate(confirm=False, input_disconnected=True)


def test_calibrate_delegates_to_calibration_module(
    sim_scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from edu09.device import calibration as calmod

    expected = units.Calibration(base_ofs=0x81, source="measured")
    monkeypatch.setattr(calmod, "measure_calibration", lambda scope: expected)
    cal = sim_scope.calibrate(confirm=True, input_disconnected=True, path=tmp_path / "c.json")
    assert cal == expected and sim_scope.calibration == expected
    assert storage.load_calibration(tmp_path / "c.json") == expected


# --------------------------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------------------------
def test_hardware_transport_takes_owner_lock(tmp_path: Path) -> None:
    s1 = Scope.open(HidLike(), lock_dir=tmp_path, owner_kind="gui", calibration=units.Calibration())
    try:
        assert s1.owner_lock is not None and s1.owner_lock.is_held
        assert s1.status.owner is not None and s1.status.owner.kind == "gui"
        assert s1.status.to_dict()["owner"]["kind"] == "gui"
        s1.set_mcp_url("http://127.0.0.1:8765/mcp")
        owner = read_owner(tmp_path)
        assert owner is not None and owner.mcp_url == "http://127.0.0.1:8765/mcp"
        hw2 = HidLike()
        with pytest.raises(DeviceBusy) as ei:
            Scope.open(hw2, lock_dir=tmp_path, calibration=units.Calibration())
        assert ei.value.owner is not None and ei.value.owner.kind == "gui"
        assert ei.value.owner.mcp_url == "http://127.0.0.1:8765/mcp"
        assert not hw2.inner.is_open  # never touched the device
    finally:
        s1.close()
    assert read_owner(tmp_path) is None
    with OwnerLock(lock_dir=tmp_path):
        pass  # free again
    s3 = Scope.open(HidLike(), lock_dir=tmp_path, calibration=units.Calibration())
    s3.close()


def test_close_from_callback_releases_lock(tmp_path: Path) -> None:
    scope = Scope.open(HidLike(), lock_dir=tmp_path, calibration=units.Calibration())
    scope.subscribe(lambda ev: scope.close() if isinstance(ev, Waveform) else None)
    assert scope.acquire().seq == 1  # the frame is still delivered; close is queued after it
    wait_until(lambda: not scope.worker.thread.is_alive(), what="worker exit")
    with pytest.raises(Cancelled):
        scope.acquire()
    assert scope.closed and scope.owner_lock is not None and not scope.owner_lock.is_held
    with OwnerLock(lock_dir=tmp_path):
        pass


def test_sim_transport_skips_lock(tmp_path: Path) -> None:
    with OwnerLock(lock_dir=tmp_path), Scope.open(SimTransport.virtual(), lock_dir=tmp_path) as s:
        assert s.owner_lock is None


def test_failed_open_releases_lock(tmp_path: Path) -> None:
    hw = HidLike()
    hw.inner.unplug()
    with pytest.raises(Exception, match="unplugged"):
        Scope.open(hw, lock_dir=tmp_path, calibration=units.Calibration())
    with OwnerLock(lock_dir=tmp_path):
        pass


# --------------------------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------------------------
def test_many_threads(sim_scope: Scope) -> None:
    sim_scope.configure(timebase="1ms")
    results: list[tuple[int, Waveform]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(9)

    def client(i: int) -> None:
        start.wait(WAIT_S)
        for k in range(15):
            try:
                if k % 5 == 0:
                    sim_scope.configure(volts_per_div=(0.5, 1, 2)[(i + k) % 3])
                fut = sim_scope.acquire_async(timeout_s=1.0)
                if k % 7 == 3:
                    fut.cancel()
                else:
                    wf = fut.result(WAIT_S)
                    with lock:
                        results.append((i, wf))
                _ = sim_scope.status
                _ = sim_scope.store.snapshot()
            except (Cancelled, AcquisitionTimeout):
                pass
            except BaseException as exc:
                with lock:
                    errors.append(exc)

    def toggler() -> None:
        start.wait(WAIT_S)
        for _ in range(10):
            sim_scope.run()
            time.sleep(0.003)
            sim_scope.stop()

    threads = [threading.Thread(target=client, args=(i,)) for i in range(8)]
    threads.append(threading.Thread(target=toggler))
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert not errors, errors
    assert len(results) > 40
    for i in range(8):  # each client's waveforms are newer than its previous one
        seqs = [w.seq for j, w in results if j == i]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert sim_scope.worker.thread.is_alive()
    sim_scope.stop()
    wf = sim_scope.acquire()
    assert wf.seq == sim_scope.status.last_seq
    assert sim_scope.status.state == "idle"


def test_concurrent_close_while_acquiring() -> None:
    # real-time sim + worker clock: nothing times out. (A virtual-clock sim under a real-time
    # worker would exhaust the worker's clock-independent wait budget in simulated time, H2.)
    sim = SimTransport(seed=2, clock=time.monotonic)
    sim.device.never_trigger = True
    scope = Scope.open(sim)
    scope.configure(trigger_mode="normal")
    outcomes: list[BaseException | Waveform] = []

    def client() -> None:
        try:
            outcomes.append(scope.acquire(timeout_s=30))
        except BaseException as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=client) for _ in range(6)]
    for t in threads:
        t.start()
    wait_until(lambda: scope.status.state == "armed", what="armed")
    closers = [threading.Thread(target=scope.close) for _ in range(3)]
    for t in closers:
        t.start()
    for t in threads + closers:
        t.join(WAIT_S)
    assert not any(t.is_alive() for t in threads + closers)
    assert len(outcomes) == 6 and all(isinstance(o, Cancelled) for o in outcomes)
    assert scope.closed and not scope.worker.thread.is_alive()


def test_connection_events_reach_subscribers(sim_scope: Scope) -> None:
    got: list[Any] = []
    sim_scope.subscribe(got.append)
    sim = sim_scope._transport
    assert isinstance(sim, SimTransport)
    sim.unplug()
    with pytest.raises(Exception):  # noqa: B017 - DeviceDisconnected
        sim_scope.acquire()
    wait_until(lambda: any(isinstance(e, ConnectionEvent) for e in got), what="disconnect")
    sim.replug()
    wait_until(lambda: sim_scope.connected, what="reconnect")
    conn = [e.connected for e in got if isinstance(e, ConnectionEvent)]
    assert conn == [False, True]
    assert sim_scope.acquire().seq == 1
