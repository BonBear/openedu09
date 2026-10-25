"""Review of the GUI follow-ups: indicator cost per frame, abort of a running calibration /
exclusive job through the Scope API. Simulator / fakes only."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from edu09.errors import Cancelled

pytestmark = pytest.mark.gui


def test_set_indicator_skips_unchanged_style(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_update_indicator`` runs for every frame (~30/s): an unchanged state must not re-set
    the style sheet (a full re-polish of the label each time)."""
    from edu09.gui.controls import ControlPanel

    panel = ControlPanel()
    calls: list[str] = []
    real = panel.state_indicator.setStyleSheet
    monkeypatch.setattr(
        panel.state_indicator, "setStyleSheet", lambda s: (calls.append(s), real(s))
    )
    panel.set_indicator("running")
    panel.set_indicator("running")
    panel.set_indicator("running")
    assert len(calls) == 1
    panel.set_indicator("busy", "CALIBRATION")
    panel.set_indicator("busy", "CALIBRATION — QUIT PENDING")  # same kind, new text
    assert panel.state_indicator.text() == "CALIBRATION — QUIT PENDING"
    panel.set_indicator("stopped")
    assert panel.indicator_kind == "stopped" and panel.state_indicator.text() == "STOPPED"


def test_quit_anyway_aborts_calibration_through_scope_api(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Quit anyway" while the calibration wizard is open aborts the wizard's session and any
    running exclusive job with the plain ``Scope.abort_exclusive`` (no proxy objects)."""
    scope = window.scope
    aborts: list[bool] = []
    real_abort = scope.abort_exclusive

    def abort_exclusive() -> bool:
        aborts.append(True)
        return bool(real_abort())

    from edu09.gui.cal_wizard import CalibrationWizard

    session_aborts: list[bool] = []
    real_session_abort = CalibrationWizard.abort_session

    def abort_session(wiz: Any) -> bool:
        session_aborts.append(True)
        return real_session_abort(wiz)

    monkeypatch.setattr(scope, "abort_exclusive", abort_exclusive)
    monkeypatch.setattr(CalibrationWizard, "abort_session", abort_session)
    monkeypatch.setattr(window, "confirm_close_while_busy", lambda _what: "quit")
    window.show()
    wiz = window.open_calibration_wizard()
    assert wiz is not None and window.busy_job == "calibration"
    assert window.close() is True
    assert aborts == [True] and session_aborts  # (the wizard's reject may abort it again)
    assert not wiz.isVisible() and window.cal_wizard is None


def test_window_close_then_scope_close_aborts_foreign_exclusive_job(qtbot: Any) -> None:
    """An exclusive job the GUI did not start (e.g. over its in-process MCP server) is aborted
    by the owner's ``Scope.close`` after the window closed, so the close is prompt."""
    from gui_helpers import make_window

    from edu09.device.scope import Scope
    from edu09.transport.sim import SimTransport

    scope = Scope.open(SimTransport("dc:0v:noise=40mv", seed=0), reconnect_interval_s=0.01)
    started = threading.Event()
    result: list[BaseException | None] = []

    def long_job(io: Any) -> None:
        started.set()
        while True:
            io.live_samples(1024, timebase="2ms")

    def runner() -> None:
        try:
            scope.run_exclusive(long_job, timeout_s=30.0)
        except BaseException as exc:
            result.append(exc)
        else:
            result.append(None)

    t = threading.Thread(target=runner, daemon=True)
    try:
        win = make_window(qtbot, scope)
        win.show()
        t.start()
        assert started.wait(10.0)
        assert win.close() is True  # not busy: no dialog
        t0 = time.monotonic()
        scope.close()
        assert time.monotonic() - t0 < 2.0
        t.join(10.0)
        assert isinstance(result[0], Cancelled)
    finally:
        scope.close()
