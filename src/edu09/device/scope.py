"""Scope: the public, thread-safe facade over one :class:`AcquisitionWorker` (architecture.md 3.3).

Every method may be called from any thread. Blocking methods wait on the worker's futures;
``*_async`` variants return the future. Events (``Waveform``, ``ConnectionEvent``,
``SettingsEvent``, ``StateEvent``, ``ErrorEvent``) are delivered to :meth:`Scope.subscribe`
callbacks **on the worker thread**: callbacks must be cheap and must not call blocking Scope
methods (they raise ``RuntimeError`` there); non-blocking calls such as ``configure`` or ``stop``
are fine (they are queued).

Every waveform is also kept in :attr:`Scope.store` (a ``storage.WaveformStore``, last 32) before
any future resolves, so ``store.get("latest")`` is consistent with ``acquire()`` results.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, TypeVar

from edu09 import storage, units
from edu09.device.lock import OwnerInfo, OwnerLock
from edu09.device.worker import (
    FRAME_READOUT_TIMEOUT_S,
    LIVE_CAPTURE_TIMEOUT_S,
    LIVE_TIMEBASE,
    AcquisitionWorker,
    DeviceIO,
    Event,
    PublicState,
    StateEvent,
    WorkerState,
    frame_duration_s,
    public_state,
)
from edu09.errors import AcquisitionTimeout, CalibrationError, JobAborted, UnsafeOperation
from edu09.model import Settings, Timebase, TriggerMode, Waveform
from edu09.protocol import tables
from edu09.transport.base import Transport

__all__ = ["Scope", "ScopeStatus"]

log = logging.getLogger(__name__)

T = TypeVar("T")

COMMAND_TIMEOUT_S: Final = 10.0
"""Upper bound for waiting on non-acquisition commands (configure, run, stop, ...)."""
QUEUE_SLACK_S: Final = 10.0
"""Extra time ``acquire`` allows for commands queued ahead of it."""
CALIBRATION_MODULE: Final = "edu09.device.calibration"


@dataclass(frozen=True)
class ScopeStatus:
    """Snapshot for ``get_status`` / the GUI status bar."""

    connected: bool
    firmware: str | None
    state: PublicState
    worker_state: WorkerState
    running: bool
    settings: Settings
    last_seq: int
    calibration_source: str
    calibration_id: str
    transport_kind: str
    simulated: bool
    owner: OwnerInfo | None

    @property
    def last_waveform_id(self) -> str | None:
        return f"w{self.last_seq}" if self.last_seq > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "firmware": self.firmware,
            "state": self.state,
            "running": self.running,
            "settings": self.settings.to_dict(),
            "last_waveform_id": self.last_waveform_id,
            "calibration_source": self.calibration_source,
            "calibration_id": self.calibration_id,
            "transport": self.transport_kind,
            "simulated": self.simulated,
            "owner": (
                {"kind": self.owner.kind, "pid": self.owner.pid} if self.owner is not None else None
            ),
        }


def _default_transport() -> Transport:
    from edu09.transport.hid_transport import HidTransport

    return HidTransport()


def _virtual_clock_of(transport: Transport) -> Callable[[], float] | None:
    """The simulator's VirtualClock, so worker deadlines run in simulated time."""
    from edu09.transport.sim import SimTransport, VirtualClock

    if isinstance(transport, SimTransport) and isinstance(transport.device.clock, VirtualClock):
        return transport.device.clock
    return None


class Scope:
    """Thread-safe synchronous API for one EDU09. Create with :meth:`Scope.open`."""

    def __init__(
        self,
        transport: Transport,
        *,
        settings: Settings | None = None,
        calibration: units.Calibration | None = None,
        calibration_path: Path | None = None,
        auto_reconnect: bool = True,
        reconnect_interval_s: float = 1.0,
        clock: Callable[[], float] | None = None,
        owner_lock: OwnerLock | None = None,
        store: storage.WaveformStore | None = None,
    ) -> None:
        """Low-level constructor: does not start anything. Prefer :meth:`open`."""
        self._transport = transport
        self._settings = settings if settings is not None else Settings()
        self._settings_lock = threading.Lock()
        self._calibration_path = calibration_path
        self._owner_lock = owner_lock
        self.store = store if store is not None else storage.WaveformStore()
        self._subscribers: list[Callable[[Event], None]] = []
        self._subs_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        # abort events of calibrations in progress (Scope.calibrate); see abort_exclusive
        self._abort_events: set[threading.Event] = set()
        self._abort_events_lock = threading.Lock()
        self._job_local = threading.local()  # .abort_event: default for run_exclusive
        self._worker = AcquisitionWorker(
            transport,
            settings=self._settings,
            calibration=calibration,
            auto_reconnect=auto_reconnect,
            reconnect_interval_s=reconnect_interval_s,
            clock=clock,
            on_event=self._dispatch,
        )

    # -- lifecycle --------------------------------------------------------------------------
    @classmethod
    def open(
        cls,
        transport: Transport | None = None,
        *,
        auto_reconnect: bool = True,
        calibration: units.Calibration | str | os.PathLike[str] | None = None,
        settings: Settings | None = None,
        owner_kind: str = "other",
        mcp_url: str | None = None,
        use_lock: bool | None = None,
        lock_dir: str | os.PathLike[str] | None = None,
        reconnect_interval_s: float = 1.0,
        clock: Callable[[], float] | None = None,
        open_timeout_s: float = 10.0,
    ) -> Scope:
        """Open a device and start its worker; returns once connected (firmware read).

        ``transport=None`` opens the first attached EDU09 (``HidTransport``). ``calibration``
        is a ``Calibration``, a path to a calibration JSON file, or None: the default file for
        hardware transports (``storage.load_calibration``), the uncalibrated default for
        sim/replay. Hardware without a (usable) calibration file gets
        ``Calibration.typical()`` (HW-measured typical values; the ``Calibration()`` model's
        DAC slope of 2 is about twice the real one). Hardware transports take the
        inter-process :class:`OwnerLock` first (``use_lock=None`` = auto;
        ``DeviceBusy(owner=...)`` if another process owns it).
        A ``SimTransport`` on a ``VirtualClock`` drives the deadlines in simulated time unless
        ``clock`` is given.

        Raises ``DeviceNotFound`` / ``DeviceAccessError`` / ``DeviceBusy`` / ``ProtocolError``.
        """
        if transport is None:
            transport = _default_transport()
        is_hw = transport.info.kind == "hid"
        cal_path: Path | None = None
        cal: units.Calibration
        if isinstance(calibration, units.Calibration):
            cal = calibration
        elif calibration is not None:
            cal_path = Path(calibration)
            cal = (
                storage.load_calibration(cal_path)
                if cal_path.exists() or not is_hw
                else units.Calibration.typical()
            )
        elif is_hw:
            cal_path = storage.default_calibration_path()
            try:
                cal = (
                    storage.load_calibration(cal_path)
                    if cal_path.exists()
                    else units.Calibration.typical()
                )
            except CalibrationError as exc:
                log.warning("ignoring unusable calibration file: %s", exc)
                cal = units.Calibration.typical()
        else:
            cal = units.Calibration()
        if clock is None:
            clock = _virtual_clock_of(transport)
        lock: OwnerLock | None = None
        if use_lock if use_lock is not None else is_hw:
            lock = OwnerLock(owner_kind, mcp_url=mcp_url, lock_dir=lock_dir)
            lock.acquire()
        try:
            scope = cls(
                transport,
                settings=settings,
                calibration=cal,
                calibration_path=cal_path,
                auto_reconnect=auto_reconnect,
                reconnect_interval_s=reconnect_interval_s,
                clock=clock,
                owner_lock=lock,
            )
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        started = scope._worker.start()
        try:
            started.result(open_timeout_s)
        except BaseException as exc:  # timeout, connect error, KeyboardInterrupt, ...
            scope._abort_open()
            if isinstance(exc, concurrent.futures.TimeoutError):
                raise TimeoutError(f"device did not connect within {open_timeout_s:g} s") from None
            raise
        return scope

    def _abort_open(self, timeout_s: float = 5.0) -> None:
        """Failed ``open``: shut the worker down; release the owner lock only once the worker
        thread has exited (it may still be doing I/O), else on its final CLOSED state event."""
        with self._close_lock:
            self._closed = True
        self._worker.shutdown(timeout_s=timeout_s)
        if self._worker.thread.is_alive():
            log.warning("acquisition worker did not stop within %.1f s", timeout_s)
            return
        if self._owner_lock is not None:
            self._owner_lock.release()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop acquisition, cancel pending futures (``Cancelled``), close the device, release
        the owner lock. Idempotent. From a subscriber callback it only requests the shutdown."""
        with self._close_lock:
            first = not self._closed
            self._closed = True
        if first:
            self._set_abort_events()  # a calibration in progress never saves after this
            # the worker aborts a running exclusive operation (calibration, live_level) at its
            # next device call, stops/resyncs the device and then closes it
            self._worker.shutdown(timeout_s=timeout_s)
        if self._worker.is_worker_thread():
            return  # the owner lock is released by _dispatch on the final "closed" StateEvent
        if not first and self._worker.thread.is_alive():
            # closed concurrently / from a callback: still wait, so that "close() returned"
            # always means the device is closed and the owner lock is free
            self._worker.thread.join(timeout_s)
        if self._worker.thread.is_alive():
            # still doing I/O: keep the lock; _dispatch releases it on the "closed" StateEvent
            log.warning("acquisition worker did not stop within %.1f s", timeout_s)
            return
        if self._owner_lock is not None:
            self._owner_lock.release()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- status -----------------------------------------------------------------------------
    @property
    def status(self) -> ScopeStatus:
        snap = self._worker.snapshot()
        cal: units.Calibration = snap["calibration"]
        kind = self._transport.info.kind
        return ScopeStatus(
            connected=snap["connected"],
            firmware=snap["firmware"],
            state=public_state(snap["state"], snap["running"]),
            worker_state=snap["state"],
            running=snap["running"],
            settings=self.settings,
            last_seq=snap["last_seq"],
            calibration_source=cal.source,
            calibration_id=cal.calibration_id,
            transport_kind=kind,
            simulated=kind == "sim",
            owner=self._owner_lock.info() if self._owner_lock is not None else None,
        )

    @property
    def settings(self) -> Settings:
        """The latest requested settings (applied by the worker in submission order)."""
        with self._settings_lock:
            return self._settings

    @property
    def calibration(self) -> units.Calibration:
        return self._worker.calibration

    @property
    def firmware(self) -> str | None:
        return self._worker.firmware

    @property
    def connected(self) -> bool:
        return self._worker.connected

    @property
    def running(self) -> bool:
        return self._worker.running

    @property
    def owner_lock(self) -> OwnerLock | None:
        return self._owner_lock

    @property
    def worker(self) -> AcquisitionWorker:
        return self._worker

    def latest(self) -> Waveform | None:
        return self.store.latest()

    def set_mcp_url(self, url: str | None) -> None:
        """Publish the in-process MCP HTTP URL in ``owner.json`` (no-op without a lock)."""
        if self._owner_lock is not None:
            self._owner_lock.set_mcp_url(url)

    # -- events -----------------------------------------------------------------------------
    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        """Register ``cb`` for all events (called on the worker thread). Returns unsubscribe."""
        with self._subs_lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._subs_lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def _dispatch(self, ev: Event) -> None:
        if isinstance(ev, Waveform):
            self.store.add(ev)
        elif (
            isinstance(ev, StateEvent)
            and ev.worker_state is WorkerState.CLOSED
            and self._owner_lock is not None
        ):
            self._owner_lock.release()  # transport is closed by now (any close path)
        with self._subs_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(ev)
            except Exception:
                log.exception("subscriber callback raised; ignored")

    # -- helpers ----------------------------------------------------------------------------
    def _wait(self, fut: Future[T], timeout_s: float | None) -> T:
        if self._worker.is_worker_thread():
            raise RuntimeError(
                "blocking Scope call from a subscriber callback (worker thread) would deadlock"
            )
        try:
            return fut.result(timeout_s)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError(
                f"the acquisition worker did not answer within {timeout_s} s"
            ) from None

    def _wait_or_none(self, fut: Future[T]) -> T | None:
        """Wait unless called on the worker thread (then the command is just queued)."""
        if self._worker.is_worker_thread():
            return None
        return self._wait(fut, COMMAND_TIMEOUT_S)

    # -- settings ---------------------------------------------------------------------------
    def configure(self, *, source: str | None = None, **partial: Any) -> Settings:
        """Partial, atomic settings update with flat keys (``model.SETTINGS_KEYS``).

        Raises ``ValueError`` listing allowed values. ``None`` values mean unchanged.
        """
        return self.configure_with_warnings(source=source, **partial)[0]

    def configure_with_warnings(
        self, *, source: str | None = None, **partial: Any
    ) -> tuple[Settings, list[str]]:
        """Like :meth:`configure`, also returning coercion warnings (for MCP ``configure``)."""
        with self._settings_lock:
            new, warnings = self._settings.replace_with_warnings(**partial)
            self._settings = new
            fut = self._worker.configure(new, tuple(warnings), source)
        self._wait_or_none(fut)
        return new, warnings

    def set_settings(self, settings: Settings, *, source: str | None = None) -> Settings:
        """Replace all settings at once."""
        if not isinstance(settings, Settings):
            raise TypeError(f"expected Settings, got {type(settings).__name__}")
        with self._settings_lock:
            self._settings = settings
            fut = self._worker.configure(settings, (), source)
        self._wait_or_none(fut)
        return settings

    # -- acquisition ------------------------------------------------------------------------
    def acquire_async(
        self, timeout_s: float = 2.0, trigger_mode: TriggerMode | str | None = None
    ) -> Future[Waveform]:
        """Single shot. While running, the next new frame with the current settings is returned
        (NORMAL additionally requires a triggered frame). Cancel with ``future.cancel()`` or
        :meth:`stop` (-> ``Cancelled``)."""
        if not (isinstance(timeout_s, int | float) and timeout_s > 0):
            raise ValueError(f"timeout_s must be > 0, got {timeout_s!r}")
        mode = TriggerMode.parse(trigger_mode) if trigger_mode is not None else None
        if mode is TriggerMode.NONE and self.settings.timebase.is_ets:
            raise ValueError(
                f"timebase {self.settings.timebase.label} uses equivalent-time sampling and "
                "needs a trigger; allowed trigger modes: 'auto', 'normal'"
            )
        return self._worker.single(float(timeout_s), mode)

    def acquire(
        self, timeout_s: float = 2.0, trigger_mode: TriggerMode | str | None = None
    ) -> Waveform:
        """Blocking single shot. Raises ``AcquisitionTimeout``, ``Cancelled``,
        ``DeviceDisconnected`` or ``ProtocolError``."""
        fut = self.acquire_async(timeout_s, trigger_mode)
        s = self.settings
        outer = (
            timeout_s + 3 * frame_duration_s(s.timebase) + s.trigger.auto_timeout_s + QUEUE_SLACK_S
        )
        try:
            return self._wait(fut, outer)
        except TimeoutError:
            raise AcquisitionTimeout(f"no waveform within {outer:.3g} s") from None

    def run(self) -> None:
        """Continuous acquisition (re-arm after every frame)."""
        self._wait_or_none(self._worker.run())

    def stop(self) -> None:
        """Stop acquisition; pending single shots fail with ``Cancelled``."""
        self._wait_or_none(self._worker.stop())

    def set_running(self, running: bool) -> None:
        if running:
            self.run()
        else:
            self.stop()

    def read_version(self) -> str:
        """Firmware version (re-queried when idle, cached while acquiring)."""
        return self._wait(self._worker.read_version(), COMMAND_TIMEOUT_S)

    def reconnect(self) -> str:
        """Reconnect now (for ``auto_reconnect=False``); returns the firmware version."""
        return self._wait(self._worker.reconnect(), COMMAND_TIMEOUT_S)

    def live_level(
        self, n: int = tables.FRAME_SIZE, *, timebase: Timebase | str | float | None = None
    ) -> float:
        """Mean raw ADC count (never volts) of ``n`` samples from free-run frame capture(s).

        Trigger off, current V/div/offset, ``timebase`` (default 200 us/div, realtime only);
        the user's settings are restored afterwards. Used by calibration (T9). The original's
        ``0x0D`` live reports are status reports on firmware 1.01 (H2) and are never used.
        """
        if n < 1:
            raise ValueError("n must be >= 1")
        tb = Timebase.parse(timebase) if timebase is not None else LIVE_TIMEBASE
        if tb.is_ets:
            raise ValueError(f"timebase {tb.label} is ETS and cannot free-run; use a realtime one")
        frames = -(-n // tables.FRAME_SIZE)
        per_frame = 3 * frame_duration_s(tb) + LIVE_CAPTURE_TIMEOUT_S + FRAME_READOUT_TIMEOUT_S
        return self._wait(self._worker.live_level(n, tb), COMMAND_TIMEOUT_S + frames * per_frame)

    def run_exclusive(
        self,
        fn: Callable[[DeviceIO], T],
        timeout_s: float = 120.0,
        *,
        abort_event: threading.Event | None = None,
    ) -> T:
        """Run ``fn(io)`` on the worker with acquisition paused (calibration/autoset hook).

        The operation ends with ``JobAborted`` at its next device call once ``abort_event`` is
        set, :meth:`abort_exclusive` is called or the scope is closed (the device is stopped
        and reset afterwards). Inside :meth:`calibrate`, ``abort_event`` defaults to the
        calibration's own abort event.
        """
        if abort_event is None:
            abort_event = getattr(self._job_local, "abort_event", None)
        return self._wait(self._worker.exclusive(fn, abort_event), timeout_s)

    def abort_exclusive(self) -> bool:
        """Abort the running exclusive operation and any calibration in progress (thread-safe,
        non-blocking). An aborted calibration raises ``JobAborted`` and saves nothing; the
        device is stopped and acquisition resumes as before. Returns True if anything was
        running."""
        hit = self._set_abort_events()
        return self._worker.abort_exclusive() or hit

    def _set_abort_events(self) -> bool:
        with self._abort_events_lock:
            events = list(self._abort_events)
        for ev in events:
            ev.set()
        return bool(events)

    # -- calibration ------------------------------------------------------------------------
    def set_calibration(
        self,
        cal: units.Calibration,
        *,
        save: bool = False,
        path: str | os.PathLike[str] | None = None,
    ) -> Path | None:
        """Use ``cal`` from now on. With ``save``, back up the existing file
        (``storage.backup_calibration``) and write ``cal`` atomically; returns the backup path."""
        if not isinstance(cal, units.Calibration):
            raise TypeError(f"expected Calibration, got {type(cal).__name__}")
        backup: Path | None = None
        if save:
            if path is None and self._calibration_path is None:
                # sim/replay (or an explicit Calibration object): never fall through to the
                # real user calibration file (storage's default path)
                raise ValueError("this scope has no calibration file; pass path= to save")
            target = Path(path) if path is not None else self._calibration_path
            backup = storage.backup_calibration(target)
            storage.save_calibration(cal, target)
        self._wait_or_none(self._worker.set_calibration(cal))
        return backup

    def calibrate(
        self,
        *,
        confirm: bool,
        input_disconnected: bool,
        save: bool = True,
        path: str | os.PathLike[str] | None = None,
        abort_event: threading.Event | None = None,
    ) -> units.Calibration:
        """Opt-in calibration (architecture.md 5). Both flags must be True.

        The procedure itself lives in ``edu09.device.calibration`` (task T9), which must provide
        ``measure_calibration(scope: Scope) -> Calibration``.

        Abort: setting ``abort_event``, :meth:`abort_exclusive` or :meth:`close` (from another
        thread) ends the procedure at its next device call with ``JobAborted``; an aborted
        calibration is never installed or saved (an abort that arrives while the atomic save
        is already being written lets it complete).
        """
        if not (confirm is True and input_disconnected is True):
            raise UnsafeOperation(
                "calibration overwrites the stored calibration and needs the input disconnected; "
                "pass confirm=True and input_disconnected=True"
            )
        if save and path is None and self._calibration_path is None:
            raise ValueError("this scope has no calibration file; pass path= or save=False")
        try:
            mod: Any = importlib.import_module(CALIBRATION_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != CALIBRATION_MODULE:
                raise
            raise NotImplementedError("the calibration procedure is not implemented yet") from exc
        abort = abort_event if abort_event is not None else threading.Event()
        with self._abort_events_lock:
            self._abort_events.add(abort)
        if self._closed:
            abort.set()
        prev = getattr(self._job_local, "abort_event", None)
        self._job_local.abort_event = abort
        try:
            if abort.is_set():
                raise JobAborted("calibration aborted before it started")
            cal = mod.measure_calibration(self)
            if not isinstance(cal, units.Calibration):
                raise CalibrationError("calibration procedure returned no Calibration")
            if abort.is_set() or self._closed:
                raise JobAborted("calibration aborted; nothing was saved")
            self.set_calibration(cal, save=save, path=path)
        finally:
            self._job_local.abort_event = prev
            with self._abort_events_lock:
                self._abort_events.discard(abort)
        return cal

    def __repr__(self) -> str:
        st = self._worker.state
        return f"Scope({self._transport.info.kind}:{self._transport.info.path!r}, {st})"
