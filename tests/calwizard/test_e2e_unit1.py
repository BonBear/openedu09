"""End to end: the wizard recovers the simulated unit 1 front end (spec 6, session happy path)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from calwizard_helpers import drive, make_scope

from edu09 import storage
from edu09.calwizard.model import AdjustableSource, PlanOptions, SessionState
from edu09.calwizard.planner import plan_calibration
from edu09.calwizard.session import CalibrationSession
from edu09.calwizard.simref import SimReference
from edu09.transport.sim import PHYSICAL_UNITS

UNIT1 = PHYSICAL_UNITS["unit1"]
GOOD_RANGES = (0.1, 0.2, 0.5, 1.0, 2.0)  # 5 V/div positive is only a fallback below 15 V


def _knot_excess(knots: tuple[tuple[float, float], ...], r: float) -> float:
    rs = [k[0] for k in knots]
    es = [k[1] for k in knots]
    if r <= rs[-1]:
        return float(np.interp(r, rs, es))
    slope = (es[-1] - es[-2]) / (rs[-1] - rs[-2])
    return es[-1] + slope * (r - rs[-1])


@pytest.mark.parametrize("seed", [1, 2])
def test_standard_run_recovers_unit1(tmp_path: Path, seed: int) -> None:
    scope = make_scope("unit1", seed=seed)
    try:
        path = tmp_path / "calibration.json"
        storage.save_calibration(scope.calibration, path)  # an existing file gets backed up
        sim = SimReference.for_scope(scope, noise_v=0.0)
        bench = AdjustableSource("bench", 0.0, 15.0, reversible=True)
        plan = plan_calibration([bench], PlanOptions(mode="standard"))
        s = CalibrationSession(scope, plan, path=path, sim=sim)
        drive(s)
        assert s.state is SessionState.DONE
        summ = s.summary()
        assert summ["verify"] == "pass"
        assert summ["saved"]["saved"] is True
        assert Path(summ["saved"]["backup_path"]).is_file()
        cal = storage.load_calibration(path)
        assert cal == scope.calibration
        assert cal.meta is not None and cal.meta["tool"] == "edu09-calibrate"
        for v in GOOD_RANGES:
            assert cal.range_gain_for(v) == pytest.approx(UNIT1.range_gain_for(v), abs=0.001)
            assert cal.range_gain_neg_for(v) == pytest.approx(
                UNIT1.range_gain_neg_for(v), abs=0.002
            )
        assert cal.range_gain_neg_for(5.0) == pytest.approx(
            UNIT1.range_gain_neg_for(5.0), abs=0.003
        )
        for v in (0.2, 0.5):
            got, want = cal.nonlinearity_for(v), UNIT1.nonlinearity_for(v)
            assert got, f"no NL fitted at {v} V/div"
            assert _knot_excess(got, 6.0) == pytest.approx(_knot_excess(want, 6.0), abs=0.03)
        for v in (0.1, 1.0):
            assert not cal.nonlinearity_for(v)
    finally:
        scope.close()
