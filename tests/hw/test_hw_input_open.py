"""Hardware: zero-volt accuracy with the INPUT OPEN (nothing connected to CH1).

Opt-in: ``EDU09_HW_INPUT_OPEN=1``. Needs a saved calibration (``Scope.calibrate``) to be
meaningful; the tests skip when only the typical fallback is in use.
"""

from __future__ import annotations

import numpy as np
import pytest
from hw_helpers import ENV_INPUT_OPEN, assert_clean, skip_unless

from edu09.device.scope import Scope
from edu09.model import VoltsPerDiv

pytestmark = pytest.mark.hardware

POSITIONS = (-3.0, 0.0, 3.0)


@pytest.fixture(autouse=True)
def _require_open_input(scope: Scope) -> None:
    skip_unless(ENV_INPUT_OPEN, "needs CH1 disconnected")
    if scope.calibration.source != "measured":
        pytest.skip(f"no saved calibration (source={scope.calibration.source!r}); calibrate first")


@pytest.mark.parametrize("vdiv", list(VoltsPerDiv), ids=lambda v: v.label)
@pytest.mark.parametrize("pos", POSITIONS, ids=lambda p: f"pos{p:+g}")
def test_zero_volts_within_0_1_div(scope: Scope, vdiv: VoltsPerDiv, pos: float) -> None:
    scope.configure(
        volts_per_div=vdiv, position_div=pos, coupling="dc", timebase="2ms", trigger_mode="none"
    )
    wf = scope.acquire(timeout_s=3.0)
    assert_clean(wf.raw)
    mean_div = float(wf.volts.mean()) / float(vdiv)
    assert abs(mean_div) <= 0.1, f"0 V reads {mean_div:+.3f} div"


@pytest.mark.parametrize("start,end", [(0.0, 3.0), (3.0, -3.0), (-3.0, 0.0)])
def test_first_frame_after_position_change_is_clean(scope: Scope, start: float, end: float) -> None:
    """The offset DAC settles in ~20 ms; the worker must wait before arming."""
    scope.configure(volts_per_div=1.0, position_div=start, timebase="200us", trigger_mode="none")
    scope.acquire(timeout_s=2.0)
    scope.configure(position_div=end)
    wf = scope.acquire(timeout_s=2.0)
    assert_clean(wf.raw)
    head = float(wf.volts[:64].mean())
    tail = float(np.median(wf.volts[512:]))
    assert abs(head - tail) <= 0.1, f"first samples {head:+.3f} V vs rest {tail:+.3f} V"
    assert abs(tail) <= 0.1
