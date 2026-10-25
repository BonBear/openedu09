"""ScopeQtAdapter: worker callbacks become GUI-thread signals; calls run off the GUI thread."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from edu09.device.scope import Scope
from edu09.device.worker import ConnectionEvent, SettingsEvent, StateEvent, WorkerState
from edu09.gui.qt_bridge import GUI_SOURCE, ScopeQtAdapter
from edu09.model import Settings, Waveform

pytestmark = pytest.mark.gui

WAIT_MS = 10_000


def make_wf(seq: int, settings: Settings | None = None, raw: Any = None) -> Waveform:
    r = np.full(1024, 128, dtype=np.uint8) if raw is None else np.asarray(raw, dtype=np.uint8)
    return Waveform(
        seq=seq,
        timestamp=datetime.now(UTC),
        raw=r,
        volts=(r.astype(float) - 127) / 32,
        dt=1 / 15625,
        trigger_index=255,
        settings=settings or Settings(),
        calibration_id="test",
    )


class FakeScope:
    """Minimal Scope stand-in that records which thread each call ran on."""

    def __init__(self) -> None:
        self.subscribers: list[Callable[[Any], None]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.settings = Settings()

    def subscribe(self, cb: Callable[[Any], None]) -> Callable[[], None]:
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)

    def _rec(self, name: str, **kw: Any) -> None:
        self.calls.append((name, threading.current_thread().name, kw))

    def configure(self, **kw: Any) -> Settings:
        self._rec("configure", **kw)
        return self.settings

    def run(self) -> None:
        self._rec("run")

    def stop(self) -> None:
        self._rec("stop")

    def emit(self, ev: Any) -> None:
        for cb in list(self.subscribers):
            cb(ev)


def from_thread(fn: Callable[[], None]) -> None:
    t = threading.Thread(target=fn, name="fake-worker")
    t.start()
    t.join(5)


def test_events_are_reemitted_on_gui_thread(qtbot: Any) -> None:
    fake = FakeScope()
    ad = ScopeQtAdapter(fake)  # type: ignore[arg-type]
    seen: list[tuple[str, bool]] = []
    main = threading.main_thread()

    def rec(kind: str) -> Callable[[Any], None]:
        return lambda _ev: seen.append((kind, threading.current_thread() is main))

    ad.connectionChanged.connect(rec("conn"))
    ad.settingsChanged.connect(rec("settings"))
    ad.stateChanged.connect(rec("state"))
    ad.waveformReceived.connect(rec("wave"))

    def produce() -> None:
        fake.emit(ConnectionEvent(False, error="x"))
        fake.emit(SettingsEvent(Settings(), (), "mcp"))
        fake.emit(StateEvent("idle", WorkerState.IDLE, False))
        fake.emit(make_wf(1))

    from_thread(produce)
    qtbot.waitUntil(lambda: len(seen) == 4, timeout=WAIT_MS)
    assert sorted(k for k, _ in seen) == ["conn", "settings", "state", "wave"]
    assert all(on_main for _, on_main in seen)
    ad.shutdown()


def test_waveforms_are_coalesced_latest_wins(qtbot: Any) -> None:
    fake = FakeScope()
    ad = ScopeQtAdapter(fake)  # type: ignore[arg-type]
    got: list[int] = []
    ad.waveformReceived.connect(lambda wf: got.append(wf.seq))
    from_thread(lambda: [fake.emit(make_wf(i)) for i in range(1, 201)])  # type: ignore[func-returns-value]
    qtbot.waitUntil(lambda: bool(got) and got[-1] == 200, timeout=WAIT_MS)
    assert ad.waveforms_received == 200
    assert len(got) + ad.waveforms_coalesced == 200
    assert len(got) < 200  # the GUI event loop never sees a backlog of frames
    assert got == sorted(got)
    ad.shutdown()


def test_calls_run_off_gui_thread_and_results_come_back_on_it(qtbot: Any) -> None:
    fake = FakeScope()
    ad = ScopeQtAdapter(fake)  # type: ignore[arg-type]
    results: list[bool] = []
    ad.configure(volts_per_div=0.5)
    ad.run()
    ad.call(
        lambda: 42,
        on_result=lambda r: results.append(
            threading.current_thread() is threading.main_thread() and r == 42
        ),
    )
    qtbot.waitUntil(lambda: bool(results), timeout=WAIT_MS)
    assert results == [True]
    assert [c[0] for c in fake.calls] == ["configure", "run"]
    assert all(c[1] != threading.main_thread().name for c in fake.calls)
    assert fake.calls[0][2] == {"source": GUI_SOURCE, "volts_per_div": 0.5}
    ad.shutdown()


def test_failed_call_emits_command_failed(qtbot: Any) -> None:
    fake = FakeScope()
    ad = ScopeQtAdapter(fake)  # type: ignore[arg-type]

    def boom() -> None:
        raise ValueError("bad value")

    with qtbot.waitSignal(ad.commandFailed, timeout=WAIT_MS) as blocker:
        ad.call(boom, what="configure")
    assert blocker.args[0] == "configure"
    assert isinstance(blocker.args[1], ValueError)
    ad.shutdown()


def test_shutdown_unsubscribes_and_drops_events(qtbot: Any) -> None:
    fake = FakeScope()
    ad = ScopeQtAdapter(fake)  # type: ignore[arg-type]
    assert len(fake.subscribers) == 1
    ad.shutdown()
    ad.shutdown()  # idempotent
    assert fake.subscribers == []
    fut = ad.call(lambda: 1)
    with pytest.raises(RuntimeError):
        fut.result(1)


def test_single_on_sim_scope_delivers_waveform(qtbot: Any, vscope: Scope) -> None:
    ad = ScopeQtAdapter(vscope)
    got: list[Waveform] = []
    with qtbot.waitSignal(ad.waveformReceived, timeout=WAIT_MS):
        ad.single(2.0, on_result=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=WAIT_MS)
    assert got[0].n == 1024
    ad.shutdown()
