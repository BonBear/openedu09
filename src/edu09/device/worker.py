"""AcquisitionWorker: the single thread that owns a ``Transport`` (architecture.md 3.1, 3.2).

Every other thread talks to the worker through a command queue; each command carries a
``concurrent.futures.Future``. Only this module's worker thread ever calls ``transport.open``,
``read``, ``write`` or ``close``.

States (:class:`WorkerState`)::

    DISCONNECTED -> IDLE -> ARMED -> READING -> IDLE       (+ CLOSED after shutdown)

``running`` is a separate flag meaning "re-arm after each frame".

Hardware sequencing (protocol.md section 11, HW-verified on firmware 1.01):

* Arm: ``0x0E`` config (only when the config bytes changed; no reply) -> ``0x09`` reset ->
  drain stale IN reports -> [offset settle] -> ``0x0B`` arm. Offset settle (HW-verified
  2026-10-24): when the sent config changed the offset DAC byte (``ofs1``) or the CH1 gain/coupling
  byte (or the device config is unknown), the worker waits until ``OFFSET_SETTLE_S``
  (``tables.OFFSET_DAC_SETTLE_S``, 20 ms) after that ``0x0E`` before arming; otherwise the first
  frame starts up to ~8 counts off. The wait reads (and discards) IN reports, so it runs on the
  worker clock with any transport. Unchanged configs (continuous running, the AUTO free-run
  fallback, which only clears the trigger flag) add no delay.
* While armed the device streams by itself: an all-zero waiting report (~33 ms realtime, ~2 ms
  ETS) while waiting for a trigger (none in free run) and ``64 C8 mm`` on completion. ``mm`` must
  equal ``units.expected_acq_mode(timebase)``, else ``ProtocolError``. Any other report while
  armed (for example the stale trailing report of a previous read-out) is ignored.
* Read-out: ``0x0A`` -> 1..3 leading ``64 C8 mm`` + 16 data reports (``FrameAssembler``) -> one
  trailing data-like report (``tables.READOUT_TRAILING_REPORTS``) that is read and discarded.
* Stop: ``0x08``. On an armed device the HW sends one more all-zero report up to one status tick
  later (H1: 22 ms; ``tables.STATUS_INTERVAL_S``, ETS ``STATUS_INTERVAL_ETS_S``), so every
  ``0x08``-while-armed path (AUTO fallback, stop, cancel, pause, live capture, resync, connect)
  goes through ``_stop_armed``, which reads until that report arrives or the tick +
  ``POST_STOP_MARGIN_S`` elapses. Defence in depth (R1): if it did not arrive, a read-out within
  ``POST_STOP_LATE_S`` that contains an all-zero report reads one extra report; if the device
  sent 16 + trailing + 1 reports, the (first) zero report was the late post-stop report and is
  dropped (a genuine all-zero block, i.e. clipped-low samples, is kept).

* ``0x0D`` is never sent (H2: it starts a ~1 ms status stream, no samples). Live levels for
  calibration come from free-run frames (:meth:`DeviceIO.live_samples`).
* Every wait loop is bounded twice (H2: a device that keeps streaming must never hang the worker):
  by an absolute deadline on the worker clock AND, independent of any clock, by the summed
  timeouts of empty reads plus a maximum report count (``_WaitBudget``, ``_Acq`` budget).

Trigger modes: NORMAL waits (a single fails with ``AcquisitionTimeout`` after
``1.5 x frame duration + timeout_s``; running waits forever). AUTO waits
``1.5 x frame + trigger.auto_timeout_s``, then stops and does one free-run capture with the
trigger disabled (``Waveform.triggered=False``). ETS timebases cannot free-run, so ETS + AUTO
behaves like NORMAL. NONE arms with the trigger disabled.
"""

from __future__ import annotations

import atexit
import contextlib
import enum
import logging
import queue
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, TypeVar

import numpy as np
import numpy.typing as npt

from edu09 import units
from edu09.errors import (
    AcquisitionTimeout,
    Cancelled,
    DeviceDisconnected,
    Edu09Error,
    JobAborted,
    ProtocolError,
)
from edu09.model import Coupling, Settings, Timebase, TriggerEdge, TriggerMode, Waveform
from edu09.protocol import commands, ets, reports, tables
from edu09.protocol.commands import HwConfig
from edu09.transport.base import Transport

__all__ = [
    "AcquisitionWorker",
    "ConnectionEvent",
    "DeviceIO",
    "ErrorEvent",
    "Event",
    "SettingsEvent",
    "StateEvent",
    "WorkerState",
    "decode_frame",
    "detect_trigger_index",
    "frame_duration_s",
    "public_state",
]

log = logging.getLogger(__name__)

T = TypeVar("T")

IDLE_QUEUE_TIMEOUT_S: Final = 0.05
"""Queue wait while idle/disconnected (bounds shutdown and reconnect latency)."""
TRAILING_READ_TIMEOUT_S: Final = 0.02
"""Wait for the trailing report after a frame (HW: it comes ~1 ms after the 16th report)."""
DRAIN_READ_TIMEOUT_S: Final = 0.003
"""Per-read timeout when draining stale reports before an arm."""
RESYNC_READ_TIMEOUT_S: Final = 0.02
DRAIN_MAX_REPORTS: Final = 64
VERSION_TIMEOUT_S: Final = 0.5
RUN_COMPLETE_SLACK_S: Final = 1.0
"""Extra time a free-run capture may take while running before it counts as failed."""
MAX_RUN_ERRORS: Final = 3
"""Consecutive failed captures after which continuous running stops (ErrorEvent each time)."""
TRIGGER_DEADLINE_FACTOR: Final = 1.5
"""R6: trigger deadline >= 1.5 x frame duration + timeout."""
POST_STOP_MARGIN_S: Final = 0.010
"""Wait for the post-``0x08`` all-zero report: one status tick + this margin."""
POST_STOP_LATE_S: Final = 2 * tables.STATUS_INTERVAL_S + POST_STOP_MARGIN_S
"""How long after an unanswered ``0x08`` a read-out still checks for a late post-stop report."""
ATEXIT_SHUTDOWN_TIMEOUT_S: Final = 5.0
MIN_REPORT_INTERVAL_S: Final = 0.0005
"""Clock-independent loop bound: a full-speed HID interrupt IN endpoint (bInterval 1) delivers at
most one report per ms; half that gives margin. A wait of ``t`` seconds reads at most
``t / MIN_REPORT_INTERVAL_S + DRAIN_MAX_REPORTS`` reports."""
FRAME_READOUT_TIMEOUT_S: Final = 1.0
"""Overall bound for one ``0x0A`` read-out (16 reports + leading/trailing, ~20 ms on the HW)."""
LIVE_TIMEBASE: Final = Timebase.US200
"""Default timebase of the free-run live capture (fast realtime: one frame = 6.6 ms)."""
LIVE_CAPTURE_TIMEOUT_S: Final = 1.0
"""Extra time (beyond 1.5 x frame duration) a free-run live capture may take to complete."""
OFFSET_SETTLE_S: Final[float] = tables.OFFSET_DAC_SETTLE_S
"""Wait between a ``0x0E`` that changed the offset/gain byte and the next ``0x0B`` (HW: ~20 ms)."""


# --------------------------------------------------------------------------------------------
# States and events
# --------------------------------------------------------------------------------------------
class WorkerState(enum.StrEnum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    ARMED = "armed"
    READING = "reading"
    CLOSED = "closed"


PublicState = Literal["idle", "running", "armed", "disconnected", "closed"]


def public_state(state: WorkerState, running: bool) -> PublicState:
    """The coarse state reported to users (MCP ``get_status``, GUI status bar)."""
    if state is WorkerState.CLOSED:
        return "closed"
    if state is WorkerState.DISCONNECTED:
        return "disconnected"
    if running:
        return "running"
    if state in (WorkerState.ARMED, WorkerState.READING):
        return "armed"
    return "idle"


@dataclass(frozen=True)
class ConnectionEvent:
    """Device (re)connected (``firmware`` set) or lost (``error`` set)."""

    connected: bool
    firmware: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SettingsEvent:
    """Settings applied by the worker. ``source`` tags the originator (e.g. "mcp", "gui")."""

    settings: Settings
    warnings: tuple[str, ...] = ()
    source: str | None = None


@dataclass(frozen=True)
class StateEvent:
    """The public state (``public_state``) or the running flag changed."""

    state: PublicState
    worker_state: WorkerState
    running: bool


@dataclass(frozen=True)
class ErrorEvent:
    """A continuous-run capture failed (single-shot failures go to their futures instead)."""

    error: BaseException
    running_stopped: bool = False

    @property
    def message(self) -> str:
        return f"{type(self.error).__name__}: {self.error}"


Event = Waveform | ConnectionEvent | SettingsEvent | StateEvent | ErrorEvent


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------
def frame_duration_s(tb: Timebase) -> float:
    """Wall time one capture needs at least (ETS needs one pass per sub-record)."""
    e = units.timebase_entry(tb)
    return tables.FRAME_SIZE * e.dt * tables.ETS_SUBRECORDS[e.mode]


def decode_frame(
    frame: bytes, acq_mode: int, settings: Settings, cal: units.Calibration
) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.float64]]:
    """Wire-order frame -> (time-ordered raw uint8, volts) (architecture.md 4.2).

    ``ets.deinterleave`` is selected by the *status* mode byte. GND coupling replaces the samples
    with the calibrated ground count (volts are exactly 0).
    """
    if len(frame) != tables.FRAME_SIZE:
        raise ProtocolError(f"frame must be {tables.FRAME_SIZE} bytes, got {len(frame)}")
    raw = np.frombuffer(ets.deinterleave(acq_mode, frame), dtype=np.uint8).copy()
    if settings.ch1.coupling is Coupling.GND:
        gnd = round(units.gnd_counts(settings.ch1, cal))
        raw[:] = min(max(gnd, tables.ADC_MIN), tables.ADC_MAX)
    volts = units.counts_to_volts(raw, settings.ch1, cal)
    return raw, volts


ETS_TRIGGER_SEARCH: Final = 8
"""Samples either side of the table trigger index searched for the real ETS trigger edge."""


def detect_trigger_index(
    raw: npt.NDArray[np.uint8], settings: Settings, cal: units.Calibration, table_index: int
) -> int | None:
    """Index of the first sample past the trigger level on the configured edge, near the table.

    HW (2026-10-25): the ETS trigger position varies by a few samples between sessions/signals
    (e.g. 252 vs the table 248), so the worker looks for the real crossing per ETS frame. Works in
    the raw count domain with the count the trigger byte encodes (``units.trigger_byte``), with
    the edge direction flipped for an inverted ADC. Searches ``table_index +- ETS_TRIGGER_SEARCH``
    and returns the crossing closest to ``table_index``, or ``None`` when there is none (or the
    input is GND-coupled, whose samples are synthetic).
    """
    if settings.ch1.coupling is Coupling.GND or raw.size < 2:
        return None
    b = units.trigger_byte(settings.trigger, settings.ch1, cal)
    level = float(tables.ADC_MAX - b if tables.TRIGGER_BYTE_INVERTED else b)
    falling = settings.trigger.edge is TriggerEdge.FALLING
    falling ^= cal.adc_inverted  # the edge is defined on the input voltage
    sig = raw.astype(np.float64)
    if falling:
        sig, level = -sig, -level
    lo = max(table_index - ETS_TRIGGER_SEARCH, 1)
    hi = min(table_index + ETS_TRIGGER_SEARCH, raw.size - 1)
    if lo > hi:
        return None
    idx = np.arange(lo, hi + 1)
    hits = idx[(sig[idx - 1] < level) & (sig[idx] >= level)]
    if hits.size == 0:
        return None
    return int(hits[np.argmin(np.abs(hits - table_index))])


def _set_result(fut: Future[Any], value: Any) -> None:
    if fut.done():
        return
    with contextlib.suppress(InvalidStateError):  # cancelled by the caller meanwhile
        fut.set_result(value)


def _set_exception(fut: Future[Any], exc: BaseException) -> None:
    if fut.done():
        return
    with contextlib.suppress(InvalidStateError):
        fut.set_exception(exc)


class _WaitBudget:
    """Bounds one wait loop twice: an absolute deadline on the worker ``clock`` AND, independent
    of any clock, the summed timeouts of empty reads (``waited``) and a report count cap."""

    def __init__(
        self, clock: Callable[[], float], timeout_s: float, max_reports: int | None = None
    ) -> None:
        self._clock = clock
        self.timeout_s = timeout_s
        self.deadline = clock() + timeout_s
        self.waited = 0.0
        self.reports = 0
        self.max_reports = (
            max_reports
            if max_reports is not None
            else int(timeout_s / MIN_REPORT_INTERVAL_S) + DRAIN_MAX_REPORTS
        )

    def expired(self) -> bool:
        return (
            self._clock() >= self.deadline
            or self.waited >= self.timeout_s
            or self.reports >= self.max_reports
        )

    def read_timeout(self, cap: float) -> float:
        """Timeout for the next read: at most ``cap``, at most what is left (but > 0)."""
        left = min(self.deadline - self._clock(), self.timeout_s - self.waited)
        return max(min(cap, left), 0.001)

    def account(self, report: bytes | None, timeout_s: float) -> None:
        if report is None:
            self.waited += timeout_s
        else:
            self.reports += 1


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------
@dataclass(eq=False)
class _Command:
    future: Future[Any] = field(default_factory=Future, kw_only=True)


@dataclass(eq=False)
class _Configure(_Command):
    settings: Settings
    warnings: tuple[str, ...] = ()
    source: str | None = None


@dataclass(eq=False)
class _SetCalibration(_Command):
    calibration: units.Calibration


@dataclass(eq=False)
class _Single(_Command):
    timeout_s: float
    trigger_mode: TriggerMode | None = None


@dataclass(eq=False)
class _Run(_Command):
    pass


@dataclass(eq=False)
class _Stop(_Command):
    pass


@dataclass(eq=False)
class _ReadVersion(_Command):
    pass


@dataclass(eq=False)
class _Exclusive(_Command):
    fn: Callable[[DeviceIO], Any]
    abort_event: threading.Event | None = None


@dataclass(eq=False)
class _Reconnect(_Command):
    pass


@dataclass(eq=False)
class _Shutdown(_Command):
    pass


_DEFERRED: Final = object()


@dataclass(eq=False)
class _Acq:
    """One capture in progress (a single shot, or one frame of continuous running)."""

    single: bool
    futures: list[Future[Waveform]]
    override: TriggerMode | None = None
    timeout_s: float = 2.0
    settings: Settings | None = None  # effective settings, set when armed
    phase: Literal["trigger", "freerun"] = "trigger"
    triggered: bool = True
    deadline: float = float("inf")
    # clock-independent bound (finite deadlines only): summed empty-read time and report count
    budget_s: float = float("inf")
    waited: float = 0.0
    reports: int = 0
    max_reports: int | None = None

    def over_budget(self) -> bool:
        return self.waited >= self.budget_s or (
            self.max_reports is not None and self.reports >= self.max_reports
        )


@dataclass(eq=False)
class _Waiter:
    """A single-shot request served from the continuous stream (architecture.md 3.1)."""

    future: Future[Waveform]
    min_seq: int
    deadline: float
    require_triggered: bool


# --------------------------------------------------------------------------------------------
# Raw device access for exclusive operations (calibration hook, T9)
# --------------------------------------------------------------------------------------------
class DeviceIO:
    """Raw device access handed to :meth:`AcquisitionWorker.exclusive` callbacks.

    Valid only during the callback, on the worker thread. Acquisition is paused for the
    duration; afterwards the worker re-sends the configuration and resumes. Opcodes are still
    gated by the transport.

    Abort (:meth:`AcquisitionWorker.abort_exclusive`, the command's ``abort_event``, or
    :meth:`AcquisitionWorker.shutdown`): every device call made after the abort raises
    :class:`~edu09.errors.JobAborted`; a call in progress raises it at its next transport read
    or write (its own cleanup, e.g. stopping an armed capture, still runs). The worker then
    resyncs (stops and resets the device) before it handles the next command.
    """

    def __init__(self, worker: AcquisitionWorker) -> None:
        self._w = worker
        self._valid = True

    def _check(self) -> AcquisitionWorker:
        if not self._valid:
            raise RuntimeError("DeviceIO used after its exclusive operation finished")
        if threading.current_thread() is not self._w._thread:
            raise RuntimeError("DeviceIO may only be used on the worker thread")
        if self._w._abort_requested():
            raise JobAborted(self._w._abort_reason())
        return self._w

    @property
    def aborted(self) -> bool:
        """True once the exclusive operation was asked to abort (it should return soon)."""
        return self._w._abort_requested()

    @property
    def settings(self) -> Settings:
        return self._w._settings

    @property
    def calibration(self) -> units.Calibration:
        return self._w._cal

    @property
    def firmware(self) -> str | None:
        return self._w._firmware

    def write(self, payload: bytes) -> None:
        self._check()._write(payload)

    def read(self, timeout_s: float) -> bytes | None:
        return self._check()._read(timeout_s)

    def drain(self, timeout_s: float = DRAIN_READ_TIMEOUT_S) -> int:
        """Discard pending IN reports; returns how many were discarded."""
        return self._check()._drain(timeout_s)

    def send_config(self, cfg: HwConfig) -> None:
        self._check()._send_config(cfg, force=True)

    def apply_settings(
        self, settings: Settings, calibration: units.Calibration | None = None
    ) -> HwConfig:
        """Send the config for ``settings`` (does not change the worker's stored settings)."""
        w = self._check()
        cfg = units.settings_to_hwconfig(settings, calibration or w._cal)
        w._send_config(cfg, force=True)
        return cfg

    def live_samples(
        self,
        n: int = tables.FRAME_SIZE,
        *,
        timebase: Timebase | str | float | None = None,
        timeout_s: float = LIVE_CAPTURE_TIMEOUT_S,
        settings: Settings | None = None,
        calibration: units.Calibration | None = None,
        settle_s: float = 0.0,
    ) -> bytes:
        """``n`` raw ADC samples (time order) from free-run frame capture(s).

        Trigger off, the worker's current V/div, offset and coupling bits, timebase ``timebase``
        (default ``LIVE_TIMEBASE``, 200 us/div; ETS timebases cannot free-run -> ``ValueError``).
        ``settings`` / ``calibration`` override the worker's for this capture only (calibration
        uses them to probe offset DAC values; the stored ones are not changed).
        Captures ``ceil(n / 1024)`` frames. Like every arm, the first one waits for the offset DAC
        to settle if the config changed the offset byte (``OFFSET_SETTLE_S``); ``settle_s``
        requests at least that much settling time after the config (first frame only).
        The worker's configuration is re-sent afterwards (also on errors). Raw counts, never
        volts; GND coupling is *not* substituted here.
        Replaces the original's ``0x0D`` live reports, which are status reports on firmware 1.01
        (H2). Raises ``AcquisitionTimeout`` / ``ProtocolError`` / ``DeviceDisconnected``.
        """
        w = self._check()
        if n < 1:
            raise ValueError("n must be >= 1")
        tb = Timebase.parse(timebase) if timebase is not None else LIVE_TIMEBASE
        if tb.is_ets:
            raise ValueError(
                f"timebase {tb.label} uses equivalent-time sampling and cannot free-run"
            )
        base = settings if settings is not None else w._settings
        cfg = units.settings_to_hwconfig(
            base.replace(timebase=tb, trigger_mode=TriggerMode.NONE),
            calibration if calibration is not None else w._cal,
        )
        cfg = dc_replace(cfg, flags=cfg.flags & ~tables.FLAG_TRIG_ENABLE)
        if settle_s < 0:
            raise ValueError("settle_s must be >= 0")
        limit = TRIGGER_DEADLINE_FACTOR * frame_duration_s(tb) + timeout_s
        out = bytearray()
        try:
            while len(out) < n:
                out += w._capture_freerun(
                    cfg, units.expected_acq_mode(tb), limit, min_settle_s=settle_s
                )
                settle_s = 0.0
        except BaseException:
            with contextlib.suppress(Exception):  # best effort; the original error wins
                w._restore_config()
            raise
        w._restore_config()
        return bytes(out[:n])

    def _invalidate(self) -> None:
        self._valid = False


# --------------------------------------------------------------------------------------------
# Interpreter exit: the worker thread is a daemon, so stop/close live workers from atexit
# --------------------------------------------------------------------------------------------
_live_workers: weakref.WeakSet[AcquisitionWorker] = weakref.WeakSet()
_live_lock = threading.Lock()


def _register_live(w: AcquisitionWorker) -> None:
    with _live_lock:
        _live_workers.add(w)


def _unregister_live(w: AcquisitionWorker) -> None:
    with _live_lock:
        _live_workers.discard(w)


@atexit.register
def _shutdown_live_workers() -> None:
    """Runs after non-daemon threads were joined (so other threads may use a Scope until then):
    sends ``0x08`` if armed and closes the transport; a Scope releases its owner lock on the
    final CLOSED state event."""
    with _live_lock:
        workers = list(_live_workers)
    for w in workers:
        try:
            w.shutdown(timeout_s=ATEXIT_SHUTDOWN_TIMEOUT_S)
        except Exception:  # never let one worker block the others / the exit
            log.exception("closing the EDU09 worker at exit failed")


# --------------------------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------------------------
class AcquisitionWorker:
    """Single acquisition thread for one device (see module docstring).

    ``on_event`` is called on the worker thread for every :data:`Event`; it must be cheap and
    must not block on the worker (exceptions are logged and swallowed). ``clock`` drives the
    acquisition deadlines (default ``time.monotonic``; pass the simulator's ``VirtualClock`` for
    deterministic tests). Reconnect retries always use real time.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        settings: Settings | None = None,
        calibration: units.Calibration | None = None,
        auto_reconnect: bool = True,
        reconnect_interval_s: float = 1.0,
        clock: Callable[[], float] | None = None,
        on_event: Callable[[Event], None] | None = None,
        name: str = "edu09-worker",
    ) -> None:
        if reconnect_interval_s <= 0:
            raise ValueError("reconnect_interval_s must be > 0")
        self._transport = transport
        self._settings = settings if settings is not None else Settings()
        self._cal = calibration if calibration is not None else units.Calibration()
        self._auto_reconnect = auto_reconnect
        self._reconnect_interval = reconnect_interval_s
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._on_event = on_event
        self._queue: queue.Queue[_Command] = queue.Queue()
        # daemon: an un-closed Scope must not keep the interpreter alive; the atexit hook
        # (_shutdown_live_workers) stops and closes the device instead
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._submit_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._closing = False
        self._shutdown_requested = False
        self._start_future: Future[str] = Future()
        self._shutdown_future: Future[None] = Future()
        # worker-thread state (read by other threads only through properties)
        self._state = WorkerState.DISCONNECTED
        self._running = False
        self._connected = False
        self._firmware: str | None = None
        self._seq = 0
        self._last_cfg: HwConfig | None = None
        self._current: _Acq | None = None
        self._pending: deque[_Single] = deque()
        self._waiters: list[_Waiter] = []
        self._run_errors = 0
        self._next_retry = 0.0
        self._last_public: tuple[PublicState, bool] | None = None
        self._started = False
        self._shutdown_cmds: list[Future[Any]] = []
        self._post_stop_until: float | None = None  # clock time; see _stop_armed
        self._settle_until: float | None = None  # clock time; see _send_config / _wait_settle
        # abort of exclusive operations (other threads set the flags under _abort_lock)
        self._abort_lock = threading.Lock()
        self._exclusive_cmd: _Exclusive | None = None  # running exclusive op (worker thread)
        self._abort_current = False  # abort_exclusive() hit the running op
        self._abort_all = False  # shutdown requested: abort the running and any later op
        self._abort_io_armed = False  # transport I/O raises JobAborted once (see _check_abort)

    # -- public, thread-safe ----------------------------------------------------------------
    def start(self) -> Future[str]:
        """Start the thread; the future resolves with the firmware version once connected, or
        with the connection error (the thread then exits)."""
        if self._started:
            return self._start_future
        self._started = True
        _register_live(self)
        self._thread.start()
        return self._start_future

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def is_worker_thread(self) -> bool:
        return threading.current_thread() is self._thread

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def firmware(self) -> str | None:
        return self._firmware

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def settings(self) -> Settings:
        """Settings last applied by the worker (``Scope.settings`` may be newer)."""
        return self._settings

    @property
    def calibration(self) -> units.Calibration:
        return self._cal

    @property
    def closed(self) -> bool:
        return self._state is WorkerState.CLOSED

    def snapshot(self) -> dict[str, Any]:
        """Consistent view of the worker status for other threads."""
        with self._status_lock:
            return {
                "state": self._state,
                "running": self._running,
                "connected": self._connected,
                "firmware": self._firmware,
                "last_seq": self._seq,
                "settings": self._settings,
                "calibration": self._cal,
            }

    def submit(self, cmd: _Command) -> Future[Any]:
        with self._submit_lock:
            if self._closing:
                _set_exception(cmd.future, Cancelled("the scope is closed"))
            else:
                self._queue.put(cmd)
        return cmd.future

    def configure(
        self, settings: Settings, warnings: tuple[str, ...] = (), source: str | None = None
    ) -> Future[Settings]:
        return self.submit(_Configure(settings, tuple(warnings), source))

    def set_calibration(self, cal: units.Calibration) -> Future[None]:
        return self.submit(_SetCalibration(cal))

    def single(
        self, timeout_s: float = 2.0, trigger_mode: TriggerMode | None = None
    ) -> Future[Waveform]:
        if not timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        return self.submit(_Single(float(timeout_s), trigger_mode))

    def run(self) -> Future[None]:
        return self.submit(_Run())

    def stop(self) -> Future[None]:
        return self.submit(_Stop())

    def read_version(self) -> Future[str]:
        return self.submit(_ReadVersion())

    def exclusive(
        self, fn: Callable[[DeviceIO], T], abort_event: threading.Event | None = None
    ) -> Future[T]:
        """Run ``fn(io)`` on the worker thread with acquisition paused (calibration hook).

        Setting ``abort_event`` (or :meth:`abort_exclusive`, :meth:`shutdown`) ends ``fn`` at
        its next device call with :class:`~edu09.errors.JobAborted` (see :class:`DeviceIO`)."""
        return self.submit(_Exclusive(fn, abort_event))

    def abort_exclusive(self) -> bool:
        """Abort the exclusive operation running right now (if any; queued ones are not
        affected). Thread-safe, non-blocking. Returns True if one was running."""
        with self._abort_lock:
            if self._exclusive_cmd is None:
                return False
            self._abort_current = True
            return True

    def live_level(
        self, n: int = tables.FRAME_SIZE, timebase: Timebase | str | float | None = None
    ) -> Future[float]:
        """Mean raw count of ``n`` free-run samples (:meth:`DeviceIO.live_samples`)."""
        if n < 1:
            raise ValueError("n must be >= 1")

        def _measure(io: DeviceIO) -> float:
            data = io.live_samples(n, timebase=timebase)
            return float(np.frombuffer(data, dtype=np.uint8).mean())

        return self.exclusive(_measure)

    def reconnect(self) -> Future[str]:
        """Try to reconnect now (useful with ``auto_reconnect=False``); resolves with firmware."""
        return self.submit(_Reconnect())

    def shutdown(self, timeout_s: float | None = 5.0) -> None:
        """Stop acquisition, fail pending futures with ``Cancelled``, close the transport, join."""
        if not self._started:
            self._closing = True
            self._state = WorkerState.CLOSED
            return
        with self._abort_lock:
            self._abort_all = True  # a long exclusive op (calibration) must not delay the close
        self.submit(_Shutdown())
        if not self.is_worker_thread():
            self._thread.join(timeout_s)
            if not self._thread.is_alive():
                _unregister_live(self)

    # -- events -----------------------------------------------------------------------------
    def _emit(self, ev: Event) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(ev)
        except BaseException:  # incl. SystemExit: it must not silently end the worker thread
            log.exception("event callback raised; ignored")

    def _set_state(self, state: WorkerState) -> None:
        with self._status_lock:
            self._state = state
        self._emit_state_if_changed()

    def _set_running(self, running: bool) -> None:
        with self._status_lock:
            self._running = running
        self._emit_state_if_changed()

    def _emit_state_if_changed(self) -> None:
        pub = (public_state(self._state, self._running), self._running)
        if pub != self._last_public:
            self._last_public = pub
            self._emit(StateEvent(pub[0], self._state, pub[1]))

    # -- low-level I/O (worker thread only) ---------------------------------------------------
    def _abort_requested(self) -> bool:
        cmd = self._exclusive_cmd
        if cmd is None:
            return False
        ev = cmd.abort_event
        return self._abort_current or self._abort_all or (ev is not None and ev.is_set())

    def _abort_reason(self) -> str:
        if self._abort_all:
            return "aborted: the scope is closing"
        return "aborted on request"

    def _check_abort(self) -> None:
        """In an exclusive op: raise ``JobAborted`` once at the first transport I/O after an
        abort; later I/O (the op's own cleanup, the resync) proceeds normally. DeviceIO entry
        points raise on every call (``DeviceIO._check``)."""
        if self._abort_io_armed and self._abort_requested():
            self._abort_io_armed = False
            raise JobAborted(self._abort_reason())

    def _write(self, payload: bytes) -> None:
        self._check_abort()
        try:
            self._transport.write(payload)
        except OSError as exc:
            raise DeviceDisconnected(f"write failed: {exc}") from exc

    def _read(self, timeout_s: float) -> bytes | None:
        self._check_abort()
        try:
            return self._transport.read(timeout_s)
        except OSError as exc:
            raise DeviceDisconnected(f"read failed: {exc}") from exc

    def _drain(self, timeout_s: float = DRAIN_READ_TIMEOUT_S) -> int:
        """Bounded by ``DRAIN_MAX_REPORTS`` reads and a worker-clock deadline."""
        n = 0
        deadline = self._clock() + DRAIN_MAX_REPORTS * timeout_s
        while n < DRAIN_MAX_REPORTS and self._read(timeout_s) is not None:
            n += 1
            if self._clock() >= deadline:
                break
        if n:
            log.debug("drained %d stale report(s)", n)
        return n

    def _resync(self) -> None:
        """Stop, reset and drain everything (after a protocol error or timeout)."""
        if self._state in (WorkerState.ARMED, WorkerState.READING):
            self._stop_armed()
        else:
            self._write(commands.encode_stop())
        self._write(commands.encode_reset())
        self._drain(RESYNC_READ_TIMEOUT_S)

    def _armed_is_ets(self) -> bool:
        acq = self._current
        s = acq.settings if acq is not None and acq.settings is not None else self._settings
        return s.timebase.is_ets

    def _stop_armed(self, ets: bool | None = None) -> bool:
        """Send ``0x08`` to an (possibly) armed device and consume its post-stop report.

        HW (protocol.md 11 finding 7): one all-zero report follows, on the status tick grid
        (up to ``STATUS_INTERVAL_S`` later; ETS ``STATUS_INTERVAL_ETS_S``). Reads until that
        report arrives (then drains briefly, for ETS waiting reports already in flight) or the
        tick + ``POST_STOP_MARGIN_S`` elapses. The window is measured on the worker clock and,
        independently, as the sum of timed-out reads (so a simulator on another clock also
        terminates). Returns True if the report was seen; otherwise a late one may still come
        and ``_read_frame`` checks for it until ``POST_STOP_LATE_S`` after the stop.
        """
        if ets is None:
            ets = self._armed_is_ets()
        interval = tables.STATUS_INTERVAL_ETS_S if ets else tables.STATUS_INTERVAL_S
        window = interval + POST_STOP_MARGIN_S
        self._write(commands.encode_stop())
        t0 = self._clock()
        waited = 0.0
        others = 0
        while others < DRAIN_MAX_REPORTS:
            remaining = window - max(self._clock() - t0, waited)
            if remaining <= 0:
                break
            timeout = min(remaining, tables.POLL_READ_TIMEOUT_S)
            r = self._read(timeout)
            if r is None:
                waited += timeout
            elif reports.is_waiting_report(r):
                self._drain(2 * interval if ets else DRAIN_READ_TIMEOUT_S)
                self._post_stop_until = None
                return True
            else:  # a completion status or stale trailing report: not the one we wait for
                others += 1
                log.debug("discarding %s while waiting for the post-stop report", r[:4].hex(" "))
        log.debug("no post-stop report within %.0f ms", window * 1e3)
        self._post_stop_until = t0 + POST_STOP_LATE_S
        return False

    def _send_config(self, cfg: HwConfig, force: bool = False) -> None:
        """Send ``0x0E`` if ``cfg`` differs from the last one sent (or ``force``).

        If the offset byte or the CH1 gain/coupling byte changed (or the device config was
        unknown), the next arm must wait for the offset DAC to settle (``_wait_settle``).
        """
        if force or cfg != self._last_cfg:
            prev = self._last_cfg
            self._write(commands.encode_config(cfg))
            self._last_cfg = cfg
            if prev is None or prev.ofs1 != cfg.ofs1 or prev.gain1 != cfg.gain1:
                self._request_settle(OFFSET_SETTLE_S)

    def _request_settle(self, seconds: float) -> None:
        """The next ``0x0B`` must not be sent before ``seconds`` from now (worker clock)."""
        if seconds <= 0:
            return
        until = self._clock() + seconds
        if self._settle_until is None or until > self._settle_until:
            self._settle_until = until

    def _wait_settle(self) -> None:
        """Wait (reading and discarding IN reports, like ``_drain``) until the requested offset
        settle time has passed. Bounded by the worker clock AND by summed read timeouts plus a
        report cap (``_WaitBudget``), so it terminates with any transport/clock pairing."""
        until, self._settle_until = self._settle_until, None
        if until is None:
            return
        remaining = until - self._clock()
        if remaining <= 0:
            return
        budget = _WaitBudget(self._clock, remaining)
        n = 0
        while not budget.expired():
            t = budget.read_timeout(tables.POLL_READ_TIMEOUT_S)
            r = self._read(t)
            budget.account(r, t)
            if r is not None:
                n += 1
        log.debug("offset settle: waited %.1f ms (%d report(s) discarded)", remaining * 1e3, n)

    def _read_reply(self, timeout_s: float, what: str) -> bytes:
        """First report that is neither a waiting report nor a completion status.

        H2: the deadline is checked after *every* read; skipping streamed status reports
        without checking it hung the worker on a device that kept streaming.
        """
        budget = _WaitBudget(self._clock, timeout_s)
        while not budget.expired():
            t = budget.read_timeout(0.05)
            r = self._read(t)
            budget.account(r, t)
            if r is not None and not (
                reports.is_waiting_report(r) or reports.is_complete_status(r)
            ):
                return r
        raise ProtocolError(f"no reply to {what} within {timeout_s:g} s")

    def _query_version(self) -> str:
        self._write(commands.encode_version_query())
        return reports.parse_version(self._read_reply(VERSION_TIMEOUT_S, "version query (0x0F)"))

    def _restore_config(self) -> None:
        self._send_config(units.settings_to_hwconfig(self._settings, self._cal), force=True)

    def _capture_freerun(
        self, cfg: HwConfig, expected_mode: int, timeout_s: float, min_settle_s: float = 0.0
    ) -> bytes:
        """Arm with ``cfg`` (trigger off), wait for ``64 C8 mm``, read the frame out; returns the
        time-ordered raw samples. Used by exclusive operations (state stays IDLE). On failure the
        device is stopped (``_stop_armed``) before the error propagates. The arm waits for the
        offset DAC to settle if needed, and at least ``min_settle_s`` after the config."""
        self._send_config(cfg, force=True)
        self._request_settle(min_settle_s)
        self._write(commands.encode_reset())
        self._drain()
        self._wait_settle()
        self._write(commands.encode_arm())
        done = False
        try:
            budget = _WaitBudget(self._clock, timeout_s)
            while True:
                if budget.expired():
                    raise AcquisitionTimeout(
                        f"free-run live capture did not complete within {timeout_s:.3g} s"
                    )
                t = budget.read_timeout(tables.POLL_READ_TIMEOUT_S)
                r = self._read(t)
                budget.account(r, t)
                if r is None or reports.is_waiting_report(r):
                    continue
                st = reports.parse_status(r)
                if st is None or not st.complete:
                    log.debug("ignoring non-status report while armed: %s", r[:8].hex(" "))
                    continue
                if st.mode != expected_mode:
                    raise ProtocolError(
                        f"status reports acquisition mode {st.mode}, expected {expected_mode}"
                    )
                break
            frame = self._read_frame(expected_mode)
            done = True
        finally:
            if not done:
                with contextlib.suppress(Edu09Error, OSError):
                    self._stop_armed(ets=False)
        return ets.deinterleave(expected_mode, frame)

    # -- thread main ------------------------------------------------------------------------
    def _main(self) -> None:
        try:
            try:
                fw = self._connect()
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    self._transport.close()
                _set_exception(self._start_future, exc)
                return
            _set_result(self._start_future, fw)
            while not self._shutdown_requested:
                try:
                    self._step()
                except (DeviceDisconnected, OSError) as exc:
                    self._on_disconnect(exc)
                except Exception as exc:
                    log.exception("unexpected error in acquisition worker")
                    self._on_internal_error(exc)
        finally:
            self._finish()

    def _step(self) -> None:
        st = self._state
        if st is WorkerState.DISCONNECTED:
            self._disconnected_step()
        elif st is WorkerState.ARMED:
            while not self._shutdown_requested:  # drain commands first
                cmd = self._get(0.0)
                if cmd is None:
                    break
                self._handle(cmd)
                if self._state is not WorkerState.ARMED:
                    return
            if self._shutdown_requested or self._state is not WorkerState.ARMED:
                return
            try:
                self._poll()
            except (ProtocolError, AcquisitionTimeout) as exc:
                self._acq_failed(exc)
        else:  # IDLE (READING never persists across steps)
            self._maybe_start()
            if self._state is WorkerState.IDLE:
                cmd = self._get(IDLE_QUEUE_TIMEOUT_S)
                if cmd is not None:
                    self._handle(cmd)

    def _get(self, timeout_s: float) -> _Command | None:
        try:
            if timeout_s <= 0:
                return self._queue.get_nowait()
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    # -- connection -------------------------------------------------------------------------
    def _connect(self) -> str:
        self._transport.open()
        try:
            self._last_cfg = None
            # idle: no reply; armed (stale session): one late all-zero report
            self._stop_armed(ets=False)
            self._write(commands.encode_reset())
            self._drain(RESYNC_READ_TIMEOUT_S)
            fw = self._query_version()
            self._send_config(units.settings_to_hwconfig(self._settings, self._cal))
        except BaseException:
            with contextlib.suppress(Exception):
                self._transport.close()
            raise
        with self._status_lock:
            self._firmware = fw
            self._connected = True
        self._run_errors = 0
        self._set_state(WorkerState.IDLE)
        self._emit(ConnectionEvent(True, firmware=fw))
        log.info("EDU09 connected, firmware %s", fw)
        return fw

    def _on_disconnect(self, exc: BaseException) -> None:
        with contextlib.suppress(Exception):
            self._transport.close()
        err = exc if isinstance(exc, DeviceDisconnected) else DeviceDisconnected(str(exc))
        log.warning("EDU09 disconnected: %s", err)
        self._fail_inflight(err)
        with self._status_lock:
            self._connected = False
        self._last_cfg = None
        self._next_retry = time.monotonic() + self._reconnect_interval
        self._set_state(WorkerState.DISCONNECTED)
        self._emit(ConnectionEvent(False, error=str(err)))

    def _disconnected_step(self) -> None:
        if self._auto_reconnect:
            wait = min(IDLE_QUEUE_TIMEOUT_S, max(0.0, self._next_retry - time.monotonic()))
        else:
            wait = IDLE_QUEUE_TIMEOUT_S
        cmd = self._get(wait) if wait > 0 else self._get(0.0)
        if cmd is not None:
            self._handle(cmd)
            return
        if self._auto_reconnect and time.monotonic() >= self._next_retry:
            self._try_reconnect()

    def _try_reconnect(self) -> str | None:
        try:
            return self._connect()
        except (Edu09Error, OSError) as exc:
            log.debug("reconnect failed: %s", exc)
            self._next_retry = time.monotonic() + self._reconnect_interval
            return None

    # -- command handling -------------------------------------------------------------------
    def _handle(self, cmd: _Command) -> None:
        if cmd.future.done() and not isinstance(
            cmd, _Shutdown | _Configure | _SetCalibration | _Stop
        ):
            # cancelled by the caller before we got to it. State-changing commands are applied
            # anyway: Scope.settings was already updated and a timed-out wait cancels the future
            return
        try:
            result = self._dispatch(cmd)
        except (DeviceDisconnected, OSError) as exc:
            _set_exception(cmd.future, exc)
            if self._state is not WorkerState.DISCONNECTED:
                raise
        except Exception as exc:
            _set_exception(cmd.future, exc)
            if isinstance(cmd, _Exclusive) and self._connected:
                self._resync()
        except BaseException as exc:  # e.g. SystemExit from an exclusive fn: never leave it pending
            _set_exception(cmd.future, exc)
            raise
        else:
            if result is not _DEFERRED:
                _set_result(cmd.future, result)

    def _dispatch(self, cmd: _Command) -> Any:
        connected = self._connected
        if isinstance(cmd, _Shutdown):
            self._shutdown_requested = True
            self._shutdown_cmds.append(cmd.future)
            return _DEFERRED  # resolved in _finish
        if isinstance(cmd, _Configure):
            return self._do_configure(cmd)
        if isinstance(cmd, _SetCalibration):
            with self._status_lock:
                self._cal = cmd.calibration
            self._reapply()
            return None
        if isinstance(cmd, _Stop):
            self._do_stop()
            return None
        if isinstance(cmd, _Reconnect):
            if connected:
                return self._firmware
            fw = self._connect()
            return fw
        if not connected:
            raise DeviceDisconnected("EDU09 is disconnected" + self._reconnect_hint())
        if isinstance(cmd, _Single):
            self._do_single(cmd)
            return _DEFERRED
        if isinstance(cmd, _Run):
            if not self._running:
                self._run_errors = 0
                self._set_running(True)
            return None
        if isinstance(cmd, _ReadVersion):
            if self._state is WorkerState.IDLE:
                fw = self._query_version()
                with self._status_lock:
                    self._firmware = fw
            return self._firmware
        if isinstance(cmd, _Exclusive):
            return self._do_exclusive(cmd)
        raise TypeError(f"unknown command {cmd!r}")

    def _reconnect_hint(self) -> str:
        return " (reconnecting)" if self._auto_reconnect else ""

    def _do_configure(self, cmd: _Configure) -> Settings:
        with self._status_lock:
            self._settings = cmd.settings
        self._emit(SettingsEvent(cmd.settings, cmd.warnings, cmd.source))
        self._reapply()
        return cmd.settings

    def _reapply(self) -> None:
        """Push settings/calibration to the device: re-arm if armed, else send the config."""
        if not self._connected:
            return
        self._run_errors = 0
        if self._state is WorkerState.ARMED and self._current is not None:
            self._arm(self._current)
        else:
            self._send_config(units.settings_to_hwconfig(self._settings, self._cal))

    def _do_single(self, cmd: _Single) -> None:
        if self._running:
            self._add_waiter(cmd)
        else:
            self._pending.append(cmd)

    def _add_waiter(self, cmd: _Single) -> None:
        s = self._settings
        budget = (
            2 * TRIGGER_DEADLINE_FACTOR * frame_duration_s(s.timebase)
            + s.trigger.auto_timeout_s
            + cmd.timeout_s
        )
        self._waiters.append(
            _Waiter(
                future=cmd.future,
                min_seq=self._seq,
                deadline=self._clock() + budget,
                require_triggered=cmd.trigger_mode is TriggerMode.NORMAL,
            )
        )

    def _do_stop(self) -> None:
        if self._state is WorkerState.ARMED and self._connected:
            self._stop_armed()
        cancelled = Cancelled("acquisition stopped")
        acq, self._current = self._current, None
        if self._connected:
            self._set_state(WorkerState.IDLE)
        self._set_running(False)
        if acq is not None:
            for f in acq.futures:
                _set_exception(f, cancelled)
        self._fail_pending(cancelled)

    def _do_exclusive(self, cmd: _Exclusive) -> Any:
        with self._abort_lock:
            self._exclusive_cmd = cmd
            self._abort_current = False
        try:
            if self._state is WorkerState.ARMED:  # pause; _current is re-armed afterwards
                self._stop_armed()
                self._set_state(WorkerState.IDLE)
            if self._abort_requested():  # aborted while queued (closing / event already set)
                raise JobAborted(self._abort_reason())
            io = DeviceIO(self)
            self._abort_io_armed = True
            try:
                return cmd.fn(io)
            finally:
                self._abort_io_armed = False
                io._invalidate()
                self._last_cfg = None  # unknown device config: resend before the next arm
        finally:
            with self._abort_lock:
                self._exclusive_cmd = None
                self._abort_current = False

    # -- acquisition --------------------------------------------------------------------------
    def _maybe_start(self) -> None:
        if self._state is not WorkerState.IDLE or not self._connected:
            return
        self._expire_waiters()
        if self._current is None:
            if self._running:
                while self._pending:
                    req = self._pending.popleft()
                    if not req.future.done():
                        self._add_waiter(req)
                self._current = _Acq(single=False, futures=[])
            else:
                while self._pending:
                    req = self._pending.popleft()
                    if req.future.done():
                        continue
                    self._current = _Acq(
                        single=True,
                        futures=[req.future],
                        override=req.trigger_mode,
                        timeout_s=req.timeout_s,
                    )
                    break
        if self._current is not None:
            self._arm(self._current)

    def _effective_settings(self, acq: _Acq) -> Settings:
        s = self._settings
        if acq.override is not None and acq.override is not s.trigger.mode:
            s = s.replace(trigger_mode=acq.override)
        return s

    def _arm(self, acq: _Acq, phase: Literal["trigger", "freerun"] = "trigger") -> None:
        s = self._effective_settings(acq)
        acq.settings = s
        acq.phase = phase
        mode = s.trigger.mode
        frame = TRIGGER_DEADLINE_FACTOR * frame_duration_s(s.timebase)
        cfg = units.settings_to_hwconfig(s, self._cal)
        if phase == "freerun":
            cfg = dc_replace(cfg, flags=cfg.flags & ~tables.FLAG_TRIG_ENABLE)
        self._send_config(cfg)
        self._write(commands.encode_reset())
        self._drain()  # stale trailing / stop / completion reports from the previous capture
        self._wait_settle()  # only if the config changed the offset/gain byte
        now = self._clock()  # deadlines count from the arm (after any settle wait)
        extra = acq.timeout_s if acq.single else RUN_COMPLETE_SLACK_S
        if phase == "freerun" or mode is TriggerMode.NONE:
            deadline = now + frame + extra
        elif mode is TriggerMode.AUTO and not s.timebase.is_ets:
            deadline = now + frame + s.trigger.auto_timeout_s
        else:  # NORMAL, or AUTO on an ETS timebase (no free-run fallback possible)
            deadline = now + frame + acq.timeout_s if acq.single else float("inf")
        self._write(commands.encode_arm())
        acq.triggered = bool(cfg.flags & tables.FLAG_TRIG_ENABLE)
        acq.deadline = deadline
        acq.waited = 0.0
        acq.reports = 0
        if deadline == float("inf"):  # running NORMAL: waits for a trigger until stopped
            acq.budget_s, acq.max_reports = float("inf"), None
        else:
            acq.budget_s = deadline - now
            acq.max_reports = int(acq.budget_s / MIN_REPORT_INTERVAL_S) + DRAIN_MAX_REPORTS
        self._set_state(WorkerState.ARMED)

    def _poll(self) -> None:
        acq = self._current
        if acq is None:  # defensive: nothing to wait for
            self._set_state(WorkerState.IDLE)
            return
        if acq.single and not self._running and all(f.done() for f in acq.futures):
            log.debug("single acquisition cancelled by the caller")
            self._stop_armed()
            self._current = None
            self._set_state(WorkerState.IDLE)
            return
        self._expire_waiters()
        r = self._read(tables.POLL_READ_TIMEOUT_S)
        if r is None:
            acq.waited += tables.POLL_READ_TIMEOUT_S
        else:
            acq.reports += 1
        if r is not None and not reports.is_waiting_report(r):
            st = reports.parse_status(r)
            if st is not None and st.complete:
                self._on_complete(acq, st)
                return
            log.debug("ignoring non-status report while armed: %s", r[:8].hex(" "))
        if self._clock() >= acq.deadline or acq.over_budget():
            self._on_deadline(acq)

    def _on_deadline(self, acq: _Acq) -> None:
        s = acq.settings
        assert s is not None
        mode = s.trigger.mode
        if acq.phase == "trigger" and mode is TriggerMode.AUTO and not s.timebase.is_ets:
            log.debug("AUTO: no trigger, free-run fallback")
            self._stop_armed(ets=False)
            self._arm(acq, phase="freerun")
            return
        if acq.phase == "trigger" and mode is not TriggerMode.NONE:
            ets_note = " (ETS timebases need a trigger; no free-run fallback)"
            raise AcquisitionTimeout(
                f"no trigger within {acq.timeout_s:g} s"
                + (ets_note if s.timebase.is_ets else "")
                + "; try trigger_mode='auto' or lower level_v"
            )
        raise AcquisitionTimeout(
            f"free-run capture did not complete in time (timebase {s.timebase.label}/div)"
        )

    def _on_complete(self, acq: _Acq, st: reports.Status) -> None:
        s = acq.settings
        assert s is not None
        expected = units.expected_acq_mode(s.timebase)
        if st.mode != expected:
            raise ProtocolError(
                f"status reports acquisition mode {st.mode}, expected {expected} for "
                f"{s.timebase.label}/div (timebase table mismatch)"
            )
        self._set_state(WorkerState.READING)
        frame = self._read_frame(expected)
        raw, volts = decode_frame(frame, st.mode, s, self._cal)
        trig_index = units.trigger_index(s.timebase)
        if acq.triggered and s.timebase.is_ets:
            found = detect_trigger_index(raw, s, self._cal, trig_index)
            log.debug("ETS trigger index: detected %s, table %d", found, trig_index)
            if found is not None:
                trig_index = found
        with self._status_lock:
            self._seq += 1
            seq = self._seq
        wf = Waveform(
            seq=seq,
            timestamp=datetime.now(UTC),
            raw=raw,
            volts=volts,
            dt=units.sample_interval(s.timebase),
            trigger_index=trig_index,
            settings=s,
            calibration_id=self._cal.calibration_id,
            acq_mode=st.mode,
            triggered=acq.triggered,
        )
        self._current = None
        self._run_errors = 0
        self._set_state(WorkerState.IDLE)
        self._publish(wf, acq)

    def _read_frame(self, expected_mode: int) -> bytes:
        """``0x0A`` -> 16 data reports -> trailing report(s); returns the wire-order frame.

        R1 defence (see the module docstring): while an unanswered ``0x08`` may still deliver
        its all-zero report late (``_post_stop_until``), that report can land anywhere in the
        read-out. Before the data starts, an all-zero report followed by a ``64 C8 mm`` leading
        status cannot be data (statuses never follow data) and is dropped. Inside the data, the
        device then sends one report more than 16 + trailing, so the reports are counted to tell
        it from genuine zero samples (clipped low). Zero reports are identical, so dropping the
        first one of a run gives the same frame wherever in the run the late report was.
        """
        late, self._post_stop_until = self._post_stop_until, None
        if late is not None and self._clock() >= late:
            late = None
        self._write(commands.encode_readout())
        asm = reports.FrameAssembler(expected_mode=expected_mode)
        held: bytes | None = None  # all-zero report before any data, while a late one may come
        budget = _WaitBudget(
            self._clock,
            FRAME_READOUT_TIMEOUT_S,
            max_reports=tables.REPORTS_PER_FRAME + asm.max_leading_status + 2,
        )
        while True:
            if budget.expired():
                raise ProtocolError(
                    f"frame read-out did not finish ({asm.reports_received}/"
                    f"{tables.REPORTS_PER_FRAME} reports, {budget.reports} read)"
                )
            r = self._read(tables.FRAME_READ_TIMEOUT_S)
            budget.account(r, tables.FRAME_READ_TIMEOUT_S)
            if r is None:
                raise ProtocolError(
                    f"frame read-out timed out after {asm.reports_received}/"
                    f"{tables.REPORTS_PER_FRAME} reports"
                )
            if late is not None and held is None and asm.reports_received == 0:
                if reports.is_waiting_report(r):
                    held = bytes(r)
                    continue
            elif held is not None:
                if reports.is_complete_status(r, expected_mode):
                    log.debug("dropping a late post-stop report before the read-out data")
                    late = None
                else:
                    asm.feed(held)
                held = None
            if asm.feed(r):
                break
        frame = asm.frame
        size = tables.REPORT_SIZE
        zero_at = -1
        if late is not None:
            zero_at = next(
                (
                    i
                    for i in range(tables.REPORTS_PER_FRAME)
                    if reports.is_waiting_report(frame[i * size : (i + 1) * size])
                ),
                -1,
            )
        extra: list[bytes] = []
        for _ in range(tables.READOUT_TRAILING_REPORTS + (1 if zero_at >= 0 else 0)):
            r = self._read(TRAILING_READ_TIMEOUT_S)  # HW: one stale report follows the frame
            if r is None:
                break
            extra.append(bytes(r))
        if zero_at >= 0 and len(extra) > tables.READOUT_TRAILING_REPORTS:
            log.debug("dropping a late post-stop report from frame report %d", zero_at)
            frame = frame[: zero_at * size] + frame[(zero_at + 1) * size :] + extra[0]
        return frame

    def _publish(self, wf: Waveform, acq: _Acq) -> None:
        self._emit(wf)  # subscribers (store) first, so "latest" is consistent with futures
        for f in acq.futures:
            _set_result(f, wf)
        if self._waiters:
            keep: list[_Waiter] = []
            for w in self._waiters:
                if w.future.done():
                    continue
                if (
                    wf.seq > w.min_seq
                    and wf.settings == self._settings
                    and (wf.triggered or not w.require_triggered)
                ):
                    _set_result(w.future, wf)
                else:
                    keep.append(w)
            self._waiters = keep

    def _expire_waiters(self) -> None:
        if not self._waiters:
            return
        now = self._clock()
        keep: list[_Waiter] = []
        for w in self._waiters:
            if w.future.done():
                continue
            if now >= w.deadline:
                _set_exception(
                    w.future,
                    AcquisitionTimeout("no matching frame from the running acquisition in time"),
                )
            else:
                keep.append(w)
        self._waiters = keep

    def _acq_failed(self, exc: BaseException) -> None:
        """Resync the device, go IDLE, then fail the capture's futures (single) or count the
        error (running; ``MAX_RUN_ERRORS`` in a row stop running)."""
        acq, self._current = self._current, None
        single = acq is not None and acq.single
        log.log(logging.INFO if single else logging.WARNING, "acquisition failed: %s", exc)
        try:
            self._resync()
        except BaseException:
            if acq is not None:
                for f in acq.futures:
                    _set_exception(f, exc)
            raise
        self._set_state(WorkerState.IDLE)
        if acq is None:
            return
        if single:
            for f in acq.futures:
                _set_exception(f, exc)
            return
        self._run_errors += 1
        stopped = self._run_errors >= MAX_RUN_ERRORS
        if stopped:
            self._set_running(False)
            self._fail_waiters(exc)
        self._emit(ErrorEvent(exc, running_stopped=stopped))

    def _on_internal_error(self, exc: BaseException) -> None:
        """A bug or unexpected exception: fail what is in flight, try to get back to IDLE."""
        acq, self._current = self._current, None
        if acq is not None:
            for f in acq.futures:
                _set_exception(f, exc)
        if self._running:
            self._run_errors += 1
            stopped = self._run_errors >= MAX_RUN_ERRORS
            self._emit(ErrorEvent(exc, running_stopped=stopped))
            if stopped:
                self._fail_waiters(exc)
                self._set_running(False)
        if self._connected:
            try:
                self._resync()
                self._set_state(WorkerState.IDLE)
            except (DeviceDisconnected, OSError) as dexc:
                self._on_disconnect(dexc)
            except Exception:
                log.exception("resync after internal error failed")
                self._set_state(WorkerState.IDLE)

    # -- failing futures --------------------------------------------------------------------
    def _fail_waiters(self, exc: BaseException) -> None:
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            _set_exception(w.future, exc)

    def _fail_pending(self, exc: BaseException) -> None:
        while self._pending:
            _set_exception(self._pending.popleft().future, exc)
        self._fail_waiters(exc)

    def _fail_inflight(self, exc: BaseException) -> None:
        acq, self._current = self._current, None
        if acq is not None:
            for f in acq.futures:
                _set_exception(f, exc)
        self._fail_pending(exc)

    # -- shutdown ---------------------------------------------------------------------------
    def _finish(self) -> None:
        with self._submit_lock:
            self._closing = True
        cancelled = Cancelled("the scope is closed")
        shutdown_futs: list[Future[Any]] = list(self._shutdown_cmds)
        while True:
            cmd = self._get(0.0)
            if cmd is None:
                break
            if isinstance(cmd, _Shutdown):
                shutdown_futs.append(cmd.future)
            else:
                _set_exception(cmd.future, cancelled)
        if self._connected:
            try:
                if self._state in (WorkerState.ARMED, WorkerState.READING):
                    self._write(commands.encode_stop())
                if tables.SEND_RELEASE_ON_CLOSE:
                    self._write(commands.encode_release())
            except Exception as exc:  # closing anyway
                log.debug("ignoring error while stopping on close: %s", exc)
        self._fail_inflight(cancelled)
        with contextlib.suppress(Exception):
            self._transport.close()
        with self._status_lock:
            self._connected = False
            self._running = False
        self._set_state(WorkerState.CLOSED)
        _set_exception(self._start_future, cancelled)  # no-op unless still pending
        _set_result(self._shutdown_future, None)
        for f in shutdown_futs:
            _set_result(f, None)
        _unregister_live(self)
