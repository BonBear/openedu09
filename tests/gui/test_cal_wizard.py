"""Calibration wizard GUI (calibration_wizard.md 4.2): menu action, zero-step confirmation,
background measurements, trimmer live readout, cancel/close abort, and an end-to-end run on the
real session against the simulator (UNIT1 physics). Offscreen Qt; never the real device."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gui_helpers import make_window

from cal_fake_session import FakeSession, install
from edu09 import calwizard as cw
from edu09.device.scope import Scope
from edu09.gui.cal_wizard import (
    PAGE_PLAN,
    PAGE_PREVIEW,
    PAGE_SOURCES,
    PAGE_STEP,
    PAGE_VERIFY,
    CalibrationWizard,
)
from edu09.gui.qt_bridge import ScopeQtAdapter
from edu09.transport.sim import UNIT1, SimTransport

WAIT_MS = 15_000
AA = cw.FixedSource("AA", 1.5, reversible=True)
USB = cw.FixedSource("USB", 5.0)


@pytest.fixture
def calscope() -> Iterator[Scope]:
    """Virtual-clock simulator with an open input and the UNIT1 front end."""
    tr = SimTransport.virtual("dc:0v", seed=0)
    tr.device.calibration = UNIT1
    scope = Scope.open(tr, reconnect_interval_s=0.01)
    try:
        yield scope
    finally:
        scope.close()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeSession]:
    return install(monkeypatch)


@pytest.fixture
def adapter(calscope: Scope) -> Iterator[ScopeQtAdapter]:
    a = ScopeQtAdapter(calscope)
    try:
        yield a
    finally:
        a.shutdown(wait=False)


def _wizard(qtbot: Any, scope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, **kw: Any) -> Any:
    wiz = CalibrationWizard(scope, adapter, None, sources_path=tmp_path / "sources.json", **kw)
    qtbot.addWidget(wiz)
    wiz.confirm_abort = lambda: True  # type: ignore[method-assign]
    wiz.show()
    return wiz


def _to_steps(
    qtbot: Any, wiz: Any, sources: list[Any], *, mode: str = "quick", trimmer: bool = False
) -> None:
    assert wiz.currentId() == PAGE_SOURCES
    wiz.sources_page.set_sources(sources)
    wiz.sources_page.mode.setCurrentText(mode)
    wiz.sources_page.trimmer.setChecked(trimmer)
    wiz.next()
    assert wiz.currentId() == PAGE_PLAN, wiz.sources_page.error.text()
    assert wiz.plan_page.steps.count() == len(wiz.plan.setups)
    wiz.next()
    assert wiz.currentId() == PAGE_STEP, wiz.plan_page.error.text()
    assert wiz.session is not None


def _idle(qtbot: Any, page: Any) -> None:
    qtbot.waitUntil(lambda: not page.is_busy, timeout=WAIT_MS)


def _run_steps(qtbot: Any, page: Any) -> None:
    """Answer every setup of ``page`` (zero: tick the checkbox; readings: sim prefill)."""
    for _ in range(40):
        _idle(qtbot, page)
        if page.isComplete():
            return
        view = page.wiz.session.view()
        if view.needs == "input_disconnected":
            assert not page.measure_btn.isEnabled()  # needs the confirmation
            page.disconnected.setChecked(True)
        elif view.needs == "reading_v":
            assert page.reading.value() == pytest.approx(view.sim_reading_v, abs=1e-4)
        elif view.needs == "done":
            qtbot.waitUntil(lambda: page.trimmer_readings >= 2, timeout=WAIT_MS)
            page.trim_done_btn.click()
            qtbot.waitUntil(lambda: page.wiz.session.view().needs != "done", timeout=WAIT_MS)
            continue
        assert page.measure_btn.isEnabled(), view
        page.measure_btn.click()
    raise AssertionError("steps did not finish")


# --------------------------------------------------------------------------- main window


def test_menu_action_opens_wizard_and_close_resumes(
    qtbot: Any, calscope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = make_window(qtbot, calscope)
    try:
        assert win.calibrate_action.isEnabled()
        win.calibrate_action.trigger()
        wiz = win.cal_wizard
        assert isinstance(wiz, CalibrationWizard)
        assert wiz.isVisible() and win.busy_job == "calibration"
        assert not win.calibrate_action.isEnabled()
        assert win.open_calibration_wizard() is None  # busy: no second wizard
        wiz.reject()  # nothing measured: closes without asking
        qtbot.waitUntil(lambda: win.busy_job is None, timeout=WAIT_MS)
        assert win.cal_wizard is None and win.calibrate_action.isEnabled()
        assert "calibration unchanged" in win.log_view.toPlainText()
    finally:
        win.shutdown()


def test_quit_anyway_aborts_wizard_session(
    qtbot: Any, calscope: Scope, fake: type[FakeSession]
) -> None:
    win = make_window(qtbot, calscope)
    wiz = win.open_calibration_wizard()
    _to_steps(qtbot, wiz, [USB])
    sess = fake.instances[0]
    sess.gate = threading.Event()  # the zero measurement hangs until aborted
    wiz.step_page.disconnected.setChecked(True)
    wiz.step_page.measure_btn.click()
    qtbot.waitUntil(lambda: sess._op.locked(), timeout=WAIT_MS)
    win.confirm_close_while_busy = lambda what: "quit"  # type: ignore[method-assign]
    t0 = time.monotonic()
    win.close()
    assert time.monotonic() - t0 < 2.0  # never joins the measurement on the GUI thread
    assert "abort" in sess.calls
    qtbot.waitUntil(lambda: sess.state == "aborted", timeout=WAIT_MS)
    assert not wiz.isVisible()


# --------------------------------------------------------------------------- fake session


def test_zero_step_needs_checkbox_and_runs_in_background(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    page = wiz.step_page
    sess = fake.instances[0]
    assert page.disconnected.isVisible() and not page.reading.isVisible()
    assert not page.measure_btn.isEnabled()
    page.measure()  # ignored without the confirmation
    assert sess.calls == []
    sess.gate = threading.Event()
    page.disconnected.setChecked(True)
    t0 = time.monotonic()
    page.measure_btn.click()
    assert time.monotonic() - t0 < 0.5  # returns at once; measuring on the pool
    assert page.is_busy and page.busy.bar.isVisible() and not page.measure_btn.isEnabled()
    qtbot.waitUntil(lambda: sess._op.locked(), timeout=WAIT_MS)
    sess.gate.set()
    _idle(qtbot, page)
    assert sess.calls[0] == "confirm_zero"
    assert page.reading.isVisible() and not page.disconnected.isVisible()
    assert "OK" in page.results.item(0).text()
    assert not page.isComplete()


def test_reading_must_be_entered_on_hardware_like_scope(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    sess = fake.instances[0]
    sess.sim = None  # no simulated meter: nothing is prefilled
    page = wiz.step_page
    page.disconnected.setChecked(True)
    page.measure_btn.click()
    _idle(qtbot, page)
    assert page.reading.value() == 0.0 and not page.measure_btn.isEnabled()
    page.reading.setValue(4.987)
    assert page.measure_btn.isEnabled()
    page.measure_btn.click()
    _idle(qtbot, page)
    assert "submit_reading(4.987)" in sess.calls


def test_failed_step_retry_and_skip(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    page = wiz.step_page
    sess = fake.instances[0]
    sess.fail_next = True
    page.disconnected.setChecked(True)
    page.measure_btn.click()
    _idle(qtbot, page)
    assert "FAILED" in page.results.item(0).text()
    assert page.retry_btn.isEnabled()
    page.retry_btn.click()
    _idle(qtbot, page)
    assert "retry" in sess.calls
    page.skip_btn.click()
    _idle(qtbot, page)
    assert sess.view().setup.kind == "reference"  # type: ignore[union-attr]


def test_trimmer_live_updates_stop_on_done(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [AA, USB], trimmer=True)
    sess = fake.instances[0]
    page = wiz.step_page
    kinds = [s.kind for s in wiz.plan.setups]
    assert "trimmer" in kinds
    while sess.view().setup.kind != "trimmer":  # type: ignore[union-attr]
        if sess.view().needs == "input_disconnected":
            page.disconnected.setChecked(True)
        page.measure_btn.click()
        _idle(qtbot, page)
    page.measure_btn.click()  # the trimmer's reference reading -> live phase
    qtbot.waitUntil(lambda: page.trimmer_readings >= 3, timeout=WAIT_MS)
    assert page.trim_box.isVisible()
    assert "V" in page.trim_value.text() and "mV" in page.trim_detail.text()
    assert "%" in page.trim_detail.text()
    assert not page.isComplete() and not page.skip_btn.isEnabled()
    page.trim_done_btn.click()
    qtbot.waitUntil(lambda: "trimmer_done" in sess.calls, timeout=WAIT_MS)
    _idle(qtbot, page)
    n = sess.trim_n
    qtbot.wait(800)
    assert sess.trim_n == n  # no more samples after Done
    assert not page.trim_box.isVisible()
    assert sess.view().setup.kind == "reference"  # type: ignore[union-attr]
    # samples never overlapped (the fake raises "busy" on re-entry)
    texts = [page.results.item(i).text() for i in range(page.results.count())]
    assert not any("busy" in t for t in texts), texts


def test_cancel_with_points_asks_and_aborts(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    sess = fake.instances[0]
    page = wiz.step_page
    page.disconnected.setChecked(True)
    page.measure_btn.click()
    _idle(qtbot, page)
    asked: list[bool] = []
    wiz.confirm_abort = lambda: asked.append(False) or False  # type: ignore[method-assign]
    wiz.reject()
    assert asked and wiz.isVisible() and sess.state == "in_setup"
    wiz.confirm_abort = lambda: True  # type: ignore[method-assign]
    wiz.close()
    assert not wiz.isVisible()
    assert sess.state == "aborted"


def test_preview_save_and_verify_with_fake(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    _run_steps(qtbot, wiz.step_page)
    wiz.next()
    assert wiz.currentId() == PAGE_PREVIEW
    pv = wiz.preview_page
    qtbot.waitUntil(lambda: pv.fit is not None, timeout=WAIT_MS)
    assert pv.table.rowCount() == 1 and pv.table.item(0, 0).text() == "0.9960"
    assert "demo warning" in pv.warnings.toPlainText()
    assert not pv.isComplete()  # Finish/Next disabled until save or "close without saving"
    pv.save_btn.click()
    qtbot.waitUntil(lambda: pv.saved is not None, timeout=WAIT_MS)
    assert "Applied" in pv.status.text()  # sim scope without a file
    assert pv.isComplete() and pv.nextId() == PAGE_VERIFY
    wiz.next()
    vp = wiz.verify_page
    assert wiz.currentId() == PAGE_VERIFY
    _run_steps(qtbot, vp)
    assert vp.isComplete()
    vp.revert_btn.click()
    _idle(qtbot, vp)
    assert "revert" in fake.instances[0].calls
    wiz.accept()
    assert fake.instances[0].state == "done"


def test_close_without_saving(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    _to_steps(qtbot, wiz, [USB])
    _run_steps(qtbot, wiz.step_page)
    wiz.next()
    pv = wiz.preview_page
    qtbot.waitUntil(lambda: pv.fit is not None, timeout=WAIT_MS)
    pv.discard_btn.click()
    assert pv.isComplete() and pv.nextId() == -1
    wiz.accept()
    sess = fake.instances[0]
    assert sess.state == "aborted" and not any(c.startswith("save") for c in sess.calls)


def test_sources_profile_round_trip(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, fake: type[FakeSession]
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    sp = wiz.sources_page
    sp.set_sources([AA, cw.AdjustableSource("bench", 0.0, 12.0)])
    assert sp.floating.isVisibleTo(wiz)  # a reversible source shows the warning
    sp.mode.setCurrentText("standard")
    wiz.next()
    assert (tmp_path / "sources.json").exists()
    wiz.reject()
    wiz2 = _wizard(qtbot, calscope, adapter, tmp_path)
    assert wiz2.sources_page.sources() == (AA, cw.AdjustableSource("bench", 0.0, 12.0))
    assert wiz2.sources_page.mode.currentText() == "standard"
    wiz2.reject()


def test_invalid_source_shows_error(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path
) -> None:
    wiz = _wizard(qtbot, calscope, adapter, tmp_path)
    wiz.sources_page.set_sources([AA])
    wiz.sources_page.table.item(0, 1).setText("")
    wiz.next()
    assert wiz.currentId() == PAGE_SOURCES
    assert "row 1" in wiz.sources_page.error.text()
    wiz.reject()


# --------------------------------------------------------------------------- real session


@pytest.mark.parametrize("trimmer", [False, True])
def test_end_to_end_real_session_on_sim(
    qtbot: Any, calscope: Scope, adapter: ScopeQtAdapter, tmp_path: Path, trimmer: bool
) -> None:
    target = tmp_path / "calibration.json"
    before = calscope.calibration
    wiz = _wizard(qtbot, calscope, adapter, tmp_path, calibration_path=target)
    _to_steps(qtbot, wiz, [AA, USB], trimmer=trimmer)
    sess = wiz.session
    assert isinstance(sess, cw.CalibrationSession)
    assert ("trimmer" in [s.kind for s in wiz.plan.setups]) is trimmer
    _run_steps(qtbot, wiz.step_page)
    assert calscope.calibration == before  # nothing installed before save
    wiz.next()
    pv = wiz.preview_page
    qtbot.waitUntil(lambda: pv.fit is not None, timeout=WAIT_MS)
    assert pv.table.rowCount() == 6
    pv.save_btn.click()
    qtbot.waitUntil(lambda: pv.saved is not None, timeout=WAIT_MS)
    assert pv.saved.saved and target.exists()
    assert calscope.calibration.calibration_id == pv.saved.calibration_id
    g1 = calscope.calibration.range_gain_for(1.0)
    assert g1 == pytest.approx(UNIT1.range_gain_for(1.0), abs=0.01)
    if pv.nextId() == PAGE_VERIFY:
        wiz.next()
        _run_steps(qtbot, wiz.verify_page)
    wiz.accept()
    assert sess.state.terminal
    if trimmer:
        assert wiz.step_page.trimmer_readings >= 2
