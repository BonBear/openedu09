"""Calibration wizard (``QWizard``) over a :class:`~edu09.calwizard.CalibrationSession`
(calibration_wizard.md 4.2).

Pages: Sources -> Plan -> Step (reused for every setup) -> Preview (fit + save) -> Verify.

Threading: every session call that may touch the device (measure, trimmer sample, fit, save,
revert, retry/skip) runs on the adapter's background pool via ``adapter.call_background``; the
results come back on the GUI thread. ``session.view()`` and ``session.abort()`` are cheap and
thread-safe and are called directly. Trimmer samples are chained (the next one is requested
only after the previous one returned), so session calls never overlap. Cancel / close aborts
the session (nothing is installed or saved before the explicit "Save calibration").
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from edu09 import calwizard as cw
from edu09.calwizard.model import FLOATING_WARNING, MAX_SOURCES

if TYPE_CHECKING:
    from edu09.device.scope import Scope
    from edu09.gui.qt_bridge import ScopeQtAdapter

__all__ = [
    "PAGE_PLAN",
    "PAGE_PREVIEW",
    "PAGE_SOURCES",
    "PAGE_STEP",
    "PAGE_VERIFY",
    "TRIMMER_INTERVAL_MS",
    "TRIMMER_TEXT",
    "CalibrationWizard",
]

log = logging.getLogger(__name__)

PAGE_SOURCES, PAGE_PLAN, PAGE_STEP, PAGE_PREVIEW, PAGE_VERIFY = range(5)
TRIMMER_INTERVAL_MS = 250
"""Pause between two chained trimmer samples (each sample itself takes ~0.25 s)."""
TRIMMER_TEXT = (
    "Turn RV1 slowly with a small (insulated) screwdriver until the deviation is ~0. RV1 changes "
    "the gain of all ranges, so the gain steps follow."
)
MODES = ("quick", "standard", "full")
UNCALIBRATED = ("keep", "typical", "unity")
VDIVS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
COL_TYPE, COL_LABEL, COL_V1, COL_V2, COL_TOL, COL_REV = range(6)


def _vdiv_text(v: float) -> str:
    return f"{v * 1000:g} mV/div" if v < 1 else f"{v:g} V/div"


def _fmt(x: float | None, spec: str = ".4g", unit: str = "") -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x:{spec}}{unit}"


class _Busy:
    """A busy indicator (indeterminate progress bar) shared by the session pages."""

    def __init__(self) -> None:
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        self.bar.hide()

    def set(self, busy: bool) -> None:
        self.bar.setVisible(busy)


# ----------------------------------------------------------------------------------------------
# Page 1: sources
# ----------------------------------------------------------------------------------------------


class SourcesPage(QWizardPage):
    def __init__(self, wizard: CalibrationWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle("Reference sources")
        self.setSubTitle(
            "Describe the DC voltages you can apply to CH1 and measure with your multimeter "
            "(batteries, USB 5 V, a bench supply). The wizard plans the steps from them."
        )
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Type", "Label", "Volts / min", "Max (adjustable)", "Tol %", "Reversible"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            COL_LABEL, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.add_fixed = QPushButton("Add fixed")
        self.add_adj = QPushButton("Add adjustable")
        self.remove = QPushButton("Remove")
        for b in (self.add_fixed, self.add_adj, self.remove):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.add_fixed.clicked.connect(lambda: self.add_source(cw.FixedSource("battery", 1.5)))
        self.add_adj.clicked.connect(
            lambda: self.add_source(cw.AdjustableSource("bench supply", 0.0, 15.0))
        )
        self.remove.clicked.connect(self._remove_selected)

        self.floating = QLabel(FLOATING_WARNING)
        self.floating.setWordWrap(True)
        self.floating.setStyleSheet("QLabel { color: #b5651d; font-weight: bold; }")
        self.floating.hide()
        lay.addWidget(self.floating)

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems(MODES)
        self.max_v = QDoubleSpinBox()
        self.max_v.setRange(0.5, 30.0)
        self.max_v.setDecimals(1)
        self.max_v.setSuffix(" V")
        self.max_v.setToolTip("Highest |voltage| the wizard may ask for (input limit 30 V)")
        self.uncal = QComboBox()
        self.uncal.addItems(UNCALIBRATED)
        self.uncal.setToolTip(
            "Ranges without measurements: keep the previous gains, use typical values, or 1.0"
        )
        form.addRow("Mode", self.mode)
        form.addRow("Max voltage", self.max_v)
        form.addRow("Uncovered ranges", self.uncal)
        lay.addLayout(form)

        self.trimmer = QGroupBox("Trimmer RV1 step (input amplifier gain)")
        self.trimmer.setCheckable(True)
        self.trimmer.setChecked(False)
        tform = QFormLayout(self.trimmer)
        self.t_placement = QComboBox()
        self.t_placement.addItems(["after_zero", "first"])
        self.t_reference = QComboBox()
        self.t_reference.addItems(["source", "open"])
        self.t_vdiv = QComboBox()
        for v in VDIVS:
            self.t_vdiv.addItem(_vdiv_text(v), v)
        tform.addRow("Placement", self.t_placement)
        tform.addRow("Reference", self.t_reference)
        tform.addRow("Range", self.t_vdiv)
        lay.addWidget(self.trimmer)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("QLabel { color: #d1495b; }")
        lay.addWidget(self.error)

    # -- table -----------------------------------------------------------------------------
    def add_source(self, src: cw.Source) -> None:
        if self.table.rowCount() >= MAX_SOURCES:
            self.error.setText(f"At most {MAX_SOURCES} sources.")
            return
        r = self.table.rowCount()
        self.table.insertRow(r)
        kind = QComboBox()
        kind.addItems(["fixed", "adjustable"])
        fixed = isinstance(src, cw.FixedSource)
        kind.setCurrentIndex(0 if fixed else 1)
        self.table.setCellWidget(r, COL_TYPE, kind)
        self.table.setItem(r, COL_LABEL, QTableWidgetItem(src.label))
        v1 = QDoubleSpinBox()
        v1.setRange(0.0, 30.0)
        v1.setDecimals(3)
        v1.setSuffix(" V")
        v2 = QDoubleSpinBox()
        v2.setRange(0.0, 30.0)
        v2.setDecimals(3)
        v2.setSuffix(" V")
        tol = QDoubleSpinBox()
        tol.setRange(0.0, 25.0)
        tol.setDecimals(1)
        if isinstance(src, cw.FixedSource):
            v1.setValue(src.volts)
            tol.setValue(src.tolerance_pct)
        else:
            v1.setValue(src.min_v)
            v2.setValue(src.max_v)
            tol.setValue(5.0)
        rev = QCheckBox()
        rev.setChecked(src.reversible)
        rev.setToolTip(FLOATING_WARNING)
        rev.toggled.connect(self._update_floating)
        self.table.setCellWidget(r, COL_V1, v1)
        self.table.setCellWidget(r, COL_V2, v2)
        self.table.setCellWidget(r, COL_TOL, tol)
        self.table.setCellWidget(r, COL_REV, rev)

        def kind_changed(_i: int, v2: QDoubleSpinBox = v2, tol: QDoubleSpinBox = tol) -> None:
            adj = kind.currentText() == "adjustable"
            v2.setEnabled(adj)
            tol.setEnabled(not adj)

        kind.currentIndexChanged.connect(kind_changed)
        kind_changed(0)
        self._update_floating()

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self._update_floating()

    def _update_floating(self) -> None:
        any_rev = False
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, COL_REV)
            any_rev = any_rev or (isinstance(w, QCheckBox) and w.isChecked())
        self.floating.setVisible(any_rev)

    def set_sources(self, sources: Sequence[cw.Source]) -> None:
        self.table.setRowCount(0)
        for s in sources:
            self.add_source(s)

    def sources(self) -> tuple[cw.Source, ...]:
        """The table as sources (``ValueError`` with the row number on invalid input)."""
        out: list[cw.Source] = []
        for r in range(self.table.rowCount()):
            kind = self.table.cellWidget(r, COL_TYPE)
            item = self.table.item(r, COL_LABEL)
            label = item.text().strip() if item is not None else ""
            v1 = self.table.cellWidget(r, COL_V1)
            v2 = self.table.cellWidget(r, COL_V2)
            tol = self.table.cellWidget(r, COL_TOL)
            rev = self.table.cellWidget(r, COL_REV)
            assert isinstance(kind, QComboBox) and isinstance(rev, QCheckBox)
            assert isinstance(v1, QDoubleSpinBox) and isinstance(v2, QDoubleSpinBox)
            assert isinstance(tol, QDoubleSpinBox)
            try:
                if kind.currentText() == "fixed":
                    out.append(cw.FixedSource(label, v1.value(), tol.value(), rev.isChecked()))
                else:
                    out.append(cw.AdjustableSource(label, v1.value(), v2.value(), rev.isChecked()))
            except ValueError as exc:
                raise ValueError(f"row {r + 1}: {exc}") from None
        return tuple(out)

    def set_options(self, o: cw.PlanOptions) -> None:
        self.mode.setCurrentText(o.mode)
        self.max_v.setValue(o.max_abs_v)
        self.uncal.setCurrentText(o.uncalibrated)
        t = o.trimmer
        self.trimmer.setChecked(t.enabled)
        self.t_placement.setCurrentText(t.placement)
        self.t_reference.setCurrentText(t.reference)
        i = self.t_vdiv.findData(t.vdiv)
        if i >= 0:
            self.t_vdiv.setCurrentIndex(i)

    def options(self) -> cw.PlanOptions:
        base = self.wiz.base_options
        trim = cw.TrimmerConfig(
            enabled=self.trimmer.isChecked(),
            placement=self.t_placement.currentText(),  # type: ignore[arg-type]
            reference=self.t_reference.currentText(),  # type: ignore[arg-type]
            source_label=base.trimmer.source_label,
            vdiv=float(self.t_vdiv.currentData()),
            position_div=base.trimmer.position_div,
            instruction=base.trimmer.instruction,
        )
        return cw.PlanOptions(
            mode=self.mode.currentText(),  # type: ignore[arg-type]
            max_abs_v=self.max_v.value(),
            ranges=base.ranges,
            zero=base.zero,
            verify=base.verify,
            trimmer=trim,
            uncalibrated=self.uncal.currentText(),  # type: ignore[arg-type]
        )

    def validatePage(self) -> bool:  # noqa: N802 - Qt API
        self.error.clear()
        try:
            sources = self.sources()
            options = self.options()
            plan = cw.plan_calibration(sources, options)
        except ValueError as exc:
            self.error.setText(str(exc))
            return False
        self.wiz.plan = plan
        self.wiz.save_profile(sources, options)
        return True


# ----------------------------------------------------------------------------------------------
# Page 2: plan
# ----------------------------------------------------------------------------------------------


class PlanPage(QWizardPage):
    def __init__(self, wizard: CalibrationWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle("Plan")
        lay = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.steps = QListWidget()
        lay.addWidget(self.steps, 2)
        self.coverage = QTableWidget(0, 4)
        self.coverage.setHorizontalHeaderLabels(["Positive", "Negative", "Nonlinearity", "Needs"])
        self.coverage.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.coverage.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.coverage, 2)
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet("QLabel { color: #b5651d; }")
        lay.addWidget(self.warnings)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("QLabel { color: #d1495b; }")
        lay.addWidget(self.error)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        plan = self.wiz.plan
        self.error.clear()
        self.steps.clear()
        if plan is None:
            return
        mins = plan.estimated_s / 60.0
        self.summary.setText(
            f"{len(plan.setups)} steps, {plan.user_actions} readings, "
            f"{plan.reconnections} reconnections, about {mins:.0f} min. "
            "Measurements cannot be undone; Cancel aborts without changing anything."
        )
        for s in plan.setups:
            what: str = s.kind
            if s.volts is not None:
                what += f": {s.source_label} {s.volts:+.3g} V"
            ranges = ", ".join(_vdiv_text(v) for v in sorted({p.vdiv for p in s.points}))
            self.steps.addItem(f"{s.id}  {what}" + (f"  ({ranges})" if ranges else ""))
        self.coverage.setRowCount(len(plan.coverage))
        labels = []
        for r, c in enumerate(plan.coverage):
            labels.append(_vdiv_text(c.vdiv))
            for col, text in enumerate((c.pos, c.neg, c.nonlin, "; ".join(c.needs))):
                word = {"good": "ok"}.get(text, text).replace("_", " ")
                self.coverage.setItem(r, col, QTableWidgetItem(word))
        self.coverage.setVerticalHeaderLabels(labels)
        self.warnings.setText("\n".join(plan.warnings))

    def validatePage(self) -> bool:  # noqa: N802 - Qt API
        try:
            self.wiz.start_session()
        except Exception as exc:
            self.error.setText(f"Cannot start: {type(exc).__name__}: {exc}")
            return False
        return True


# ----------------------------------------------------------------------------------------------
# Pages 3 and 5: setups (reference/zero/trimmer, and verify)
# ----------------------------------------------------------------------------------------------


class StepPage(QWizardPage):
    """One page reused for every setup of a phase (``verify=False``: everything before the
    fit; ``verify=True``: the verify setups after saving)."""

    def __init__(self, wizard: CalibrationWizard, *, verify: bool) -> None:
        super().__init__()
        self.wiz = wizard
        self.verify = verify
        self._busy = False
        self._trim_live = False
        self._trim_inflight = False
        self._trim_stop = False
        self._last_reading: float | None = None
        self._shown_setup: str | None = None
        self.trimmer_readings = 0
        self.setTitle("Verify" if verify else "Measure")
        lay = QVBoxLayout(self)
        self.header = QLabel()
        f = QFont(self.header.font())
        f.setBold(True)
        self.header.setFont(f)
        lay.addWidget(self.header)
        self.instruction = QLabel()
        self.instruction.setWordWrap(True)
        self.instruction.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.instruction)
        self.target = QLabel()
        self.target.setWordWrap(True)
        lay.addWidget(self.target)

        self.disconnected = QCheckBox("The input is disconnected (probe tip and clip free)")
        self.disconnected.toggled.connect(self._update_buttons)
        lay.addWidget(self.disconnected)

        rrow = QHBoxLayout()
        self.reading_label = QLabel("Multimeter reading:")
        self.reading = QDoubleSpinBox()
        self.reading.setRange(-30.0, 30.0)
        self.reading.setDecimals(4)
        self.reading.setSingleStep(0.001)
        self.reading.setSuffix(" V")
        self.reading.valueChanged.connect(self._update_buttons)
        rrow.addWidget(self.reading_label)
        rrow.addWidget(self.reading)
        rrow.addStretch(1)
        lay.addLayout(rrow)

        brow = QHBoxLayout()
        self.measure_btn = QPushButton("Measure")
        self.retry_btn = QPushButton("Retry")
        self.skip_btn = QPushButton("Skip")
        for b in (self.measure_btn, self.retry_btn, self.skip_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)
        self.measure_btn.clicked.connect(self.measure)
        self.retry_btn.clicked.connect(self.retry)
        self.skip_btn.clicked.connect(self.skip)

        # trimmer live readout
        self.trim_box = QGroupBox("Trimmer RV1: live reading")
        tl = QVBoxLayout(self.trim_box)
        self.trim_value = QLabel("—")
        big = QFont(self.trim_value.font())
        big.setPointSizeF(max(big.pointSizeF(), 9.0) * 2.4)
        big.setBold(True)
        self.trim_value.setFont(big)
        self.trim_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trim_detail = QLabel()
        self.trim_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trim_hint = QLabel(TRIMMER_TEXT)
        self.trim_hint.setWordWrap(True)
        self.trim_done_btn = QPushButton("Done adjusting")
        self.trim_done_btn.clicked.connect(self.trimmer_done)
        tl.addWidget(self.trim_value)
        tl.addWidget(self.trim_detail)
        tl.addWidget(self.trim_hint)
        tl.addWidget(self.trim_done_btn)
        self.trim_box.hide()
        lay.addWidget(self.trim_box)

        self.busy = _Busy()
        lay.addWidget(self.busy.bar)
        self.results = QListWidget()
        lay.addWidget(self.results, 1)
        self.revert_btn = QPushButton("Revert to previous calibration")
        self.revert_btn.clicked.connect(self.revert)
        self.revert_btn.setVisible(verify)
        lay.addWidget(self.revert_btn)
        self._trim_timer = QTimer(self)
        self._trim_timer.setSingleShot(True)
        self._trim_timer.timeout.connect(self._trimmer_next)

    # -- state -----------------------------------------------------------------------------
    @property
    def is_busy(self) -> bool:
        return self._busy

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy.set(busy or self._trim_live)
        self._update_buttons()
        self.completeChanged.emit()

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        self.results.clear()
        self._shown_setup = None
        self.refresh()

    def cleanupPage(self) -> None:  # noqa: N802 - Qt API
        self.stop_trimmer()

    def _phase_over(self, view: Any) -> bool:
        st = view.state
        if self.verify:
            return bool(st.terminal) or view.setup is None
        return st not in (cw.SessionState.PLANNED, cw.SessionState.IN_SETUP)

    def refresh(self) -> None:
        sess = self.wiz.session
        if sess is None:
            return
        view = sess.view()
        setup = view.setup
        over = self._phase_over(view)
        if over or setup is None:
            self.header.setText("All steps done." if not over or not self.verify else "Done.")
            if self.verify and view.state == cw.SessionState.DONE:
                self.header.setText("Verification finished.")
            self.instruction.clear()
            self.target.clear()
            self.disconnected.hide()
            self.reading_label.hide()
            self.reading.hide()
            self.trim_box.hide()
        else:
            self.header.setText(
                f"Step {view.index + 1} of {view.total}: {setup.kind} ({setup.id})"
                + (f" — {setup.source_label}" if setup.source_label else "")
            )
            self.instruction.setText(setup.instruction)
            self.target.setText(self._target_text(setup))
            zero = view.needs == "input_disconnected"
            reading = view.needs == "reading_v"
            self.disconnected.setVisible(zero)
            self.reading_label.setVisible(reading)
            self.reading.setVisible(reading)
            if setup.id != self._shown_setup:
                self._shown_setup = setup.id
                self.disconnected.setChecked(False)
                prefill = view.sim_reading_v
                if prefill is None and self.verify:
                    prefill = self._last_reading
                self.reading.setValue(prefill if prefill is not None else 0.0)
            live = view.needs == "done" and setup.kind == "trimmer"
            self.trim_box.setVisible(live)
            if live and not self._trim_live:
                self.start_trimmer()
        self._update_buttons()
        self.completeChanged.emit()

    @staticmethod
    def _target_text(setup: Any) -> str:
        if setup.volts is None:
            return ""
        verb = "Set the supply to" if setup.adjustable else "Expected about"
        text = f"{verb} {setup.volts:+.4g} V"
        if setup.accept_v is not None:
            lo, hi = setup.accept_v
            text += f" (accepted {lo:+.4g} .. {hi:+.4g} V)"
        return text + ". Type the multimeter reading at the probe tip."

    def _update_buttons(self) -> None:
        sess = self.wiz.session
        view = sess.view() if sess is not None else None
        idle = not self._busy and view is not None and not self._phase_over(view)
        needs = view.needs if view is not None else "none"
        can_measure = idle and (
            (needs == "input_disconnected" and self.disconnected.isChecked())
            or (needs == "reading_v" and abs(self.reading.value()) > 0)
        )
        self.measure_btn.setEnabled(bool(can_measure))
        phase = getattr(view, "phase", None)
        # retry: the failed current setup, or redo the previous one (e.g. a mistyped reading)
        failed = phase is not None and str(phase) == "failed"
        has_prev = view is not None and view.last_result is not None
        self.retry_btn.setEnabled(bool(idle and (failed or has_prev) and not self._trim_live))
        self.skip_btn.setEnabled(bool(idle and not self._trim_live))
        self.trim_done_btn.setEnabled(not self._trim_stop)
        state = getattr(view, "state", None)
        self.revert_btn.setEnabled(
            not self._busy
            and state in (cw.SessionState.SAVED, cw.SessionState.DONE)
            and not self.wiz.reverted
        )

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        sess = self.wiz.session
        if sess is None or self._busy or self._trim_live:
            return False
        view = sess.view()
        if self.verify:
            return self._phase_over(view)
        return view.state in (cw.SessionState.READY_TO_FIT, cw.SessionState.FITTED)

    # -- actions ---------------------------------------------------------------------------
    def _call(self, fn: Callable[[], Any], done: Callable[[Any], None], what: str) -> None:
        self._set_busy(True)

        def ok(res: Any) -> None:
            self._set_busy(False)
            done(res)
            self.refresh()

        def failed(exc: BaseException) -> None:
            self._set_busy(False)
            self._add_line(f"{what} failed: {type(exc).__name__}: {exc}")
            self.refresh()
            self.wiz.session_error(exc)

        self.wiz.adapter.call_background(fn, ok, failed, what=f"calibration {what}")

    def _add_line(self, text: str) -> None:
        self.results.addItem(text)
        self.results.scrollToBottom()

    def _show_result(self, res: Any) -> None:
        self._add_line(f"{res.setup_id} {res.kind}: {str(res.status).upper()}")
        for p in res.points:
            parts = [f"  {_vdiv_text(p.vdiv)} @ {p.position_div:+g} div: {p.status}"]
            if p.deflection_div is not None:
                parts.append(f"{p.deflection_div:+.2f} div")
            if p.err_pct is not None:
                parts.append(f"err {p.err_pct:+.2f} %")
            if p.message:
                parts.append(p.message)
            self._add_line(", ".join(parts))
        for m in res.messages:
            self._add_line(f"  {m}")

    def measure(self) -> None:
        sess = self.wiz.session
        if sess is None or self._busy:
            return
        view = sess.view()
        if view.needs == "input_disconnected":
            if not self.disconnected.isChecked():
                return
            self._call(
                lambda: sess.confirm_zero(input_disconnected=True), self._show_result, "measure"
            )
        elif view.needs == "reading_v":
            v = float(self.reading.value())
            if v == 0:
                return
            self._last_reading = v
            self._call(lambda: sess.submit_reading(v), self._show_result, "measure")

    def retry(self) -> None:
        sess = self.wiz.session
        if sess is not None and not self._busy:
            self._call(sess.retry, lambda _v: self._add_line("Retrying the step"), "retry")

    def skip(self) -> None:
        sess = self.wiz.session
        if sess is not None and not self._busy:
            sid = self._shown_setup
            self._call(sess.skip, lambda _v: self._add_line(f"{sid}: skipped"), "skip")

    def revert(self) -> None:
        sess = self.wiz.session
        if sess is None or self._busy:
            return

        def done(res: Any) -> None:
            self.wiz.reverted = True
            self._add_line(f"Previous calibration restored ({res.calibration_id})")

        self._call(sess.revert, done, "revert")

    # -- trimmer ---------------------------------------------------------------------------
    def start_trimmer(self) -> None:
        self._trim_live = True
        self._trim_stop = False
        self.busy.set(True)
        self._trimmer_next()

    def stop_trimmer(self) -> None:
        self._trim_stop = True
        self._trim_timer.stop()
        if not self._trim_inflight:
            self._trim_live = False

    def _trimmer_next(self) -> None:
        sess = self.wiz.session
        if sess is None or self._trim_stop or self._trim_inflight or self.wiz.closing:
            self._trim_live = self._trim_inflight
            return
        self._trim_inflight = True

        def ok(r: Any) -> None:
            self._trim_inflight = False
            self.trimmer_readings += 1
            self._show_trimmer(r)
            if self._trim_stop:
                self._finish_trimmer()
            elif not self.wiz.closing:
                self._trim_timer.start(TRIMMER_INTERVAL_MS)

        def failed(exc: BaseException) -> None:
            self._trim_inflight = False
            self.trim_detail.setText(f"{type(exc).__name__}: {exc}")
            if self._trim_stop:
                self._finish_trimmer()
            elif not self.wiz.closing and not self.wiz.session_error(exc):
                self._trim_timer.start(TRIMMER_INTERVAL_MS * 4)

        self.wiz.adapter.call_background(sess.trimmer_sample, ok, failed, what="trimmer sample")

    def _show_trimmer(self, r: Any) -> None:
        self.trim_value.setText(f"{r.v_meas:.4f} V")
        arrow = {"better": "better", "worse": "worse", "same": "steady"}.get(r.trend, r.trend)
        self.trim_detail.setText(
            f"reference {r.v_ref:.4f} V   deviation {r.dev_v * 1e3:+.1f} mV "
            f"({r.dev_pct:+.2f} %)   best {r.best_abs_dev_pct:.2f} %   {arrow}   "
            f"noise {r.noise_counts:.1f} counts"
            + ("   (model ground)" if getattr(r, "ground_source", "map") == "model" else "")
        )

    def trimmer_done(self) -> None:
        if self._trim_stop:
            return
        self._trim_stop = True
        self._trim_timer.stop()
        self._update_buttons()
        if not self._trim_inflight:
            self._finish_trimmer()

    def _finish_trimmer(self) -> None:
        sess = self.wiz.session
        self._trim_live = False
        if sess is None:
            return

        def done(_v: Any) -> None:
            self._trim_stop = False
            self._add_line("Trimmer adjustment done")

        self._call(sess.trimmer_done, done, "trimmer")


# ----------------------------------------------------------------------------------------------
# Page 4: preview + save
# ----------------------------------------------------------------------------------------------


class PreviewPage(QWizardPage):
    def __init__(self, wizard: CalibrationWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self._busy = False
        self.fit: Any = None
        self.saved: Any = None
        self.discard = False
        self.setTitle("Result")
        self.setSubTitle("Fitted gains per range. Nothing is stored until you press Save.")
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["g+", "g−", "NL knots", "error before %", "error after %", "status"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table, 1)
        self.offsets = QLabel()
        self.offsets.setWordWrap(True)
        lay.addWidget(self.offsets)
        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setMaximumHeight(90)
        lay.addWidget(self.warnings)
        self.busy = _Busy()
        lay.addWidget(self.busy.bar)
        row = QHBoxLayout()
        self.save_btn = QPushButton("Save calibration")
        self.save_btn.clicked.connect(self.save)
        self.discard_btn = QPushButton("Close without saving")
        self.discard_btn.clicked.connect(self.close_without_saving)
        row.addWidget(self.save_btn)
        row.addWidget(self.discard_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy.set(busy)
        ready = not busy and self.fit is not None and self.saved is None and not self.discard
        self.save_btn.setEnabled(ready)
        self.discard_btn.setEnabled(not busy and self.saved is None and not self.discard)
        self.completeChanged.emit()

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        sess = self.wiz.session
        if sess is None or self.saved is not None:
            return
        self.status.setText("Fitting…")
        self._set_busy(True)

        def ok(fit: Any) -> None:
            self.fit = fit
            self._show(fit)
            self.status.setText("Review the result, then save it (the old file is backed up).")
            self._set_busy(False)

        def failed(exc: BaseException) -> None:
            self.status.setText(f"Fit failed: {type(exc).__name__}: {exc}")
            self._set_busy(False)
            self.wiz.session_error(exc)

        self.wiz.adapter.call_background(sess.fit, ok, failed, what="calibration fit")

    def _show(self, fit: Any) -> None:
        ranges = sorted(fit.ranges.items())
        self.table.setRowCount(len(ranges))
        labels = []
        for r, (v, rf) in enumerate(ranges):
            labels.append(_vdiv_text(v))
            cells = (
                _fmt(rf.gain_pos, ".4f"),
                _fmt(rf.gain_neg, ".4f"),
                str(len(rf.neg_knots)),
                _fmt(rf.before_err_pct_max, "+.2f"),
                _fmt(rf.after_err_pct_max, "+.2f"),
                rf.status,
            )
            for c, text in enumerate(cells):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.setVerticalHeaderLabels(labels)
        cal = fit.calibration
        text = f"Offsets: base 0x{cal.base_ofs:02X}, k {cal.gnd_fit.k:.4f}, b {cal.gnd_fit.b:.2f}"
        prev = getattr(fit, "previous", None)
        if prev is not None:
            text += (
                f" (previous: base 0x{prev.base_ofs:02X}, k {prev.gnd_fit.k:.4f}, "
                f"b {prev.gnd_fit.b:.2f})"
            )
        self.offsets.setText(text)
        warns = list(fit.warnings)
        for _v, rf in ranges:
            warns.extend(rf.warnings)
        self.warnings.setPlainText("\n".join(warns) if warns else "No warnings.")

    def save(self) -> None:
        sess = self.wiz.session
        if sess is None or self._busy or self.fit is None or self.saved is not None:
            return
        self._set_busy(True)

        def ok(res: Any) -> None:
            self.saved = res
            if res.saved:
                backup = res.backup_path
                self.status.setText(
                    f"Saved to {res.path}"
                    + (f"; previous file backed up as {backup}" if backup else "")
                    + ". Next: verify."
                )
            else:
                self.status.setText(
                    "Applied (simulated scope: no calibration file was written). Next: verify."
                )
            self._set_busy(False)

        def failed(exc: BaseException) -> None:
            self.status.setText(f"Save failed: {type(exc).__name__}: {exc}")
            self._set_busy(False)
            self.wiz.session_error(exc)

        self.wiz.adapter.call_background(
            # sim/replay scopes without a calibration file: the session only applies it
            lambda: sess.save(confirm=True),
            ok,
            failed,
            what="save",
        )

    def close_without_saving(self) -> None:
        if self.saved is not None:
            return
        self.discard = True
        self.status.setText(
            "Not saved. Press Finish to close (the stored calibration is unchanged)."
        )
        self._set_busy(False)

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        return not self._busy and (self.saved is not None or self.discard)

    def nextId(self) -> int:  # noqa: N802 - Qt API
        if self.saved is not None and self.wiz.has_verify:
            return PAGE_VERIFY
        return -1


# ----------------------------------------------------------------------------------------------
# the wizard
# ----------------------------------------------------------------------------------------------


class CalibrationWizard(QWizard):
    """Window-modal calibration wizard. Create with ``(scope, adapter, parent)`` and ``show()``.

    ``calibration_path``: where ``save`` writes (default: the scope's calibration file; sim and
    replay scopes without one only *apply* the result). ``sources_path``: the source profile
    (default ``calibration_sources.json`` in the config dir).
    """

    #: emitted (GUI thread) once the session ended (state name)
    sessionEnded = Signal(str)

    def __init__(
        self,
        scope: Scope,
        adapter: ScopeQtAdapter,
        parent: QWidget | None = None,
        *,
        calibration_path: str | Path | None = None,
        sources_path: str | Path | None = None,
        base_options: cw.PlanOptions | None = None,
    ) -> None:
        super().__init__(parent)
        self.scope = scope
        self.adapter = adapter
        self.calibration_path = Path(calibration_path) if calibration_path is not None else None
        self.sources_path = Path(sources_path) if sources_path is not None else None
        self.plan: cw.Plan | None = None
        self.session: cw.CalibrationSession | None = None
        self.reverted = False
        self.closing = False
        self._ended = False
        self.setWindowTitle("Calibrate EDU09")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.resize(760, 620)

        sources, options = cw.load_sources(self.sources_path)
        self.base_options = base_options if base_options is not None else options
        self.sources_page = SourcesPage(self)
        self.plan_page = PlanPage(self)
        self.step_page = StepPage(self, verify=False)
        self.preview_page = PreviewPage(self)
        self.verify_page = StepPage(self, verify=True)
        self.setPage(PAGE_SOURCES, self.sources_page)
        self.setPage(PAGE_PLAN, self.plan_page)
        self.setPage(PAGE_STEP, self.step_page)
        self.setPage(PAGE_PREVIEW, self.preview_page)
        self.setPage(PAGE_VERIFY, self.verify_page)
        self.sources_page.set_sources(sources)
        self.sources_page.set_options(self.base_options)
        # no way back once measuring started (measurements can't be undone): commit pages
        for p in (self.plan_page, self.step_page, self.preview_page):
            p.setCommitPage(True)
        self.setButtonText(QWizard.WizardButton.CommitButton, "Next >")

    # -- helpers ---------------------------------------------------------------------------
    @property
    def has_verify(self) -> bool:
        return self.plan is not None and any(s.kind == "verify" for s in self.plan.setups)

    def save_profile(self, sources: Sequence[cw.Source], options: cw.PlanOptions) -> None:
        try:
            cw.save_sources(sources, options, self.sources_path)
        except (OSError, ValueError) as exc:
            log.warning("could not save the source profile: %s", exc)

    def start_session(self) -> None:
        """Create and start the session (GUI thread; ``start()`` does not measure)."""
        if self.session is not None:
            return
        if self.plan is None:
            raise RuntimeError("no plan")
        sim = None
        if self.scope.status.transport_kind == "sim":
            try:
                from edu09.calwizard.simref import SimReference

                sim = SimReference.for_scope(self.scope)
            except ImportError:
                sim = None
        sess = cw.CalibrationSession(self.scope, self.plan, path=self.calibration_path, sim=sim)
        sess.start()
        self.session = sess

    def session_error(self, exc: BaseException) -> bool:
        """A session call failed. Returns True if the session is over (aborted/failed)."""
        sess = self.session
        if sess is not None and sess.state.terminal:
            self._emit_ended()
            return True
        return False

    def _emit_ended(self) -> None:
        if not self._ended and self.session is not None and self.session.state.terminal:
            self._ended = True
            self.sessionEnded.emit(str(self.session.state))

    def points_measured(self) -> bool:
        sess = self.session
        if sess is None:
            return False
        try:
            return any(r.points for r in sess.results)
        except Exception:
            return True

    # -- abort / close ---------------------------------------------------------------------
    def abort_session(self) -> bool:
        """Abort the session (thread-safe, non-blocking; safe to call repeatedly)."""
        self.closing = True
        self.step_page.stop_trimmer()
        self.verify_page.stop_trimmer()
        sess = self.session
        if sess is None or sess.state.terminal:
            return False
        try:
            return bool(sess.abort())
        except Exception:
            log.exception("aborting the calibration session failed")
            return False

    def confirm_abort(self) -> bool:
        """Ask before discarding measured points (overridable in tests)."""
        answer = QMessageBox.question(
            self,
            "Abort calibration",
            "Abort the calibration? The points measured so far are discarded and the stored "
            "calibration is not changed"
            + (" (a calibration already saved stays)." if self.preview_page.saved else "."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def reject(self) -> None:
        sess = self.session
        if (
            sess is not None
            and not sess.state.terminal
            and not self.closing
            and self.points_measured()
            and not self.confirm_abort()
        ):
            return
        self.abort_session()
        super().reject()

    def accept(self) -> None:
        # Finish: a session not finished yet (not saved, or verify not run) is ended here; a
        # saved calibration stays installed
        self.abort_session()
        super().accept()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.reject()
        if self.isVisible():
            event.ignore()
        else:
            event.accept()

    def done(self, result: int) -> None:
        super().done(result)
        self._emit_ended()
