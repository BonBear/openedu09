"""Main window: plot + controls + measurements, menus, status bar, MCP toggle, CSV export.

Threading: the window only talks to the Scope through :class:`ScopeQtAdapter` (queued signals
in, executor-backed calls out). The only direct Scope accesses on the GUI thread are
non-blocking snapshots (``scope.settings``, ``scope.status``, ``scope.latest()``) and
``acquire_async`` (which only queues a worker command).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from edu09 import analysis, storage
from edu09.device.worker import (
    ConnectionEvent,
    ErrorEvent,
    SettingsEvent,
    StateEvent,
    WorkerState,
    frame_duration_s,
)
from edu09.errors import Cancelled, JobAborted
from edu09.gui.controls import ControlPanel
from edu09.gui.fmt import timebase_text, vdiv_text
from edu09.gui.plot import MeasurementPanel, ScopePlot
from edu09.gui.qt_bridge import GUI_SOURCE, ScopeQtAdapter
from edu09.model import Settings, TriggerMode, Waveform

__all__ = ["CloseChoice", "JobAborted", "MainWindow", "module_available"]

log = logging.getLogger(__name__)

MCP_SERVER_MODULE = "edu09.mcp_server.server"
AUTOSET_MODULE = "edu09.device.autoset"
CALIBRATION_MODULE = "edu09.device.calibration"
CALWIZARD_MODULE = "edu09.calwizard"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8765
SINGLE_TIMEOUT_S = 10.0
AUTOSET_TIMEOUT_S = 5.0
RENDER_INTERVAL_MS = 33  # ~30 fps
MEASURE_INTERVAL_MS = 250
MCP_STOP_TIMEOUT_S = 3.0  # bound for joining MCP server stops on close (GUI thread)
TRIGGER_WAIT_MIN_S = 0.5  # armed this long without a frame -> "WAITING FOR TRIGGER"

CloseChoice = str
"""``confirm_close_while_busy`` answer: ``"wait"``, ``"quit"`` or ``"cancel"``."""

CALIBRATION_TEXT = (
    "Calibration measures the ground level and gain of every V/div range and overwrites the "
    "stored calibration (the previous file is backed up).\n\n"
    "DISCONNECT THE PROBE / INPUT SIGNAL from CH1 now.\n\n"
    "Is the input disconnected and do you want to calibrate?"
)


class _SessionAbort(threading.Event):
    """``_job_abort`` stand-in for the calibration wizard: ``set()`` also aborts the wizard's
    session (thread-safe, non-blocking), so the existing "quit anyway" path ends it."""

    def __init__(self, abort: Any) -> None:
        super().__init__()
        self._abort = abort

    def set(self) -> None:
        super().set()
        try:
            self._abort()
        except Exception:
            log.warning("could not abort the calibration session", exc_info=True)


def module_available(name: str) -> bool:
    """True if ``name`` can be imported (without importing it, where possible)."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class MainWindow(QMainWindow):
    """The EDU09 oscilloscope window."""

    #: emitted (on the GUI thread) after every redraw; for tests
    rendered = Signal(object)

    def __init__(
        self,
        adapter: ScopeQtAdapter,
        *,
        mcp_host: str = DEFAULT_MCP_HOST,
        mcp_port: int = DEFAULT_MCP_PORT,
        render_interval_ms: int = RENDER_INTERVAL_MS,
        measure_interval_ms: int = MEASURE_INTERVAL_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self.scope = adapter.scope
        self._mcp_host = mcp_host
        self._mcp_port = mcp_port
        self._mcp_handle: Any = None
        self._mcp_live: list[Any] = []  # every started, not yet stopped server handle
        self._mcp_starting = False
        self._busy = False
        self._busy_what: str | None = None
        self._job_abort: threading.Event | None = None
        self._quit_pending = False
        self._shut_down = False
        self._mcp_lock = threading.Lock()  # guards _mcp_live and _shut_down vs MCP start jobs
        self._mcp_stoppers: list[threading.Thread] = []
        self._waiting_trigger = False
        self._render_interval_s = render_interval_ms / 1000.0
        self._measure_interval_s = measure_interval_ms / 1000.0
        self._last_render = 0.0
        self._last_measure = 0.0
        self._pending: Waveform | None = None
        self._shown: Waveform | None = None
        self._shown_volts: npt.NDArray[np.float64] | None = None
        self._measure_dirty = False
        self._disconnected_since: float | None = None
        self.last_autoset: Any = None
        self.cal_wizard: Any = None
        #: where the calibration wizard saves (None: the scope's calibration file; sim scopes
        #: then only apply the result)
        self.calibration_wizard_path: Path | None = None
        self._last_log: tuple[str, float] = ("", 0.0)

        status = self.scope.status
        settings = status.settings
        self.setWindowTitle("EDU09 Oscilloscope" + (" (simulated)" if status.simulated else ""))

        # widgets
        self.plot = ScopePlot(settings)
        self.controls = ControlPanel(settings)
        self.measurements = MeasurementPanel()
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(4, 4, 4, 4)
        side_layout.addWidget(self.controls)
        side_layout.addWidget(self.measurements)
        side_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(260)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.plot)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        self.setCentralWidget(splitter)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_dock = QDockWidget("Messages", self)
        self.log_dock.setObjectName("messages")
        self.log_dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_view.setMaximumHeight(72)

        # status bar
        sb = self.statusBar()
        self.conn_label = QLabel()
        self.fw_label = QLabel()
        self.state_label = QLabel()
        self.owner_label = QLabel()
        self.owner_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (self.conn_label, self.fw_label, self.state_label, self.owner_label):
            sb.addPermanentWidget(w)

        self._build_menus()

        # timers
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render)
        self._measure_timer = QTimer(self)
        self._measure_timer.setSingleShot(True)
        self._measure_timer.timeout.connect(self._measure)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(1000)
        self._reconnect_timer.timeout.connect(self._update_conn_label)
        self._trigger_timer = QTimer(self)  # armed without frames -> waiting for trigger
        self._trigger_timer.setSingleShot(True)
        self._trigger_timer.timeout.connect(self._on_trigger_timeout)

        # wiring
        adapter.waveformReceived.connect(self._on_waveform)
        adapter.settingsChanged.connect(self._on_settings)
        adapter.stateChanged.connect(self._on_state)
        adapter.connectionChanged.connect(self._on_connection)
        adapter.errorOccurred.connect(self._on_error_event)
        adapter.commandFailed.connect(self._on_command_failed)
        self.controls.settingsRequested.connect(self._on_settings_requested)
        self.controls.runRequested.connect(self.run)
        self.controls.stopRequested.connect(self.stop)
        self.controls.singleRequested.connect(self.single)
        self.controls.autosetRequested.connect(self.auto_set)
        self.plot.levelDragged.connect(self.controls.request_level)
        self.plot.cursorsChanged.connect(self._update_cursor_readout)

        # initial state
        self._connected = status.connected
        self._firmware = status.firmware
        self._state = status.state
        self._running = status.running
        self.controls.set_running(status.running)
        self._apply_connected(status.connected)
        if not module_available(AUTOSET_MODULE):
            self.controls.set_autoset_available(False, "Auto-set is not available in this build")
            self.autoset_action.setEnabled(False)
            self.autoset_action.setToolTip("Auto-set is not available in this build")
        if not self._calibration_available():
            self.calibrate_action.setEnabled(False)
            self.calibrate_action.setToolTip("Calibration is not available in this build")
        if not module_available(MCP_SERVER_MODULE):
            self._disable_mcp("MCP server is not available (install the 'mcp' package)")
        self._update_labels()
        latest = self.scope.latest()
        if latest is not None:
            self._on_waveform(latest)
        self.resize(1200, 720)

    # -- menus ---------------------------------------------------------------------------------
    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        self.export_action = QAction("&Export CSV…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.triggered.connect(lambda: self.export_csv())
        file_menu.addAction(self.export_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        acq = mb.addMenu("&Acquire")
        self.run_action = QAction("&Run", self)
        self.run_action.setShortcut(QKeySequence("F5"))
        self.run_action.triggered.connect(self.run)
        self.stop_action = QAction("&Stop", self)
        self.stop_action.setShortcut(QKeySequence("F6"))
        self.stop_action.triggered.connect(self.stop)
        self.single_action = QAction("S&ingle", self)
        self.single_action.setShortcut(QKeySequence("F7"))
        self.single_action.triggered.connect(self.single)
        self.autoset_action = QAction("&Auto-set", self)
        self.autoset_action.triggered.connect(self.auto_set)
        for a in (self.run_action, self.stop_action, self.single_action, self.autoset_action):
            acq.addAction(a)

        view = mb.addMenu("&View")
        self.cursors_action = QAction("&Cursors", self, checkable=True)
        self.cursors_action.setShortcut(QKeySequence("Ctrl+K"))
        self.cursors_action.toggled.connect(self.plot.set_cursors_visible)
        self.despike_action = QAction("&Despike ETS frames (50/20/10 µs/div)", self)
        self.despike_action.setCheckable(True)
        self.despike_action.setToolTip(
            "Remove isolated outlier samples of equivalent-time frames (display + measurements)"
        )
        self.despike_action.toggled.connect(lambda _on: self._rerender())
        reset_action = QAction("&Reset zoom", self)
        reset_action.setShortcut(QKeySequence("Ctrl+0"))
        reset_action.triggered.connect(self.plot.reset_view)
        view.addAction(self.cursors_action)
        view.addAction(self.despike_action)
        view.addAction(reset_action)
        view.addAction(self.log_dock.toggleViewAction())

        tools = mb.addMenu("&Tools")
        self.mcp_action = QAction("&MCP server (HTTP)", self)
        self.mcp_action.setCheckable(True)
        self.mcp_action.setToolTip(
            f"Serve this scope to AI agents at http://{self._mcp_host}:{self._mcp_port}/mcp"
        )
        self.mcp_action.toggled.connect(self.set_mcp_enabled)
        tools.addAction(self.mcp_action)
        tools.addSeparator()
        self.calibrate_action = QAction("&Calibrate…", self)
        self.calibrate_action.setToolTip(
            "Calibration wizard: zero (input disconnected) and per-range gain against your "
            "multimeter"
        )
        self.calibrate_action.triggered.connect(lambda: self.open_calibration_wizard())
        tools.addAction(self.calibrate_action)

    # -- logging / notices -----------------------------------------------------------------------
    def notify(self, text: str, timeout_ms: int = 6000) -> None:
        self.statusBar().showMessage(text, timeout_ms)
        self.log_message(text)

    def log_message(self, text: str) -> None:
        now = time.monotonic()
        if text == self._last_log[0] and now - self._last_log[1] < 2.0:
            return  # collapse bursts (e.g. several settings changes by one agent call)
        self._last_log = (text, now)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {text}")

    # -- scope events ----------------------------------------------------------------------------
    def _on_waveform(self, wf: Waveform) -> None:
        self._pending = wf
        self._restart_trigger_watch()
        if self._render_timer.isActive():
            return
        wait = self._render_interval_s - (time.monotonic() - self._last_render)
        self._render_timer.start(max(0, int(wait * 1000)))

    def _render(self) -> None:
        wf, self._pending = self._pending, None
        if wf is None or self._shut_down:
            return
        self._last_render = time.monotonic()
        self._show(wf)

    def _rerender(self) -> None:
        if self._shown is not None:
            self._show(self._shown)

    def _show(self, wf: Waveform) -> None:
        volts = wf.volts
        if self.despike_action.isChecked() and wf.settings.timebase.is_ets:
            volts = analysis.despike(wf.volts)
        self._shown = wf
        self._shown_volts = volts
        self.plot.show_waveform(wf, volts)
        self._measure_dirty = True
        if not self._measure_timer.isActive():
            wait = self._measure_interval_s - (time.monotonic() - self._last_measure)
            self._measure_timer.start(max(0, int(wait * 1000)))
        self.rendered.emit(wf)

    def _measure(self) -> None:
        if not self._measure_dirty or self._shown is None or self._shown_volts is None:
            return
        self._measure_dirty = False
        self._last_measure = time.monotonic()
        try:
            self.measurements.update_measurements(self._shown_volts, self._shown.dt)
        except ValueError as exc:  # defensive: malformed frame
            log.warning("measurement failed: %s", exc)

    def _current_settings(self) -> Settings:
        """Latest requested settings (non-blocking snapshot)."""
        return self.scope.settings

    def _on_settings(self, ev: SettingsEvent) -> None:
        s = self._current_settings()
        self.controls.set_settings(s)
        self.plot.set_settings(s)
        self._restart_trigger_watch()
        for w in ev.warnings:
            self.log_message(f"Warning: {w}")
        if ev.source not in (None, GUI_SOURCE) and not self._busy:  # not our own auto-set
            who = ev.source.upper() if len(ev.source) <= 4 else ev.source
            self.notify(f"Settings changed by {who}")

    def _on_state(self, ev: StateEvent) -> None:
        self._state = ev.state
        self._running = ev.running
        self.controls.set_running(ev.running, pending=ev.state in ("armed", "reading"))
        self._restart_trigger_watch()
        self._update_labels()

    def _on_connection(self, ev: ConnectionEvent) -> None:
        self._apply_connected(ev.connected)
        if ev.connected:
            self._firmware = ev.firmware or self._firmware
            self.notify(f"Device connected (firmware {self._firmware})")
        else:
            self.notify(f"Device disconnected: {ev.error or 'unknown error'}")
        self._update_labels()

    def _on_error_event(self, ev: ErrorEvent) -> None:
        suffix = " (acquisition stopped)" if ev.running_stopped else ""
        self.notify(f"Acquisition error: {ev.message}{suffix}")

    def _on_command_failed(self, what: str, exc: BaseException) -> None:
        self.notify(f"{what} failed: {type(exc).__name__}: {exc}")
        if what == "configure":
            s = self._current_settings()
            self.controls.set_settings(s)
            self.plot.set_settings(s)

    def _apply_connected(self, connected: bool) -> None:
        self._connected = connected
        self.controls.set_connected(connected)
        for a in (self.run_action, self.stop_action, self.single_action):
            a.setEnabled(connected)
        self.autoset_action.setEnabled(connected and module_available(AUTOSET_MODULE))
        if connected:
            self._disconnected_since = None
            self._reconnect_timer.stop()
        else:
            if self._disconnected_since is None:
                self._disconnected_since = time.monotonic()
            self._reconnect_timer.start()
        self._update_conn_label()

    # -- acquisition-state indicator ---------------------------------------------------------------
    def _trigger_wait_s(self) -> float:
        """How long an armed capture may go without a frame before we call it "waiting"."""
        try:
            frame = frame_duration_s(self._current_settings().timebase)
        except Exception:  # defensive: unknown timebase
            frame = 0.0
        return max(TRIGGER_WAIT_MIN_S, 1.5 * frame + 0.3)

    def _restart_trigger_watch(self) -> None:
        """A frame / state / settings change arrived: restart the waiting-for-trigger watchdog."""
        self._waiting_trigger = False
        self._trigger_timer.stop()
        acquiring = self._running or self._state == "armed"
        if acquiring and self._current_settings().trigger.mode is not TriggerMode.NONE:
            self._trigger_timer.start(int(self._trigger_wait_s() * 1000))
        self._update_indicator()

    def _on_trigger_timeout(self) -> None:
        if self._shut_down:
            return
        st = self.scope.status  # non-blocking snapshot
        self._waiting_trigger = st.worker_state is WorkerState.ARMED and (
            st.running or st.state == "armed"
        )
        self._update_indicator()

    def _update_indicator(self) -> None:
        c = self.controls
        if self._busy_what is not None:
            text = self._busy_what.upper()
            if self._quit_pending:
                text += " — QUIT PENDING"
            c.set_indicator("busy", text)
        elif not self._connected:
            c.set_indicator("disconnected")
        elif self._waiting_trigger and (self._running or self._state == "armed"):
            c.set_indicator("waiting")
        elif self._running:
            c.set_indicator("running")
        elif self._state == "armed":
            c.set_indicator("single")
        else:
            c.set_indicator("stopped")

    # -- status bar --------------------------------------------------------------------------------
    def _update_conn_label(self) -> None:
        if self._connected:
            self.conn_label.setText("● Connected")
            self.conn_label.setStyleSheet("QLabel { color: #2e9d48; }")
        else:
            since = self._disconnected_since
            secs = 0 if since is None else int(time.monotonic() - since)
            self.conn_label.setText(f"○ Disconnected — reconnecting… ({secs} s)")
            self.conn_label.setStyleSheet("QLabel { color: #d1495b; }")

    def _update_labels(self) -> None:
        self._update_conn_label()
        self._update_indicator()
        self.fw_label.setText(f"FW {self._firmware}" if self._firmware else "FW —")
        self.state_label.setText(f"State: {self._state}")
        parts: list[str] = []
        st = self.scope.status
        if st.simulated:
            parts.append("Simulated device")
        elif st.owner is not None:
            parts.append(f"Owner: {st.owner.kind} (pid {st.owner.pid})")
        if self._mcp_handle is not None:
            parts.append(f"MCP: {self._mcp_handle.url}")
        elif self._mcp_starting:
            parts.append("MCP: starting…")
        self.owner_label.setText("   ".join(parts))

    # -- actions ---------------------------------------------------------------------------------
    def _on_settings_requested(self, partial: dict[str, Any]) -> None:
        self.adapter.configure(**partial)

    def run(self) -> None:
        self.adapter.run()

    def stop(self) -> None:
        self.adapter.stop()

    def single(self) -> None:
        def failed(exc: BaseException) -> None:
            if isinstance(exc, Cancelled):
                self.notify("Single acquisition cancelled")
            else:
                self.notify(f"Single failed: {type(exc).__name__}: {exc}")

        self.adapter.single(SINGLE_TIMEOUT_S, on_error=failed)

    def _set_busy(self, busy: bool, what: str | None = None) -> None:
        """Auto-set / calibration started (``what`` names it) or finished."""
        self._busy = busy
        self._busy_what = (what or "busy") if busy else None
        if not busy:
            self._job_abort = None
        self.controls.set_busy(busy)
        self.calibrate_action.setEnabled(not busy and self._calibration_available())
        self.autoset_action.setEnabled(
            not busy and self._connected and module_available(AUTOSET_MODULE)
        )
        self._update_indicator()
        if not busy and self._quit_pending and not self._shut_down:
            # "wait, then quit": the job is done now (re-checked: the user may cancel meanwhile)
            QTimer.singleShot(0, self, self._close_if_quit_pending)

    def _close_if_quit_pending(self) -> None:
        if self._quit_pending and not self._busy and not self._shut_down:
            self.close()

    @staticmethod
    def _calibration_available() -> bool:
        return module_available(CALIBRATION_MODULE) and module_available(CALWIZARD_MODULE)

    @property
    def busy_job(self) -> str | None:
        """Name of the running background job (``"auto-set"``, ``"calibration"``) or None."""
        return self._busy_what

    @property
    def quit_pending(self) -> bool:
        return self._quit_pending

    def auto_set(self) -> None:
        if self._busy:
            return
        was_running = self._running
        scope = self.scope

        def job() -> Any:
            mod: Any = importlib.import_module(AUTOSET_MODULE)
            result = mod.auto_set(scope, timeout_s=AUTOSET_TIMEOUT_S)
            if was_running and not scope.running:
                scope.run()
            return result

        def done(result: Any) -> None:
            self.last_autoset = result
            for line in getattr(result, "rationale", []) or []:
                self.log_message(f"Auto-set: {line}")
            s = getattr(result, "settings", None)
            summary = (
                f"{vdiv_text(float(s.ch1.volts_per_div))}, {timebase_text(s.timebase.seconds)}"
                if isinstance(s, Settings)
                else "done"
            )
            self.statusBar().showMessage(f"Auto-set: {summary}", 8000)
            self._set_busy(False)

        def failed(exc: BaseException) -> None:
            if isinstance(exc, ImportError):
                self.controls.set_autoset_available(False, f"Auto-set unavailable: {exc}")
                self.autoset_action.setEnabled(False)
            self.notify(f"Auto-set failed: {type(exc).__name__}: {exc}")
            self._set_busy(False)

        self._job_abort = threading.Event()  # auto-set is bounded; Scope.close cancels it
        self._set_busy(True, "auto-set")
        self.statusBar().showMessage("Auto-set running…")
        self.adapter.call_background(job, done, failed, what="auto-set")

    def confirm_calibration(self) -> bool:
        """Ask the user to disconnect the input (overridable in tests)."""
        answer = QMessageBox.warning(
            self,
            "Calibrate EDU09",
            CALIBRATION_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def calibrate(self) -> bool:
        """Confirm, then run ``Scope.calibrate`` in the background. Returns False if declined.

        "Quit anyway" on close aborts it through ``Scope.abort_exclusive`` (it ends with
        ``JobAborted`` at its next device call and saves nothing). Continuous acquisition is
        resumed afterwards if it was running.
        """
        if self._busy or not self.confirm_calibration():
            return False
        scope = self.scope
        was_running = self._running
        abort = threading.Event()

        def job() -> Any:
            try:
                return scope.calibrate(confirm=True, input_disconnected=True, save=True)
            finally:
                if was_running and not abort.is_set() and not scope.closed and not scope.running:
                    try:
                        scope.run()
                    except Exception:  # incl. TimeoutError, Edu09Error
                        log.warning("could not resume acquisition after calibration", exc_info=True)

        def done(cal: Any) -> None:
            self.notify(f"Calibration done (source: {getattr(cal, 'source', '?')})")
            self._set_busy(False)

        def failed(exc: BaseException) -> None:
            self.notify(f"Calibration failed: {type(exc).__name__}: {exc}")
            self._set_busy(False)

        self._job_abort = abort
        self._set_busy(True, "calibration")
        self.statusBar().showMessage("Calibrating… keep the input disconnected")
        self.adapter.call_background(job, done, failed, what="calibration")
        return True

    def open_calibration_wizard(self) -> Any:
        """Open the calibration wizard (window-modal, non-blocking). Returns it, or None if
        another job is running.

        While it is open the window is busy ("calibration"); "quit anyway" on close aborts the
        wizard's session. Continuous acquisition is resumed on close if it was running.
        """
        if self._busy:
            return None
        from edu09.gui.cal_wizard import CalibrationWizard

        was_running = self._running
        wiz = CalibrationWizard(
            self.scope, self.adapter, self, calibration_path=self.calibration_wizard_path
        )
        self.cal_wizard = wiz
        self._job_abort = _SessionAbort(wiz.abort_session)
        self._set_busy(True, "calibration")
        self.statusBar().showMessage("Calibration wizard open")
        wiz.finished.connect(lambda _result: self._on_wizard_closed(wiz, was_running))
        wiz.show()
        return wiz

    def _on_wizard_closed(self, wiz: Any, was_running: bool) -> None:
        if self.cal_wizard is wiz:
            self.cal_wizard = None
        if self._shut_down:
            return
        sess = getattr(wiz, "session", None)
        state = str(getattr(sess, "state", "not started"))
        saved = getattr(wiz.preview_page, "saved", None)
        if saved is not None:
            self.notify(f"Calibration saved ({saved.calibration_id}); wizard closed ({state})")
        else:
            self.notify(f"Calibration wizard closed ({state}); calibration unchanged")
        self._set_busy(False)
        if was_running and not self.scope.closed and not self.scope.running:
            self.adapter.run()

    def displayed_waveform(self) -> Waveform | None:
        return self._shown if self._shown is not None else self.scope.latest()

    def export_csv(self, path: str | Path | None = None) -> Path | None:
        """Export the displayed waveform; asks for a file name when ``path`` is None."""
        wf = self.displayed_waveform()
        if wf is None:
            self.notify("Nothing to export yet: acquire a waveform first")
            return None
        if path is None:
            stamp = wf.timestamp.astimezone().strftime("%Y%m%d-%H%M%S")
            default_dir = storage.default_export_dir()
            suggested = str(default_dir / f"edu09-{stamp}-{wf.waveform_id}.csv")
            chosen, _filter = QFileDialog.getSaveFileName(
                self, "Export waveform as CSV", suggested, "CSV files (*.csv)"
            )
            if not chosen:
                return None
            path = chosen
        target = Path(path)
        if target.suffix.lower() != ".csv":
            target = target.with_name(target.name + ".csv")
        try:
            written = storage.export_csv(wf, target)
        except OSError as exc:
            self.notify(f"Export failed: {exc}")
            return None
        self.notify(f"Exported {wf.waveform_id} ({wf.n} samples) to {written}")
        return written

    # -- MCP server --------------------------------------------------------------------------------
    @property
    def mcp_url(self) -> str | None:
        return None if self._mcp_handle is None else str(self._mcp_handle.url)

    def _disable_mcp(self, reason: str) -> None:
        self.mcp_action.blockSignals(True)
        self.mcp_action.setChecked(False)
        self.mcp_action.blockSignals(False)
        self.mcp_action.setEnabled(False)
        self.mcp_action.setToolTip(reason)

    def set_mcp_enabled(self, enabled: bool) -> None:
        """Start/stop the in-process MCP HTTP server (also the menu toggle's slot)."""
        if self.mcp_action.isChecked() != enabled:
            self.mcp_action.blockSignals(True)
            self.mcp_action.setChecked(enabled)
            self.mcp_action.blockSignals(False)
        if enabled:
            self._start_mcp()
        else:
            self._stop_mcp()

    def _start_mcp(self) -> None:
        if self._mcp_handle is not None or self._mcp_starting:
            return
        scope, host, port = self.scope, self._mcp_host, self._mcp_port

        def job() -> Any:
            server: Any = importlib.import_module(MCP_SERVER_MODULE)
            try:
                opts = server.ServerOptions(owner_kind="gui")
            except TypeError:
                opts = server.ServerOptions()
            # publishes the URL in owner.json itself (scope.set_mcp_url); stop() clears it
            handle = server.start_http_in_thread(scope, opts, host=host, port=port)
            with self._mcp_lock:
                closing = self._shut_down
                if not closing:
                    self._mcp_live.append(handle)  # lets shutdown() find it
            if closing:  # the window closed while we were starting: nobody else will stop it
                handle.stop()
            return handle

        def done(handle: Any) -> None:
            self._mcp_starting = False
            if self._shut_down or not self.mcp_action.isChecked():
                self._mcp_handle = handle
                self._stop_mcp()  # toggled off (or closing) while starting
                return
            self._mcp_handle = handle
            self.notify(f"MCP server listening at {handle.url}")
            self._update_labels()

        def failed(exc: BaseException) -> None:
            self._mcp_starting = False
            if isinstance(exc, ImportError):
                self._disable_mcp(f"MCP server unavailable: {exc}")
            else:
                self.mcp_action.blockSignals(True)
                self.mcp_action.setChecked(False)
                self.mcp_action.blockSignals(False)
            self.notify(f"MCP server failed to start: {type(exc).__name__}: {exc}")
            self._update_labels()

        self._mcp_starting = True
        self._update_labels()
        self.adapter.call_background(job, done, failed, what="MCP server start")

    def _stop_mcp(self, wait: bool = False, handle: Any = None) -> None:
        """Stop a server on its own (non-daemon, never cancelled) thread; ``wait`` joins it for
        at most :data:`MCP_STOP_TIMEOUT_S`. Not on the adapter's pool: shutting the pool down
        cancels queued jobs, which would leave the server running."""
        if handle is None:
            handle, self._mcp_handle = self._mcp_handle, None
        if handle is None:
            return
        with self._mcp_lock:
            if handle in self._mcp_live:
                self._mcp_live.remove(handle)
        notify = not wait and not self._shut_down

        def job() -> None:
            try:
                handle.stop()  # also clears the URL in owner.json
            except Exception:
                log.exception("stopping the MCP server failed")
                return
            if notify:
                self.adapter.post(lambda: self.notify("MCP server stopped"))

        t = threading.Thread(target=job, name="edu09-gui-mcp-stop", daemon=False)
        self._mcp_stoppers = [s for s in self._mcp_stoppers if s.is_alive()]
        self._mcp_stoppers.append(t)
        t.start()
        if wait:
            t.join(MCP_STOP_TIMEOUT_S)
        self._update_labels()

    def _join_mcp_stoppers(self, timeout_s: float = MCP_STOP_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        for t in list(self._mcp_stoppers):
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                log.warning("MCP server did not stop within %.1f s", timeout_s)

    # -- lifecycle ---------------------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop the MCP server(s) (bounded wait) and the adapter (no wait: a calibration or
        auto-set still running is cancelled by ``Scope.close``). Idempotent; never blocks on a
        background job. The Scope is closed by its owner."""
        if self._shut_down:
            return
        with self._mcp_lock:
            self._shut_down = True
            live = list(self._mcp_live)
        self._render_timer.stop()
        self._measure_timer.stop()
        self._reconnect_timer.stop()
        self._trigger_timer.stop()
        current, self._mcp_handle = self._mcp_handle, None
        if current is not None and current not in live:
            live.append(current)
        for handle in live:  # incl. starts that finished but were not delivered yet
            self._stop_mcp(handle=handle)
        self._join_mcp_stoppers()
        if self._job_abort is not None:
            self._job_abort.set()  # no resume of acquisition after the aborted job
            # "quit anyway": end a running calibration now (no save; the device is stopped).
            # Scope.close (by the owner) aborts any other exclusive job, e.g. one started over MCP
            try:
                self.scope.abort_exclusive()
            except Exception:
                log.warning("could not abort the running job", exc_info=True)
        wiz, self.cal_wizard = self.cal_wizard, None
        if wiz is not None:  # its session was aborted above; close it without asking
            wiz.closing = True
            wiz.reject()
        self.adapter.shutdown(wait=False)

    def confirm_close_while_busy(self, what: str) -> CloseChoice:
        """Ask what to do when the window is closed during ``what`` (overridable in tests).

        Returns ``"wait"`` (quit when it is done), ``"quit"`` (abort it and quit now) or
        ``"cancel"`` (keep the window open)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Quit EDU09")
        box.setText(f"{what.capitalize()} in progress.")
        box.setInformativeText(
            f"Wait for the {what} to finish and quit then, or quit now and abort it "
            "(the oscilloscope is stopped and released)?"
        )
        wait_b = box.addButton("Wait, then quit", QMessageBox.ButtonRole.AcceptRole)
        quit_b = box.addButton("Quit anyway", QMessageBox.ButtonRole.DestructiveRole)
        cancel_b = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(wait_b)
        box.setEscapeButton(cancel_b)
        box.exec()
        clicked = box.clickedButton()
        if clicked is wait_b:
            return "wait"
        if clicked is quit_b:
            return "quit"
        return "cancel"

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if not self._shut_down and self._busy:
            what = self._busy_what or "background job"
            choice = self.confirm_close_while_busy(what)
            if self._busy and choice != "quit":  # (the job may have ended during the dialog)
                event.ignore()
                if choice == "wait":
                    self._quit_pending = True
                    msg = f"Waiting for the {what} to finish, then quitting…"
                    self.statusBar().showMessage(msg)
                    self.log_message(msg)
                    self._update_indicator()
                elif self._quit_pending:
                    self._quit_pending = False
                    self.statusBar().showMessage("Quit cancelled", 4000)
                    self._update_indicator()
                return
            if self._busy:
                self.log_message(f"Quitting: aborting the {what}")
            elif choice == "cancel":
                event.ignore()
                return
        self.shutdown()
        super().closeEvent(event)

    def _update_cursor_readout(self) -> None:
        self.measurements.set_cursor_readout(
            self.plot.cursor_readout() if self.plot.cursors_visible() else None
        )
