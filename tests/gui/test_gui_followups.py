"""GUI follow-ups: close during calibration/auto-set, MCP stop on close, app teardown,
resume after calibration, Qt xcb-cursor pre-flight check, acquisition-state indicator.

Simulator / fakes only (never the real device); no fixed sleeps.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gui_helpers import FakeMcp, make_window

from edu09 import calwizard as cw
from edu09.device import autoset
from edu09.device.scope import Scope
from edu09.transport.sim import SimTransport

pytestmark = pytest.mark.gui

WAIT_MS = 10_000
OPEN_INPUT = "dc:0v:noise=40mv"  # disconnected input: calibration succeeds on the sim
USB = cw.FixedSource("USB", 5.0)


def _gated_autoset(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Auto-set stand-in that blocks until ``gate`` is set (bounded): a long background job."""
    state = types.SimpleNamespace(entered=threading.Event(), gate=threading.Event())

    def auto_set(_scope: Scope, timeout_s: float = 5.0) -> Any:
        state.entered.set()
        state.gate.wait(WAIT_MS / 1000)
        return None

    monkeypatch.setattr(autoset, "auto_set", auto_set)
    return state


def _open_wizard(win: Any) -> Any:
    wiz = win.open_calibration_wizard()
    assert wiz is not None and win.busy_job == "calibration"
    wiz.confirm_abort = lambda: True  # closing with measured points: discard without asking
    return wiz


def _start_zero_step(qtbot: Any, wiz: Any) -> None:
    """Sources -> plan -> click "measure" on the zero step (runs in the background)."""
    from edu09.gui.cal_wizard import PAGE_PLAN, PAGE_STEP

    wiz.sources_page.set_sources([USB])
    wiz.sources_page.mode.setCurrentText("quick")
    wiz.sources_page.trimmer.setChecked(False)
    wiz.next()
    assert wiz.currentId() == PAGE_PLAN, wiz.sources_page.error.text()
    wiz.next()
    assert wiz.currentId() == PAGE_STEP, wiz.plan_page.error.text()
    assert wiz.session.view().needs == "input_disconnected"
    wiz.step_page.disconnected.setChecked(True)
    wiz.step_page.measure_btn.click()


@pytest.fixture
def open_rtscope(tmp_path: Path) -> Iterator[Scope]:
    """Real-time simulator with a disconnected input (a real calibration takes seconds)."""
    scope = Scope.open(SimTransport(OPEN_INPUT, seed=0), reconnect_interval_s=0.01)
    scope._calibration_path = tmp_path / "cal.json"  # type: ignore[attr-defined]
    scope.configure(timebase="200us")
    try:
        yield scope
    finally:
        scope.close()


# -- 1. closing during calibration / auto-set --------------------------------------------------


def test_close_during_calibration_wait_then_quit(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        window, "confirm_close_while_busy", lambda what: asked.append(what) or "wait"
    )
    window.show()
    wiz = _open_wizard(window)

    t0 = time.monotonic()
    assert window.close() is False  # not closed yet, and the GUI thread did not block
    assert time.monotonic() - t0 < 1.0
    assert asked == ["calibration"]
    assert window.isVisible() and window.quit_pending and not window._shut_down
    assert wiz.isVisible()  # the wizard stays open
    assert "QUIT PENDING" in window.controls.state_indicator.text()
    assert "Waiting for the calibration" in window.statusBar().currentMessage()

    wiz.reject()  # the user closes the wizard -> the window closes by itself
    qtbot.waitUntil(lambda: window._shut_down and not window.isVisible(), timeout=WAIT_MS)
    assert "calibration unchanged" in window.log_view.toPlainText()


def test_close_during_autoset_cancel_keeps_window(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _gated_autoset(monkeypatch)
    answers = iter(["wait", "cancel"])
    monkeypatch.setattr(window, "confirm_close_while_busy", lambda _what: next(answers))
    window.show()
    window.auto_set()
    qtbot.waitUntil(job.entered.is_set, timeout=WAIT_MS)
    try:
        assert window.close() is False and window.quit_pending
        assert window.close() is False and not window.quit_pending  # second close: cancel quit
    finally:
        job.gate.set()
    qtbot.waitUntil(lambda: window.busy_job is None, timeout=WAIT_MS)
    assert window.isVisible() and not window._shut_down


def test_close_during_real_calibration_quit_anyway_aborts_and_stops_device(
    qtbot: Any, open_rtscope: Scope, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calibration wizard zero step (real session) on the real-time simulator, then quit."""
    from edu09.device import calibration as calmod

    scope = open_rtscope
    entered = threading.Event()
    outcome: list[BaseException | None] = []
    orig = calmod._measure

    def spy(io: Any) -> Any:
        entered.set()
        try:
            res = orig(io)
        except BaseException as exc:
            outcome.append(exc)
            raise
        outcome.append(None)
        return res

    monkeypatch.setattr(calmod, "_measure", spy)
    scope.run()
    win = make_window(qtbot, scope)
    monkeypatch.setattr(win, "confirm_close_while_busy", lambda _what: "quit")
    win.show()
    qtbot.waitUntil(lambda: win._running, timeout=WAIT_MS)
    wiz = _open_wizard(win)
    _start_zero_step(qtbot, wiz)
    qtbot.waitUntil(entered.is_set, timeout=WAIT_MS)

    t0 = time.monotonic()
    assert win.close() is True
    assert time.monotonic() - t0 < 1.0  # no join of the ~4 s measurement on the GUI thread
    assert win._shut_down and win.adapter.closed
    assert not wiz.isVisible()

    # the procedure stops at its next device call ...
    qtbot.waitUntil(lambda: bool(outcome), timeout=WAIT_MS)
    from edu09.gui.main_window import JobAborted

    assert isinstance(outcome[0], JobAborted)
    # ... so the owner's Scope.close (app.main) is prompt and leaves the device stopped
    t1 = time.monotonic()
    scope.close()
    assert time.monotonic() - t1 < 3.0
    assert not scope.worker.thread.is_alive()
    transport: Any = scope._transport  # type: ignore[attr-defined]
    assert not transport.is_open
    assert transport.device.state != "armed"
    assert not (tmp_path / "cal.json").exists()  # aborted calibration is never saved


def test_close_during_autoset_quit_anyway_does_not_block(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = threading.Event(), threading.Event()

    def auto_set(scope: Scope, timeout_s: float = 5.0) -> Any:
        entered.set()
        gate.wait(WAIT_MS / 1000)
        return None

    monkeypatch.setattr(autoset, "auto_set", auto_set)
    answers = iter(["cancel", "quit"])
    monkeypatch.setattr(window, "confirm_close_while_busy", lambda _what: next(answers))
    window.show()
    window.auto_set()
    qtbot.waitUntil(entered.is_set, timeout=WAIT_MS)
    assert "AUTO-SET" in window.controls.state_indicator.text()
    try:
        assert window.close() is False and not window.quit_pending  # cancel
        t0 = time.monotonic()
        assert window.close() is True  # quit anyway
        assert time.monotonic() - t0 < 1.0
        assert window._shut_down
    finally:
        gate.set()


def test_close_when_idle_does_not_ask(
    qtbot: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(window, "confirm_close_while_busy", lambda what: asked.append(what) or "")
    window.show()
    assert window.close() is True
    assert asked == []


# -- 2. MCP stop on close ------------------------------------------------------------------------


def test_mcp_stop_is_not_dropped_when_pool_is_busy(
    qtbot: Any, window: Any, fake_mcp: FakeMcp
) -> None:
    """Toggle off while both background workers are busy, then close: the stop still runs."""
    window.set_mcp_enabled(True)
    qtbot.waitUntil(lambda: window.mcp_url is not None, timeout=WAIT_MS)
    handle = fake_mcp.handles[0]
    gate = threading.Event()
    started = [threading.Event(), threading.Event()]
    for ev in started:
        window.adapter.call_background(lambda ev=ev: (ev.set(), gate.wait(WAIT_MS / 1000)))
    try:
        qtbot.waitUntil(lambda: all(e.is_set() for e in started), timeout=WAIT_MS)
        window.set_mcp_enabled(False)
        window.close()
        assert handle.stopped == 1  # synchronous (bounded) on close, never cancelled
    finally:
        gate.set()


def test_mcp_start_in_flight_at_close_is_stopped(
    qtbot: Any, window: Any, fake_mcp: FakeMcp
) -> None:
    fake_mcp.start_gate = threading.Event()
    window.set_mcp_enabled(True)
    qtbot.waitUntil(lambda: bool(fake_mcp.threads), timeout=WAIT_MS)  # start job is running
    t0 = time.monotonic()
    window.close()
    assert time.monotonic() - t0 < 1.0  # the start job is not joined
    fake_mcp.start_gate.set()
    qtbot.waitUntil(lambda: bool(fake_mcp.handles), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: fake_mcp.handles[0].stopped == 1, timeout=WAIT_MS)
    assert not window._mcp_live


# -- 3. app.main closes the Scope if the window cannot be built --------------------------------


def test_main_closes_scope_when_window_construction_fails(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from edu09.gui import app

    opened: list[Scope] = []
    real_open = app.Scope.open

    def spy_open(*a: Any, **k: Any) -> Scope:
        sc = real_open(*a, **k)
        opened.append(sc)
        return sc

    def broken_window(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("window construction failed")

    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(app.Scope, "open", spy_open)
    monkeypatch.setattr(app, "MainWindow", broken_window)
    with pytest.raises(RuntimeError, match="window construction failed"):
        app.main(["--simulate", "sine:1khz:2vpp", "--no-run"])
    assert len(opened) == 1
    assert opened[0].closed and not opened[0].worker.thread.is_alive()


# -- 4. resume after calibration -----------------------------------------------------------------


def test_calibration_resumes_running(qtbot: Any, tmp_path: Path) -> None:
    """Calibration wizard with a real zero measurement on the virtual sim: running before ->
    running after (also when acquisition was stopped meanwhile); stopped before -> stopped."""
    scope = Scope.open(SimTransport.virtual(OPEN_INPUT, seed=0), reconnect_interval_s=0.01)
    try:
        scope._calibration_path = tmp_path / "cal.json"  # type: ignore[attr-defined]
        win = make_window(qtbot, scope)
        try:
            win.run()
            qtbot.waitUntil(lambda: win._running, timeout=WAIT_MS)
            wiz = _open_wizard(win)
            _start_zero_step(qtbot, wiz)
            qtbot.waitUntil(lambda: not wiz.step_page.is_busy, timeout=WAIT_MS * 3)
            assert wiz.session.results, "zero step did not run"
            wiz.reject()
            qtbot.waitUntil(lambda: win.busy_job is None, timeout=WAIT_MS)
            assert "calibration unchanged" in win.log_view.toPlainText()
            qtbot.waitUntil(lambda: win._running and scope.running, timeout=WAIT_MS)

            # acquisition stopped while the wizard is open: the GUI restarts it on close
            wiz = _open_wizard(win)
            scope.stop()
            qtbot.waitUntil(lambda: not win._running, timeout=WAIT_MS)
            wiz.reject()
            qtbot.waitUntil(lambda: win.busy_job is None, timeout=WAIT_MS)
            qtbot.waitUntil(lambda: win._running and scope.running, timeout=WAIT_MS)
            assert win.controls.indicator_kind in ("running", "waiting")

            # stopped before -> stays stopped
            win.stop()
            qtbot.waitUntil(lambda: not win._running, timeout=WAIT_MS)
            wiz = _open_wizard(win)
            wiz.reject()
            qtbot.waitUntil(lambda: win.busy_job is None, timeout=WAIT_MS)
            done: list[bool] = []
            win.adapter.call(lambda: None, on_result=lambda _r: done.append(True))
            qtbot.waitUntil(lambda: bool(done), timeout=WAIT_MS)
            assert not scope.running
            assert not (tmp_path / "cal.json").exists()  # nothing saved without "save"
        finally:
            win.shutdown()
    finally:
        scope.close()


# -- 5. Qt xcb-cursor pre-flight check -------------------------------------------------------------

X11 = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}
WAYLAND = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"}


def _missing() -> bool:
    return False


def _present() -> bool:
    return True


@pytest.mark.parametrize(
    ("env", "platform", "have", "problem"),
    [
        (X11, "linux", _missing, True),
        ({"DISPLAY": ":0"}, "linux", _missing, True),  # no session type, X display
        ({**X11, "QT_QPA_PLATFORM": "xcb"}, "linux", _missing, True),
        (X11, "linux", _present, False),  # library present: unchanged behaviour
        ({**X11, "QT_QPA_PLATFORM": "offscreen"}, "linux", _missing, False),
        ({**X11, "QT_QPA_PLATFORM": "wayland"}, "linux", _missing, False),
        (WAYLAND, "linux", _missing, False),  # Qt picks wayland
        ({**WAYLAND, "QT_QPA_PLATFORM": "xcb"}, "linux", _missing, True),
        ({}, "linux", _missing, False),  # no display at all
        (X11, "win32", _missing, False),
        (X11, "darwin", _missing, False),
        ({**X11, "EDU09_SKIP_QT_CHECK": "1"}, "linux", _missing, False),
        # fallback lists: xcb is reached when the entries before it are unusable
        ({**X11, "QT_QPA_PLATFORM": "wayland;xcb"}, "linux", _missing, True),
        ({**WAYLAND, "QT_QPA_PLATFORM": "wayland;xcb"}, "linux", _missing, False),
        ({**WAYLAND, "QT_QPA_PLATFORM": "xcb;wayland"}, "linux", _missing, True),
        ({**X11, "QT_QPA_PLATFORM": "wayland-egl;offscreen"}, "linux", _missing, False),
        ({**X11, "QT_QPA_PLATFORM": " ; xcb:dpi=96 "}, "linux", _missing, True),
        # Wayland session variables but no Wayland display (X-only login, ssh -X): X11
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}, "linux", _missing, True),
        ({"XDG_SESSION_TYPE": "wayland"}, "linux", _missing, False),
    ],
)
def test_qt_platform_problem(env: dict[str, str], platform: str, have: Any, problem: bool) -> None:
    from edu09.gui.app import qt_platform_problem

    msg = qt_platform_problem(env, platform, have)
    assert (msg is not None) is problem
    if msg is not None:
        assert "sudo apt install libxcb-cursor0" in msg
        assert "xcb-util-cursor" in msg
        assert ("QT_QPA_PLATFORM=wayland" in msg) is ("WAYLAND_DISPLAY" in env)


def test_xcb_cursor_available_uses_find_library_then_dlopen() -> None:
    from edu09.gui.app import xcb_cursor_available

    tried: list[str] = []
    assert xcb_cursor_available(lambda _n: "libxcb-cursor.so.0", lambda n: tried.append(n) or False)
    assert tried == []
    assert not xcb_cursor_available(lambda _n: None, lambda n: tried.append(n) or False)
    assert tried == ["libxcb-cursor.so.0"]
    assert xcb_cursor_available(lambda _n: None, lambda _n: True)


def test_main_exits_with_message_when_xcb_cursor_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ctypes
    import ctypes.util

    from edu09.gui import app

    opened: list[Any] = []
    monkeypatch.setattr(app, "_qapp_instance", lambda: None)  # as if no QApplication yet
    monkeypatch.setattr(sys, "platform", "linux")
    for k in ("QT_QPA_PLATFORM", "WAYLAND_DISPLAY", "EDU09_SKIP_QT_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(ctypes.util, "find_library", lambda _n: None)

    def no_lib(_name: str) -> Any:
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "CDLL", no_lib)
    monkeypatch.setattr(app.Scope, "open", lambda *a, **k: opened.append(a))
    assert app.main(["--no-run"]) == app.EXIT_QT_PLATFORM
    err = capsys.readouterr().err
    assert "libxcb-cursor0" in err and "sudo apt install" in err
    assert opened == []  # never touched the device


# -- 6. acquisition-state indicator ----------------------------------------------------------------


def test_indicator_follows_acquisition_state(qtbot: Any, window: Any) -> None:
    ind = window.controls
    assert ind.indicator_kind == "stopped" and window.controls.state_indicator.text() == "STOPPED"
    window.run()
    qtbot.waitUntil(lambda: ind.indicator_kind == "running", timeout=WAIT_MS)
    assert window.controls.state_indicator.text() == "RUNNING"
    window.stop()
    qtbot.waitUntil(lambda: ind.indicator_kind == "stopped", timeout=WAIT_MS)


def test_indicator_shows_waiting_for_trigger(qtbot: Any, window: Any) -> None:
    sim: Any = window.scope._transport  # type: ignore[attr-defined]
    sim.device.never_trigger = True
    window.scope.configure(trigger_mode="normal")
    window.run()
    qtbot.waitUntil(lambda: window.controls.indicator_kind == "waiting", timeout=WAIT_MS)
    assert window.controls.state_indicator.text() == "WAITING FOR TRIGGER"
    sim.device.never_trigger = False
    window.scope.configure(trigger_mode="auto")  # re-arms: frames again -> RUNNING
    qtbot.waitUntil(lambda: window.controls.indicator_kind == "running", timeout=WAIT_MS)
    window.stop()
    qtbot.waitUntil(lambda: window.controls.indicator_kind == "stopped", timeout=WAIT_MS)


def test_indicator_disconnected(qtbot: Any, window: Any) -> None:
    from edu09.device.worker import ConnectionEvent

    window._on_connection(ConnectionEvent(False, None, "unplugged"))
    assert window.controls.indicator_kind == "disconnected"
    window._on_connection(ConnectionEvent(True, "1.01", None))
    assert window.controls.indicator_kind in ("stopped", "running")


def test_indicator_colours_are_explicit(qtbot: Any) -> None:  # qtbot: a QApplication
    from edu09.gui.controls import INDICATOR_STYLES, ControlPanel

    panel = ControlPanel()
    for kind, (text, bg, fg) in INDICATOR_STYLES.items():
        panel.set_indicator(kind)
        assert panel.state_indicator.text() == text
        sheet = panel.state_indicator.styleSheet()
        assert bg in sheet and fg in sheet  # never relies on the (dark/light) palette
    panel.set_indicator("busy", "CALIBRATION")
    assert panel.state_indicator.text() == "CALIBRATION"
