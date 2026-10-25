"""Hardware: calibration dry run (INPUT OPEN). Opt-in: ``EDU09_HW_CALIBRATE=1``.

Never saves: ``save=False`` and the previous calibration is restored afterwards."""

from __future__ import annotations

import pytest
from hw_helpers import ENV_CALIBRATE, skip_unless

from edu09.device.scope import Scope
from edu09.protocol import tables

pytestmark = pytest.mark.hardware


def test_calibration_dry_run(scope: Scope) -> None:
    skip_unless(ENV_CALIBRATE, "calibration needs CH1 disconnected")
    before = scope.calibration
    try:
        cal = scope.calibrate(confirm=True, input_disconnected=True, save=False)
        assert cal.source == "measured"
        assert tables.CAL_BASE_OFS_MIN < cal.base_ofs < tables.CAL_BASE_OFS_MAX
        assert 0.5 < cal.gnd_fit.k < 2.5
        if before.source == "measured":
            assert abs(cal.base_ofs - before.base_ofs) <= 3
            assert abs(cal.gnd_fit.k - before.gnd_fit.k) <= 0.1
    finally:
        scope.set_calibration(before, save=False)
