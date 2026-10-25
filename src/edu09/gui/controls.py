"""Scope control widgets: V/div, coupling, position, timebase, trigger, Run/Stop/Single/Auto-set.

The panel never talks to the Scope. User edits are emitted as ``settingsRequested(dict)`` with
flat ``model.SETTINGS_KEYS`` keys; :meth:`ControlPanel.set_settings` reflects the applied settings
with all widget signals blocked, so external updates (e.g. from the MCP server) never feed back
into another ``configure`` call.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from edu09.gui.fmt import timebase_text, vdiv_text
from edu09.model import (
    POSITION_DIV_RANGE,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    VoltsPerDiv,
)

__all__ = ["INDICATOR_STYLES", "ControlPanel"]

#: acquisition-state indicator: kind -> (default text, background, text colour). Solid colours
#: with explicit text colour, so the state is readable in light and dark themes alike.
INDICATOR_STYLES: dict[str, tuple[str, str, str]] = {
    "running": ("RUNNING", "#2e9d48", "#ffffff"),
    "stopped": ("STOPPED", "#c0392b", "#ffffff"),
    "waiting": ("WAITING FOR TRIGGER", "#f0b400", "#000000"),
    "single": ("SINGLE", "#f0b400", "#000000"),
    "busy": ("BUSY", "#3a7bd5", "#ffffff"),
    "disconnected": ("DISCONNECTED", "#7f8c8d", "#ffffff"),
}

_MODE_TEXT = {
    TriggerMode.AUTO: "Auto",
    TriggerMode.NORMAL: "Normal",
    TriggerMode.NONE: "Off (free run)",
}


def _combo(items: list[tuple[str, Any]], tooltip: str) -> QComboBox:
    box = QComboBox()
    for text, data in items:
        box.addItem(text, data)
    box.setToolTip(tooltip)
    return box


def _select(box: QComboBox, member: Any) -> None:
    """Select the item whose data is ``member.value`` (combo data are plain floats/strings)."""
    idx = box.findData(member.value)
    if idx >= 0:
        box.setCurrentIndex(idx)


class ControlPanel(QWidget):
    """Vertical/horizontal/trigger controls plus the acquisition buttons."""

    settingsRequested = Signal(dict)
    runRequested = Signal()
    stopRequested = Signal()
    singleRequested = Signal()
    autosetRequested = Signal()

    def __init__(self, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else Settings()
        self._connected = True
        self._running = False
        self._pending = False
        self._busy = False
        self._autoset_available = True

        # vertical
        self.vdiv = _combo(
            [(vdiv_text(float(v)), float(v)) for v in VoltsPerDiv],
            "Vertical sensitivity (CH1, x1 probe)",
        )
        self.coupling = _combo([(c.value.upper(), c.value) for c in Coupling], "Input coupling")
        self.position = QDoubleSpinBox()
        self.position.setRange(*POSITION_DIV_RANGE)
        self.position.setSingleStep(0.1)
        self.position.setDecimals(2)
        self.position.setSuffix(" div")
        self.position.setKeyboardTracking(False)
        self.position.setToolTip("Ground level position relative to the screen centre")
        vgroup = QGroupBox("Vertical (CH1)")
        vform = QFormLayout(vgroup)
        vform.addRow("Volts/div", self.vdiv)
        vform.addRow("Coupling", self.coupling)
        vform.addRow("Position", self.position)

        # horizontal
        self.timebase = _combo(
            [
                (timebase_text(float(tb)) + (" (ETS)" if tb.is_ets else ""), float(tb))
                for tb in Timebase
            ],
            "Time per division (50/20/10 µs/div use equivalent-time sampling and need a trigger)",
        )
        hgroup = QGroupBox("Horizontal")
        hform = QFormLayout(hgroup)
        hform.addRow("Time/div", self.timebase)

        # trigger
        self.trigger_mode = _combo(
            [(_MODE_TEXT[m], m.value) for m in TriggerMode],
            "Trigger mode (Auto falls back to free run)",
        )
        self.trigger_edge = _combo(
            [("Rising", TriggerEdge.RISING.value), ("Falling", TriggerEdge.FALLING.value)],
            "Trigger edge",
        )
        self.trigger_level = QDoubleSpinBox()
        self.trigger_level.setDecimals(3)
        self.trigger_level.setSuffix(" V")
        self.trigger_level.setKeyboardTracking(False)
        self.trigger_level.setToolTip("Trigger level (or drag the marker in the plot)")
        tgroup = QGroupBox("Trigger")
        tform = QFormLayout(tgroup)
        tform.addRow("Mode", self.trigger_mode)
        tform.addRow("Edge", self.trigger_edge)
        tform.addRow("Level", self.trigger_level)

        # acquisition
        self.state_indicator = QLabel()
        self.state_indicator.setObjectName("acquisitionState")
        self.state_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_indicator.setToolTip("Acquisition state")
        self._indicator_kind = ""
        self.set_indicator("stopped")
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Continuous acquisition")
        self.stop_button = QPushButton("Stop")
        self.single_button = QPushButton("Single")
        self.single_button.setToolTip("Acquire one frame")
        self.autoset_button = QPushButton("Auto-set")
        self.autoset_button.setToolTip("Choose V/div, timebase and trigger for the input signal")
        agroup = QGroupBox("Acquisition")
        agrid = QGridLayout(agroup)
        agrid.addWidget(self.state_indicator, 0, 0, 1, 2)
        agrid.addWidget(self.run_button, 1, 0)
        agrid.addWidget(self.stop_button, 1, 1)
        agrid.addWidget(self.single_button, 2, 0)
        agrid.addWidget(self.autoset_button, 2, 1)

        layout = QVBoxLayout(self)
        for g in (agroup, vgroup, hgroup, tgroup):
            layout.addWidget(g)
        layout.addStretch(1)

        self.set_settings(self._settings)
        self._update_enabled()

        self.vdiv.activated.connect(
            lambda _i: self._request(volts_per_div=float(self.vdiv.currentData()))
        )
        self.coupling.activated.connect(
            lambda _i: self._request(coupling=str(self.coupling.currentData()))
        )
        self.position.valueChanged.connect(lambda v: self._request(position_div=float(v)))
        self.timebase.activated.connect(
            lambda _i: self._request(timebase=float(self.timebase.currentData()))
        )
        self.trigger_mode.activated.connect(
            lambda _i: self._request(trigger_mode=str(self.trigger_mode.currentData()))
        )
        self.trigger_edge.activated.connect(
            lambda _i: self._request(trigger_edge=str(self.trigger_edge.currentData()))
        )
        self.trigger_level.valueChanged.connect(lambda v: self._request(trigger_level_v=float(v)))
        self.run_button.clicked.connect(self.runRequested)
        self.stop_button.clicked.connect(self.stopRequested)
        self.single_button.clicked.connect(self.singleRequested)
        self.autoset_button.clicked.connect(self.autosetRequested)

    # -- state from the scope ------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self._settings

    def set_settings(self, s: Settings) -> None:
        """Show ``s`` without emitting ``settingsRequested`` (no feedback loop)."""
        self._settings = s
        widgets = (
            self.vdiv,
            self.coupling,
            self.position,
            self.timebase,
            self.trigger_mode,
            self.trigger_edge,
            self.trigger_level,
        )
        blockers = [QSignalBlocker(w) for w in widgets]
        try:
            _select(self.vdiv, s.ch1.volts_per_div)
            _select(self.coupling, s.ch1.coupling)
            self.position.setValue(s.ch1.position_div)
            _select(self.timebase, s.timebase)
            _select(self.trigger_mode, s.trigger.mode)
            _select(self.trigger_edge, s.trigger.edge)
            lo, hi = s.level_range_v()
            vdiv = float(s.ch1.volts_per_div)
            self.trigger_level.setRange(lo, hi)
            self.trigger_level.setSingleStep(vdiv / 10)
            self.trigger_level.setValue(s.trigger.level_v)
            # free run is impossible on ETS timebases: grey the item out
            model: Any = self.trigger_mode.model()
            item = model.item(self.trigger_mode.findData(TriggerMode.NONE.value))
            if item is not None:
                item.setEnabled(not s.timebase.is_ets)
        finally:
            for b in blockers:
                b.unblock()
        self._update_enabled()

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._update_enabled()

    def set_running(self, running: bool, pending: bool = False) -> None:
        """``pending``: a Single is armed/waiting for its trigger (Stop must cancel it)."""
        self._running = running
        self._pending = pending
        self._update_enabled()

    @property
    def indicator_kind(self) -> str:
        return self._indicator_kind

    def set_indicator(self, kind: str, text: str | None = None) -> None:
        """Show the acquisition state (a key of :data:`INDICATOR_STYLES`; optional custom text)."""
        default, bg, fg = INDICATOR_STYLES[kind]
        if kind == self._indicator_kind and (text or default) == self.state_indicator.text():
            return  # called for every frame: skip the (costly) style sheet re-polish
        self._indicator_kind = kind
        self.state_indicator.setText(text or default)
        self.state_indicator.setStyleSheet(
            f"QLabel#acquisitionState {{ background-color: {bg}; color: {fg}; "
            "font-weight: bold; border-radius: 3px; padding: 4px; }"
        )

    def set_busy(self, busy: bool) -> None:
        """Auto-set / calibration in progress: block acquisition buttons."""
        self._busy = busy
        self._update_enabled()

    def set_autoset_available(self, available: bool, reason: str = "") -> None:
        self._autoset_available = available
        if not available and reason:
            self.autoset_button.setToolTip(reason)
        self._update_enabled()

    def _update_enabled(self) -> None:
        on = self._connected and not self._busy
        for w in (
            self.vdiv,
            self.coupling,
            self.position,
            self.timebase,
            self.trigger_mode,
            self.trigger_edge,
        ):
            w.setEnabled(on)
        self.trigger_level.setEnabled(on and self._settings.trigger.mode is not TriggerMode.NONE)
        self.trigger_edge.setEnabled(on and self._settings.trigger.mode is not TriggerMode.NONE)
        self.run_button.setEnabled(on and not self._running)
        self.stop_button.setEnabled(self._connected and (self._running or self._pending))
        self.single_button.setEnabled(on)
        self.autoset_button.setEnabled(on and self._autoset_available)

    # -- user input ------------------------------------------------------------------------------
    def _request(self, **partial: Any) -> None:
        self.settingsRequested.emit(partial)

    def request_level(self, level_v: float) -> None:
        """Trigger level chosen elsewhere (plot marker drag): clamp and request it."""
        lo, hi = self._settings.level_range_v()
        self._request(trigger_level_v=min(max(float(level_v), lo), hi))
