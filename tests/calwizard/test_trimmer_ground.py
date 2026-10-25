"""Trimmer step: live readout against the measured ground map, and the trimmer log history.

Never opens the real device: every scope is a ``SimTransport`` on a ``VirtualClock``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from calwizard_helpers import drive, make_scope

from edu09 import units
from edu09.calwizard import session as sess_mod
from edu09.calwizard.model import (
    AdjustableSource,
    FixedSource,
    PlanOptions,
    SessionState,
    SetupPhase,
    TrimmerConfig,
)
from edu09.calwizard.planner import TRIMMER_MODEL_GROUND_NOTE, plan_calibration
from edu09.calwizard.session import CalibrationSession
from edu09.calwizard.simref import SimReference
from edu09.device.scope import Scope
from edu09.model import ChannelSettings, Coupling, VoltsPerDiv
from edu09.transport.sim import PHYSICAL_UNITS, SimulatedDevice

AA = FixedSource("AA", 1.5, tolerance_pct=10.0, reversible=True)
BENCH = AdjustableSource("bench", 0.0, 15.0, reversible=True)
TRIM_POS = -1.0  # not used by any other point of the plan below
TRIM_VDIV = 0.5


def _opts(placement: str = "after_zero", zero: bool = True) -> PlanOptions:
    return PlanOptions(
        mode="quick",
        ranges=(TRIM_VDIV,),
        zero=zero,
        trimmer=TrimmerConfig(
            enabled=True, placement=placement, vdiv=TRIM_VDIV, position_div=TRIM_POS
        ),
    )


def _ch(vdiv: float, pos: float) -> ChannelSettings:
    return ChannelSettings(VoltsPerDiv.parse(vdiv), Coupling.DC, pos)


# --------------------------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------------------------


def test_after_zero_trimmer_position_is_in_the_ground_map() -> None:
    plan = plan_calibration([AA, BENCH], _opts())
    kinds = [s.kind for s in plan.setups]
    assert kinds[:2] == ["zero", "trimmer"]
    trim = plan.setups[1]
    assert (trim.points[0].vdiv, trim.points[0].position_div) == (TRIM_VDIV, TRIM_POS)
    others = {(p.vdiv, p.position_div) for s in plan.setups[2:] for p in s.points}
    assert (TRIM_VDIV, TRIM_POS) not in others
    gmap = {(p.vdiv, p.position_div) for p in plan.setups[0].points}
    assert (TRIM_VDIV, TRIM_POS) in gmap
    assert others <= gmap
    assert TRIMMER_MODEL_GROUND_NOTE not in trim.instruction


@pytest.mark.parametrize(("placement", "zero"), [("first", True), ("after_zero", False)])
def test_trimmer_without_prior_zero_says_model_ground(placement: str, zero: bool) -> None:
    plan = plan_calibration([AA, BENCH], _opts(placement, zero))
    trim = next(s for s in plan.setups if s.kind == "trimmer")
    assert TRIMMER_MODEL_GROUND_NOTE in trim.instruction
    for s in plan.setups:
        if s.kind == "zero":
            assert (TRIM_VDIV, TRIM_POS) not in {(p.vdiv, p.position_div) for p in s.points}


# --------------------------------------------------------------------------------------------
# Readout consistency with the gain fit (HW: readout 0 % but fitted g+ 0.988)
# --------------------------------------------------------------------------------------------


def _bump_ground(dev: SimulatedDevice, dacs: set[int], counts: float) -> None:
    """Make the physical ground at offset bytes ``dacs`` differ from any linear ground model."""
    orig = dev._adc_counts

    def bumped(v: Any, t: Any) -> Any:
        c = orig(v, t)
        if dev.config.ofs1 in dacs:
            c = np.clip(c + counts, 0, 255)
        return c

    dev._adc_counts = bumped  # type: ignore[method-assign]


@pytest.fixture
def bumped() -> Iterator[tuple[Scope, SimReference, set[int]]]:
    scope = make_scope("unit1", seed=3)
    try:
        sim = SimReference.for_scope(scope, noise_v=0.0)
        assert sim is not None
        phys = PHYSICAL_UNITS["unit1"].without_gain_correction()
        d = units.offset_dac(_ch(TRIM_VDIV, TRIM_POS), phys)
        dacs = {d - 1, d, d + 1}
        _bump_ground(sim.device, dacs, 1.0)
        yield scope, sim, dacs
    finally:
        scope.close()


def _trim_to_zero(s: CalibrationSession, sim: SimReference) -> float:
    """Turn the sim's trimmer until the readout shows 0 %; returns the final deviation."""
    for _ in range(6):
        tr = s.trimmer_sample()
        sim.device.trim_gain /= 1.0 + tr.dev_pct / 100.0
    return s.trimmer_sample().dev_pct


def _gain_after_trimming(s: CalibrationSession, sim: SimReference) -> tuple[float, Any]:
    v = s.start()
    while v.setup is not None and v.setup.kind != "trimmer":
        assert v.needs == "input_disconnected"
        assert s.confirm_zero(input_disconnected=True).ok
        v = s.view()
    assert v.setup is not None and v.needs == "reading_v"
    gsrc = v.trimmer_ground_source
    assert s.submit_reading(v.sim_reading_v).ok  # type: ignore[arg-type]
    assert abs(_trim_to_zero(s, sim)) < 0.1
    tr = s.trimmer_sample()
    s.trimmer_done()
    drive(s, save=False)
    assert s.state is SessionState.FITTED and s.fit_result is not None
    return s.fit_result.calibration.range_gain[TRIM_VDIV], (gsrc, tr)


def test_trimmed_readout_zero_gives_unit_gain(bumped: tuple[Scope, SimReference, set]) -> None:
    scope, sim, dacs = bumped
    s = CalibrationSession(scope, plan_calibration([AA, BENCH], _opts()), sim=sim)
    g, (gsrc, tr) = _gain_after_trimming(s, sim)
    assert gsrc == "map" and tr.ground_source == "map"
    # the host model is ~1 count off at the trimmer's DAC (the mismatch this test is about)
    zero = next(r for r in s.results if r.kind == "zero")
    assert zero.extra["ground_model_err_counts"] == pytest.approx(1.0, abs=0.3)
    assert units.offset_dac(_ch(TRIM_VDIV, TRIM_POS), s._meas_cal()) in dacs
    gain_pt = next(p for r in s.results if r.kind == "reference" for p in r.points)
    assert gain_pt.dac not in dacs and gain_pt.ground_source == "map"
    assert g == pytest.approx(1.0, abs=0.003)
    done = next(r for r in s.results if r.kind == "trimmer")
    assert done.extra["ground_source"] == "map"
    s.abort()


def test_trimmer_first_falls_back_to_model_ground(
    bumped: tuple[Scope, SimReference, set],
) -> None:
    scope, sim, _ = bumped
    # installed offsets as good as the zero step's: the model is off only by the bump
    scope.set_calibration(PHYSICAL_UNITS["unit1"].without_gain_correction())
    s = CalibrationSession(scope, plan_calibration([AA, BENCH], _opts("first")), sim=sim)
    v = s.start()
    assert v.setup is not None and v.setup.kind == "trimmer"
    assert v.trimmer_ground_source == "model"
    assert v.to_dict()["ground_source"] == "model"
    r = s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert r.extra["ground_source"] == "model"
    assert any("modelled ground" in m for m in r.messages)
    assert _trim_to_zero(s, sim) == pytest.approx(0.0, abs=0.1)
    tr = s.trimmer_sample()
    assert tr.ground_source == "model" and tr.to_dict()["ground_source"] == "model"
    s.trimmer_done()
    drive(s, save=False)
    assert s.fit_result is not None
    # documents the HW symptom: a model-ground trim leaves a ~1 % gain error behind
    assert abs(s.fit_result.calibration.range_gain[TRIM_VDIV] - 1.0) > 0.005
    s.abort()


def test_skipped_zero_falls_back_to_model_ground(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = CalibrationSession(unit1_scope, plan_calibration([AA, BENCH], _opts()), sim=sim_ref)
    s.start()
    s.skip()  # zero
    v = s.view()
    assert v.setup is not None and v.setup.kind == "trimmer"
    assert v.trimmer_ground_source == "model"
    s.abort()


# --------------------------------------------------------------------------------------------
# Trimmer log history
# --------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _live_session(
    scope: Scope, sim: SimReference, clock: _Clock
) -> tuple[CalibrationSession, SimulatedDevice]:
    s = CalibrationSession(scope, plan_calibration([AA, BENCH], _opts()), sim=sim, clock=clock)
    s.start()
    assert s.confirm_zero(input_disconnected=True).ok
    v = s.view()
    s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert s.view().phase is SetupPhase.LIVE
    return s, sim.device


def test_trimmer_history_downsampled_with_summary(
    unit1_scope: Scope, sim_ref: SimReference, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    s, dev = _live_session(unit1_scope, sim_ref, clock)
    dev.trim_gain = 1.1  # start far off
    t_start = clock.t - s._t0
    readings = []
    for i in range(80):
        if i == 41:
            dev.trim_gain = PHYSICAL_UNITS["unit1"].range_gain[TRIM_VDIV]  # best: ~0 %
        elif i == 42:
            dev.trim_gain = 1.03
        readings.append(s.trimmer_sample())
        clock.t += 0.2
    s.trimmer_done()
    done = next(r for r in s.results if r.kind == "trimmer")
    assert done.extra["samples"] == 80
    assert done.extra["initial_dev_pct"] == pytest.approx(readings[0].dev_pct, abs=1e-3)
    assert done.extra["duration_s"] == pytest.approx(79 * 0.2, abs=1e-6)
    s.abort()
    doc = json.loads(s.log_path.read_text())  # type: ignore[union-attr]
    # tail: last 50 at full rate
    tail = doc["trimmer_samples"]
    assert len(tail) == sess_mod.TRIMMER_TAIL and [x["n"] for x in tail] == list(range(31, 81))
    trim = doc["trimmer"]
    (run,) = trim["runs"]
    assert run["samples"] == 80 and run["ground_source"] == "map"
    assert run["t_start"] == pytest.approx(t_start, abs=1e-3)
    assert run["initial_dev_pct"] == pytest.approx(readings[0].dev_pct, abs=1e-3)
    assert run["final_dev_pct"] == pytest.approx(readings[-1].dev_pct, abs=1e-3)
    assert run["best_n"] == 42 and abs(run["best_dev_pct"]) < 0.2
    assert run["duration_s"] == pytest.approx(79 * 0.2, abs=1e-6)
    hist = trim["history"]
    fields = hist["fields"]
    rows = [dict(zip(fields, r, strict=True)) for r in hist["rows"]]
    ns = [r["n"] for r in rows]
    assert ns[0] == 1 and 42 in ns  # first sample, and the improvement
    assert len(rows) < 80 / 2 + 5  # downsampled to ~one per 0.5 s
    for a, b in zip(rows, rows[1:], strict=False):
        improved = b["best_abs_dev_pct"] < a["best_abs_dev_pct"]
        assert improved or b["t"] - a["t"] >= sess_mod.TRIMMER_HIST_INTERVAL_S - 1e-6


def test_trimmer_history_is_capped_and_log_bounded(
    unit1_scope: Scope, sim_ref: SimReference, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sess_mod, "TRIMMER_HIST_MAX", 20)
    clock = _Clock()
    s, _ = _live_session(unit1_scope, sim_ref, clock)
    for _ in range(120):
        s.trimmer_sample()
        clock.t += 0.5
    assert len(s._trim_hist) <= 20
    assert s._trim_hist[0][1] == 1 and s._trim_hist[-1][1] >= 110  # covers the whole trimming
    assert s._trim_hist_dt > sess_mod.TRIMMER_HIST_INTERVAL_S
    s.abort()


def test_full_history_fits_the_log_and_degrades(
    unit1_scope: Scope, sim_ref: SimReference, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    s, _ = _live_session(unit1_scope, sim_ref, clock)
    for _ in range(sess_mod.TRIMMER_TAIL + 5):
        s.trimmer_sample()
        clock.t += 0.25
    row = s._trim_hist[-1]
    s._trim_hist = [[row[0] + i, *row[1:]] for i in range(sess_mod.TRIMMER_HIST_MAX)]
    s.abort()
    text = s.log_path.read_text()  # type: ignore[union-attr]
    assert len(text.encode()) <= sess_mod.LOG_MAX_BYTES
    doc = json.loads(text)
    assert len(doc["trimmer"]["history"]["rows"]) == sess_mod.TRIMMER_HIST_MAX
    assert len(doc["trimmer_samples"]) == sess_mod.TRIMMER_TAIL
    # a tight limit drops detail, still valid JSON
    monkeypatch.setattr(sess_mod, "LOG_MAX_BYTES", 12000)
    s._write_log()
    small = s.log_path.read_text()  # type: ignore[union-attr]
    assert len(small.encode()) <= 12000
    doc = json.loads(small)
    assert len(doc["trimmer"]["history"]["rows"]) <= 50
    assert len(doc["trimmer_samples"]) <= 10
