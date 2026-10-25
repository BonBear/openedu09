"""ViewBox with wheel gestures shared by the time plot and the spectrum.

* plain wheel: horizontal zoom (pyqtgraph's behaviour with the Y axis mouse-disabled)
* Ctrl + wheel: vertical zoom around the pointer (Cmd on macOS: Qt maps it to ControlModifier)
* Shift + wheel: vertical pan (opt-in with ``pan_y``)

pyqtgraph's own handler is never run while Ctrl/Shift is held, so X is not zoomed as well.
"""

from __future__ import annotations

from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

__all__ = ["WheelViewBox", "zoom_range"]

#: pyqtgraph zooms by 1.02 ** (delta * wheelScaleFactor); one notch is delta 120
_STEP = 1.02
_PAN_FRACTION = 0.1  # of the visible span per wheel notch


def zoom_range(lo: float, hi: float, center: float, scale: float) -> tuple[float, float]:
    """``[lo, hi]`` scaled by ``scale`` about ``center`` (scale < 1 zooms in)."""
    return center + (lo - center) * scale, center + (hi - center) * scale


class WheelViewBox(pg.ViewBox):  # type: ignore[misc]
    """ViewBox adding Ctrl+wheel Y zoom, optional Shift+wheel Y pan and a double-click signal."""

    #: emitted after the user zoomed or panned the Y axis with the wheel
    userYChanged = Signal()
    #: emitted on a left double-click in the plot area
    doubleClicked = Signal()

    def __init__(self, *args: Any, pan_y: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pan_y = pan_y

    def wheelEvent(self, ev: Any, axis: Any = None) -> None:
        mods = ev.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if axis is None and (ctrl or (shift and self.pan_y)):
            self._wheel_y(ev, ctrl)
            return
        super().wheelEvent(ev, axis)

    def _wheel_y(self, ev: Any, zoom: bool) -> None:
        delta = float(ev.delta())
        ev.accept()
        if delta == 0:
            return
        lo, hi = self.viewRange()[1]
        if zoom:
            scale = _STEP ** (delta * self.state["wheelScaleFactor"])
            center = float(self.mapSceneToView(ev.scenePos()).y())
            new = zoom_range(lo, hi, center, scale)
        else:  # shift: wheel up moves the view up
            shift = (hi - lo) * _PAN_FRACTION * delta / 120.0
            new = (lo + shift, hi + shift)
        self.setYRange(*new, padding=0)  # honours the Y limits; disables Y auto-range
        self.userYChanged.emit()

    def mouseClickEvent(self, ev: Any) -> None:
        if ev.double() and ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()
            self.doubleClicked.emit()
        else:
            super().mouseClickEvent(ev)
