"""H2 regression: a device that keeps streaming status reports must never hang the worker.

On hardware ``0x0D`` (sent by the old ``live_level``) switched the scope to a ~1 ms status stream;
the worker's reply loop skipped the reports without ever checking its deadline, the caller got a
TimeoutError after 20 s and ``close()`` logged "acquisition worker did not stop within 5.0 s".
Every wait loop is now bounded by the worker clock AND independently of it (summed read timeouts,
report count). These tests stream reports forever, with a frozen worker clock (only the
clock-independent bounds can end a loop) and with a clock that advances 1 ms per report.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from edu09.device.scope import Scope
from edu09.errors import Edu09Error
from edu09.model import Waveform
from edu09.protocol import commands, tables
from edu09.transport.base import TransportInfo
from edu09.transport.sim import SimTransport, VirtualClock

COMPLETE_1 = bytes([tables.STATUS_MARKER, tables.STATUS_COMPLETE, 1]) + bytes(61)
CALL_LIMIT_S = 8.0
"""Every Scope call must return well before its own outer timeout (>= 10 s)."""


class StreamingTransport:
    """A virtual sim that, once ``start_op`` was written after the connect sequence, answers every
    read with ``report`` immediately and forever (0x08 / 0x09 do not end it: worst case)."""

    def __init__(self, start_op: int, report: bytes, clock_step_s: float) -> None:
        self.connected = False
        self.inner = SimTransport.virtual("dc:1v", seed=1)
        self.info = TransportInfo(kind="sim", path="sim:streaming")
        self.start_op = start_op
        self.report = report
        self.clock_step_s = clock_step_s
        self.streaming = False
        self.reads = 0
        self.thread_ids: set[int] = set()

    @property
    def clock(self) -> VirtualClock:
        clock = self.inner.device.clock
        assert isinstance(clock, VirtualClock)
        return clock

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes) -> None:
        if payload[0] == self.start_op and self.connected:
            self.streaming = True
        self.inner.write(payload)

    def read(self, timeout_s: float) -> bytes | None:
        self.thread_ids.add(threading.get_ident())
        if not self.streaming:
            return self.inner.read(timeout_s)
        self.reads += 1
        self.clock.sleep(self.clock_step_s)
        return self.report


def _timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    t0 = time.monotonic()
    try:
        out: Any = fn()
    except Edu09Error as exc:  # an error is fine; hanging is not
        out = exc
    return time.monotonic() - t0, out


@pytest.mark.parametrize("report", [tables.WAITING_REPORT, COMPLETE_1], ids=["zeros", "complete"])
@pytest.mark.parametrize("start_op", [tables.OP_ARM, tables.OP_VERSION], ids=["arm", "version"])
@pytest.mark.parametrize("clock_kind", ["frozen", "advancing"])
def test_endless_status_stream_never_hangs_scope_calls_or_close(
    report: bytes, start_op: int, clock_kind: str
) -> None:
    t = StreamingTransport(start_op, report, 0.0 if clock_kind == "frozen" else 0.001)
    clock: Callable[[], float] = (lambda: 0.0) if clock_kind == "frozen" else t.clock
    scope = Scope.open(t, clock=clock, auto_reconnect=False)
    t.connected = True  # the connect sequence (incl. its 0x0F) is done
    try:
        scope.configure(timebase="1ms", volts_per_div=1, trigger_mode="normal")
        calls: list[tuple[str, Callable[[], Any]]] = [
            ("read_version", scope.read_version),
            ("acquire", lambda: scope.acquire(timeout_s=0.5)),
            ("acquire auto", lambda: scope.acquire(timeout_s=0.5, trigger_mode="auto")),
            ("live_level", scope.live_level),
            ("run_exclusive", lambda: scope.run_exclusive(lambda io: io.drain(), 5.0)),
            ("run", scope.run),
            ("stop", scope.stop),
            ("configure", lambda: scope.configure(volts_per_div=2)),
            ("acquire after", lambda: scope.acquire(timeout_s=0.5)),
        ]
        for name, fn in calls:
            elapsed, out = _timed(fn)
            assert elapsed < CALL_LIMIT_S, f"{name} took {elapsed:.1f} s ({out!r})"
            if isinstance(out, Waveform):  # all-zero stream read out as a (clipped) frame
                assert report == tables.WAITING_REPORT
        assert t.streaming and t.reads > 0
    finally:
        t0 = time.monotonic()
        scope.close(timeout_s=5.0)
        assert time.monotonic() - t0 < 5.0
    assert scope.closed and not scope.worker.thread.is_alive()


def test_live_level_never_sends_0x0d_and_restores_settings() -> None:
    sim = SimTransport.virtual("dc:1v", seed=3)
    with Scope.open(sim) as scope:
        scope.configure(timebase="2ms", volts_per_div=1, trigger_mode="auto", trigger_level_v=0.5)
        cfg_user = sim.device.config
        level = scope.live_level()
        assert level == pytest.approx(127 + 32, abs=1)  # 1 V at 1 V/div, uncalibrated
        assert scope.live_level(3000, timebase="5ms") == pytest.approx(127 + 32, abs=1)
        assert tables.OP_LIVE not in sim.device.received
        assert sim.device.config == cfg_user  # user's config re-sent afterwards
        with pytest.raises(ValueError, match="free-run"):
            scope.live_level(timebase="20us")
        wf = scope.acquire()  # dc never crosses the level: AUTO free-run fallback at 2 ms/div
        assert not wf.triggered and wf.settings.timebase.label == "2ms"
        assert wf.raw.mean() == pytest.approx(127 + 32, abs=1)


def test_device_left_in_fast_status_stream_by_an_exclusive_op_recovers() -> None:
    """A raw 0x0D (e.g. from a probe-like exclusive op) leaves the sim streaming every 1 ms."""
    sim = SimTransport.virtual("sine:1khz:2vpp", seed=5)
    with Scope.open(sim) as scope:
        scope.configure(timebase="1ms", trigger_mode="normal")

        def start_stream(io: Any) -> None:
            io.write(commands.encode_reset())
            io.write(commands.encode_arm())
            io.write(commands.encode_live())

        scope.run_exclusive(start_stream)
        assert scope.acquire(timeout_s=1.0).triggered
        assert scope.live_level() == pytest.approx(127, abs=3)
        scope.run_exclusive(start_stream)
        scope.run()
        n = scope.status.last_seq
        deadline = time.monotonic() + 10
        while scope.status.last_seq < n + 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert scope.status.last_seq >= n + 3
        scope.stop()
