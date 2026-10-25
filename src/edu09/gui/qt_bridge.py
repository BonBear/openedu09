"""Thread marshalling between the :class:`~edu09.device.scope.Scope` and the Qt GUI thread.

``ScopeQtAdapter`` is the ONLY GUI object that subscribes to Scope events. Scope callbacks run on
the acquisition worker thread; the adapter only stores/forwards them through *queued* Qt signals,
so every public signal is emitted on the GUI thread (architecture.md 3.1, R4).

Waveforms are coalesced (latest wins): however fast the worker publishes, at most one waveform
delivery is queued in the GUI event loop at any time; the plot additionally throttles redraws.

Blocking Scope calls (``configure``, ``run``, ``stop``, auto-set, calibration, MCP server start)
never run on the GUI thread: :meth:`ScopeQtAdapter.call` executes them on a single-threaded
command executor (keeps the user's commands in order) and :meth:`ScopeQtAdapter.call_background`
on a small pool for long jobs. Results come back on the GUI thread via callbacks.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot

from edu09.device.scope import Scope
from edu09.device.worker import ConnectionEvent, ErrorEvent, Event, SettingsEvent, StateEvent
from edu09.model import Waveform

__all__ = ["GUI_SOURCE", "ScopeQtAdapter"]

log = logging.getLogger(__name__)

T = TypeVar("T")

GUI_SOURCE = "gui"
"""``source`` tag of settings changes made by this GUI (``SettingsEvent.source``)."""


class ScopeQtAdapter(QObject):
    """Turns Scope callbacks into queued Qt signals and runs Scope calls off the GUI thread."""

    #: latest Waveform (coalesced)
    waveformReceived = Signal(object)
    #: ConnectionEvent
    connectionChanged = Signal(object)
    #: SettingsEvent
    settingsChanged = Signal(object)
    #: StateEvent
    stateChanged = Signal(object)
    #: ErrorEvent (continuous-run capture failures)
    errorOccurred = Signal(object)
    #: (what: str, exc: BaseException) for failed calls without their own error handler
    commandFailed = Signal(str, object)

    # internal, always connected with Qt.QueuedConnection to a slot of this (GUI-thread) object
    _eventPosted = Signal(object)
    _wavePosted = Signal()
    _callPosted = Signal(object)

    def __init__(self, scope: Scope, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scope = scope
        self._lock = threading.Lock()
        self._pending_wave: Waveform | None = None
        self._closed = False
        self.waveforms_received = 0
        self.waveforms_coalesced = 0
        self._eventPosted.connect(self._deliver_event, Qt.ConnectionType.QueuedConnection)
        self._wavePosted.connect(self._deliver_wave, Qt.ConnectionType.QueuedConnection)
        self._callPosted.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)
        self._cmd = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edu09-gui-cmd")
        self._bg = ThreadPoolExecutor(max_workers=2, thread_name_prefix="edu09-gui-bg")
        self._unsubscribe: Callable[[], None] | None = scope.subscribe(self._on_scope_event)

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def closed(self) -> bool:
        return self._closed

    # -- worker thread side ------------------------------------------------------------------
    def _on_scope_event(self, ev: Event) -> None:
        """Scope subscriber: runs on the worker thread; must stay cheap and never block."""
        with self._lock:
            if self._closed:
                return
            if isinstance(ev, Waveform):
                self.waveforms_received += 1
                post = self._pending_wave is None
                if not post:
                    self.waveforms_coalesced += 1
                self._pending_wave = ev
                if post:
                    self._wavePosted.emit()
                return
            self._eventPosted.emit(ev)

    # -- GUI thread side ---------------------------------------------------------------------
    @Slot()
    def _deliver_wave(self) -> None:
        with self._lock:
            wf, self._pending_wave = self._pending_wave, None
            closed = self._closed
        if wf is not None and not closed:
            self.waveformReceived.emit(wf)

    @Slot(object)
    def _deliver_event(self, ev: object) -> None:
        if self._closed:
            return
        if isinstance(ev, ConnectionEvent):
            self.connectionChanged.emit(ev)
        elif isinstance(ev, SettingsEvent):
            self.settingsChanged.emit(ev)
        elif isinstance(ev, StateEvent):
            self.stateChanged.emit(ev)
        elif isinstance(ev, ErrorEvent):
            self.errorOccurred.emit(ev)

    @Slot(object)
    def _run_posted(self, fn: object) -> None:
        if self._closed or not callable(fn):
            return
        try:
            fn()
        except Exception:
            log.exception("GUI callback raised")

    def post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (callable from any thread)."""
        with self._lock:
            if not self._closed:
                self._callPosted.emit(fn)

    # -- calls ------------------------------------------------------------------------------
    def watch(
        self,
        fut: Future[T],
        on_result: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        what: str = "command",
    ) -> Future[T]:
        """Deliver the outcome of ``fut`` to ``on_result``/``on_error`` on the GUI thread."""

        def done(f: Future[T]) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                if on_error is not None:
                    self.post(lambda: on_error(exc))
                else:
                    self.post(lambda: self.commandFailed.emit(what, exc))
            elif on_result is not None:
                res = f.result()
                self.post(lambda: on_result(res))

        fut.add_done_callback(done)
        return fut

    def call(
        self,
        fn: Callable[[], T],
        on_result: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        what: str = "command",
    ) -> Future[T]:
        """Run a (possibly blocking) Scope call on the ordered command thread."""
        return self._submit(self._cmd, fn, on_result, on_error, what)

    def call_background(
        self,
        fn: Callable[[], T],
        on_result: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        what: str = "task",
    ) -> Future[T]:
        """Run a long job (auto-set, calibration, MCP server start/stop) on the background pool."""
        return self._submit(self._bg, fn, on_result, on_error, what)

    def _submit(
        self,
        pool: ThreadPoolExecutor,
        fn: Callable[[], T],
        on_result: Callable[[T], None] | None,
        on_error: Callable[[BaseException], None] | None,
        what: str,
    ) -> Future[T]:
        if self._closed:
            fut: Future[T] = Future()
            fut.set_exception(RuntimeError("GUI adapter is shut down"))
            return fut
        return self.watch(pool.submit(fn), on_result, on_error, what)

    # convenience wrappers --------------------------------------------------------------------
    def configure(self, **partial: Any) -> Future[Any]:
        return self.call(
            lambda: self._scope.configure(source=GUI_SOURCE, **partial), what="configure"
        )

    def run(self) -> Future[None]:
        return self.call(self._scope.run, what="run")

    def stop(self) -> Future[None]:
        return self.call(self._scope.stop, what="stop")

    def single(
        self,
        timeout_s: float,
        on_result: Callable[[Waveform], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Future[Waveform]:
        """``Scope.acquire_async`` (non-blocking: it only queues a worker command)."""
        try:
            fut = self._scope.acquire_async(timeout_s)
        except Exception as exc:
            fut = Future()
            fut.set_exception(exc)
        return self.watch(fut, on_result, on_error, what="single")

    # -- lifecycle ---------------------------------------------------------------------------
    def shutdown(self, wait: bool = True) -> None:
        """Unsubscribe from the Scope and stop the executors. Idempotent. Does not close the
        Scope (its owner does)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending_wave = None
            unsub, self._unsubscribe = self._unsubscribe, None
        if unsub is not None:
            unsub()
        self._cmd.shutdown(wait=wait, cancel_futures=True)
        self._bg.shutdown(wait=wait, cancel_futures=True)
