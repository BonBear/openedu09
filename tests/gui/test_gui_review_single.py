"""GUI review: a pending Single (waiting for a trigger) must be cancellable from the GUI."""

from __future__ import annotations

from typing import Any

import pytest
from gui_helpers import make_window

pytestmark = pytest.mark.gui


def test_stop_enabled_while_single_pending(qtbot: Any, rtscope: Any) -> None:
    rtscope.configure(trigger_mode="normal", trigger_level_v=3.5)  # never triggers (2 Vpp)
    win = make_window(qtbot, rtscope)
    try:
        win.single()
        qtbot.waitUntil(lambda: win._state == "armed", timeout=5000)
        assert win.controls.stop_button.isEnabled()
        win.stop()
        qtbot.waitUntil(lambda: win._state == "idle", timeout=5000)
        assert not win.controls.stop_button.isEnabled()
    finally:
        win.shutdown()
