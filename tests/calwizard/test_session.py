"""Calibration wizard session (spec 3) on a simulated UNIT1 scope."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from calwizard_helpers import drive

from edu09 import storage, units
from edu09.calwizard import session as sess_mod
from edu09.calwizard.model import (
    AdjustableSource,
    FixedSource,
    PlanOptions,
    QualityCode,
    SessionState,
    SetupPhase,
    TrimmerConfig,
)
from edu09.calwizard.planner import plan_calibration
from edu09.calwizard.session import CalibrationSession, active_session
from edu09.calwizard.simref import SimReference
from edu09.device import calibration as devcal
from edu09.device.scope import Scope
from edu09.errors import CalibrationError, JobAborted, UnsafeOperation

AA = FixedSource("AA", 1.5, tolerance_pct=10.0, reversible=True)
BENCH = AdjustableSource("bench", 0.0, 15.0, reversible=True)
SMALL = PlanOptions(mode="quick", ranges=(0.5, 1.0))


def _session(
    scope: Scope,
    sim: SimReference | None,
    sources: Any = (AA,),
    opts: PlanOptions = SMALL,
    path: Path | None = None,
) -> CalibrationSession:
    return CalibrationSession(scope, plan_calibration(list(sources), opts), path=path, sim=sim)


def _to_reference(s: CalibrationSession, kind: str = "reference") -> None:
    """Start and advance until the current setup is of ``kind``."""
    if s.state is SessionState.PLANNED:
        s.start()
    for _ in range(50):
        v = s.view()
        assert v.setup is not None
        if v.setup.kind == kind:
            return
        if v.needs == "input_disconnected":
            assert s.confirm_zero(input_disconnected=True).ok
        else:
            s.skip()
    raise AssertionError("setup kind not found")


@pytest.fixture
def blocker(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, threading.Event]]:
    """Makes the next capture_window call (on the worker) wait until ``go`` is set."""
    entered, go = threading.Event(), threading.Event()
    real = devcal.capture_window
    armed = {"on": True}

    def slow(*a: Any, **k: Any) -> Any:
        if armed["on"]:
            armed["on"] = False
            entered.set()
            go.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(devcal, "capture_window", slow)
    yield {"entered": entered, "go": go}
    go.set()


# --------------------------------------------------------------------------------------------
# Happy path and basic API
# --------------------------------------------------------------------------------------------


def test_small_session_reaches_done_and_applies(unit1_scope: Scope, sim_ref: SimReference) -> None:
    before = unit1_scope.calibration
    s = _session(unit1_scope, sim_ref)
    v = s.start()
    assert v.state is SessionState.IN_SETUP and v.setup is not None and v.setup.kind == "zero"
    assert v.needs == "input_disconnected"
    assert active_session(unit1_scope) is s
    drive(s)
    assert s.state is SessionState.DONE
    assert active_session(unit1_scope) is None
    summ = s.summary()
    assert summ["verify"] == "pass"
    assert summ["saved"]["saved"] is False  # sim scope without path: apply only
    assert unit1_scope.calibration != before
    assert unit1_scope.calibration.source == "measured"
    assert 0.5 in unit1_scope.calibration.range_gain
    assert s.log_path is not None and s.log_path.is_file()
    json.dumps(summ)
    json.dumps(s.view().to_dict())
    assert len(json.dumps(summ)) < 8192


def test_zero_needs_confirmation_and_installs_nothing(
    unit1_scope: Scope, sim_ref: SimReference
) -> None:
    before = unit1_scope.calibration
    s = _session(unit1_scope, sim_ref)
    s.start()
    with pytest.raises(UnsafeOperation):
        s.confirm_zero(input_disconnected=False)
    r = s.confirm_zero(input_disconnected=True)
    assert r.ok and r.kind == "zero"
    assert r.extra["ground_points"] == len(s.plan.setups[0].points)
    assert r.extra["ground_model_err_counts"] < 3
    assert unit1_scope.calibration == before  # nothing installed before save
    s.abort()


def test_sign_rule_note(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    while s.view().setup.volts > 0:  # type: ignore[union-attr]
        s.skip()
    v = s.view()
    assert v.sim_reading_v is not None and v.sim_reading_v < 0
    r = s.submit_reading(abs(v.sim_reading_v))
    assert r.ok
    assert any("assuming" in msg for msg in r.messages)
    assert r.reading_v == pytest.approx(v.sim_reading_v)
    s.abort()


@pytest.mark.parametrize("bad", [float("nan"), 0.0, 31.0, -40.0, True])
def test_reading_validation(unit1_scope: Scope, sim_ref: SimReference, bad: Any) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    with pytest.raises(ValueError):
        s.submit_reading(bad)
    s.abort()


# --------------------------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------------------------


def _first_point_codes(r: Any) -> list[QualityCode]:
    return [p.code for p in r.points]


def test_polarity_mistake(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    while s.view().setup.volts > 0:  # type: ignore[union-attr]
        s.skip()
    v = s.view()
    sim_ref.connect(abs(v.setup.volts))  # type: ignore[union-attr,arg-type]  # forgot the swap
    r = s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert r.status == "failed"
    assert QualityCode.POLARITY in _first_point_codes(r)
    assert any("swap the leads" in p.hint for p in r.points)
    assert s.view().phase is SetupPhase.FAILED
    assert s.view().setup.id == v.setup.id  # type: ignore[union-attr]
    # fixing the leads and submitting again completes the setup
    sim_ref.connect(v.setup.volts)  # type: ignore[union-attr,arg-type]
    assert s.submit_reading(v.sim_reading_v).ok  # type: ignore[arg-type]
    s.abort()


def test_x10_probe_reading_mismatch(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref, sources=(BENCH,), opts=PlanOptions(ranges=(0.5, 1.0)))
    _to_reference(s)
    # a setup whose first point still shows 1/10 of its deflection (>= 0.3 div)
    while abs(s.view().setup.points[0].expected_div) < 3.5:  # type: ignore[union-attr]
        s.skip()
    v = s.view()
    assert v.setup is not None and v.setup.volts is not None
    reading = sim_ref.connect(v.setup.volts / 10)  # the scope sees 1/10
    r = s.submit_reading(reading * 10)
    assert r.status == "failed"
    assert r.points[0].code is QualityCode.READING_MISMATCH
    assert "x1" in r.points[0].hint
    assert all("not measured" in p.message for p in r.points[1:])
    s.abort()


def test_wrong_supply_value_out_of_window(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref, sources=(BENCH,), opts=SMALL)
    _to_reference(s)
    v = s.view()
    assert v.setup is not None and v.setup.volts is not None
    r = s.submit_reading(sim_ref.connect(v.setup.volts * 2))
    assert r.status == "failed"
    assert all(p.code is QualityCode.OUT_OF_WINDOW for p in r.points)
    assert any("set the source to" in msg for msg in r.messages)
    s.abort()


def test_noisy_source(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    v = s.view()
    sim_ref.noise_v = 0.2  # ~13 counts rms at 0.5 V/div
    r = s.submit_reading(sim_ref.connect(v.setup.volts))  # type: ignore[union-attr,arg-type]
    assert r.status == "failed"
    assert QualityCode.NOISY in _first_point_codes(r)
    s.abort()


def test_saturation(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    v = s.view()
    sim_ref.connect(v.setup.volts * 10)  # type: ignore[union-attr,operator]
    r = s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert r.status == "failed"
    assert QualityCode.SATURATED in _first_point_codes(r)
    s.abort()


def test_no_signal(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    v = s.view()
    sim_ref.disconnect()
    r = s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert r.status == "failed"
    assert QualityCode.NO_SIGNAL in _first_point_codes(r)
    s.abort()


# --------------------------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------------------------


def test_skip_and_retry(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    first = s.view().setup
    assert first is not None
    sim_ref.disconnect()
    r = s.submit_reading(1.5)
    assert r.status == "failed" and s.view().phase is SetupPhase.FAILED
    v = s.retry()  # discard the failed attempt
    assert v.phase is SetupPhase.AWAIT_INPUT and v.setup == first
    assert v.sim_reading_v is not None  # re-connected
    assert s.submit_reading(v.sim_reading_v).ok
    assert s.view().setup != first
    # retry right after completion re-runs the completed setup
    v = s.retry()
    assert v.setup == first
    assert all(r.setup_id != first.id for r in s.results)
    v = s.skip()
    assert [r.status for r in s.results if r.setup_id == first.id] == ["skipped"]
    s.abort()


def test_skip_zero_uses_model_ground(unit1_scope: Scope, sim_ref: SimReference) -> None:
    unit1_scope.set_calibration(units.Calibration.typical())  # offsets close to the unit's
    s = _session(unit1_scope, sim_ref)
    s.start()
    s.skip()  # zero
    drive(s, save=False)
    assert s.state is SessionState.FITTED
    pts = s.gain_points()
    assert pts and all(p.ground_source == "model" for p in pts)
    fit = s.fit()  # idempotent
    assert any("model" in w for w in fit.warnings) or any(
        "model" in w for rf in fit.ranges.values() for w in rf.warnings
    )
    s.abort()


def test_trimmer_live_phase(unit1_scope: Scope, sim_ref: SimReference) -> None:
    opts = PlanOptions(mode="quick", ranges=(0.5,), trimmer=TrimmerConfig(enabled=True))
    s = _session(unit1_scope, sim_ref, opts=opts)
    s.start()
    assert s.confirm_zero(input_disconnected=True).ok
    v = s.view()
    assert v.setup is not None and v.setup.kind == "trimmer" and v.needs == "reading_v"
    with pytest.raises(CalibrationError):
        s.trimmer_sample()
    r = s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    assert r.ok and s.view().phase is SetupPhase.LIVE and s.view().needs == "done"
    t1 = s.trimmer_sample()
    assert t1.n == 1 and t1.trend == "same"
    assert t1.v_ref == pytest.approx(1.5, abs=0.01)
    dev = sim_ref.device
    dev.trim_gain = 1.05  # turning the trimmer the wrong way
    t2 = s.trimmer_sample()
    assert t2.dev_pct > t1.dev_pct + 3 and t2.trend == "worse"
    dev.trim_gain = 1.0 / (1 + t1.dev_pct / 100)  # turned to cancel the deviation
    t3 = s.trimmer_sample()
    assert abs(t3.dev_pct) < 0.5 and t3.trend == "better"
    assert t3.best_abs_dev_pct == pytest.approx(abs(t3.dev_pct))
    v = s.trimmer_done()
    assert v.setup is not None and v.setup.kind == "reference"
    tr = next(r for r in s.results if r.kind == "trimmer")
    assert tr.extra["samples"] == 3
    s.abort()
    log = json.loads(s.log_path.read_text())  # type: ignore[union-attr]
    assert len(log["trimmer_samples"]) == 3


# --------------------------------------------------------------------------------------------
# Abort and concurrency
# --------------------------------------------------------------------------------------------


def test_abort_during_measurement(
    unit1_scope: Scope, sim_ref: SimReference, blocker: dict[str, threading.Event], tmp_path: Path
) -> None:
    before = unit1_scope.calibration
    path = tmp_path / "cal.json"
    s = _session(unit1_scope, sim_ref, path=path)
    s.start()
    s.skip()  # zero: keep it short; the blocker hits the first reference capture
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            s.submit_reading(s.view().sim_reading_v)  # type: ignore[arg-type]
        except BaseException as exc:
            out["exc"] = exc

    t = threading.Thread(target=run)
    t.start()
    assert blocker["entered"].wait(10)
    assert s.view().busy
    with pytest.raises(CalibrationError, match="busy"):
        s.skip()
    with pytest.raises(CalibrationError, match="busy"):
        s.submit_reading(1.5)
    assert s.abort() is True
    assert s.state is SessionState.ABORTED
    blocker["go"].set()
    t.join(10)
    assert isinstance(out.get("exc"), JobAborted)
    assert s.state is SessionState.ABORTED
    assert unit1_scope.calibration == before
    assert not path.exists()
    assert active_session(unit1_scope) is None
    assert s.log_path is not None and s.log_path.is_file()
    assert s.abort() is False
    # the scope still works afterwards
    assert unit1_scope.acquire(timeout_s=2.0) is not None


def test_scope_close_aborts(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    s.start()
    unit1_scope.close()
    with pytest.raises(JobAborted):
        s.confirm_zero(input_disconnected=True)
    assert s.state is SessionState.ABORTED


def test_second_session_refused(unit1_scope: Scope, sim_ref: SimReference) -> None:
    a = _session(unit1_scope, sim_ref)
    a.start()
    b = _session(unit1_scope, sim_ref)
    with pytest.raises(CalibrationError, match="another calibration session"):
        b.start()
    a.abort()
    b.start()
    assert active_session(unit1_scope) is b
    b.abort()
    with pytest.raises(CalibrationError):
        b.start()  # terminal sessions cannot restart


def test_calibration_changed_refuses_save(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    drive(s, save=False)
    assert s.state is SessionState.FITTED
    unit1_scope.set_calibration(units.Calibration.typical())  # another client
    with pytest.raises(CalibrationError, match="calibration changed"):
        s.save(confirm=True)
    with pytest.raises(UnsafeOperation):
        s.save(confirm=False)
    s.abort()


def test_state_guards(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    with pytest.raises(CalibrationError):
        s.fit()
    s.start()
    with pytest.raises(CalibrationError):
        s.submit_reading(1.5)  # zero step is current
    with pytest.raises(CalibrationError):
        s.save(confirm=True)
    with pytest.raises(CalibrationError):
        s.revert()
    s.abort()


# --------------------------------------------------------------------------------------------
# Save, revert, logs
# --------------------------------------------------------------------------------------------


def test_save_backup_and_revert(unit1_scope: Scope, sim_ref: SimReference, tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    storage.save_calibration(unit1_scope.calibration, path)
    original = path.read_text()
    before = unit1_scope.calibration
    s = _session(unit1_scope, sim_ref, path=path)
    drive(s)
    assert s.state is SessionState.DONE
    saved = s.summary()["saved"]
    assert saved["saved"] is True and Path(saved["path"]) == path
    assert Path(saved["backup_path"]).read_text() == original
    new = storage.load_calibration(path)
    assert new == unit1_scope.calibration
    assert new.meta is not None and new.meta["tool"] == "edu09-calibrate"
    res = s.revert()
    assert unit1_scope.calibration == before
    assert storage.load_calibration(path) == before
    assert res.backup_path is not None and res.backup_path.is_file()
    with pytest.raises(CalibrationError):
        s.revert()


def test_log_written_and_pruned(unit1_scope: Scope, sim_ref: SimReference) -> None:
    d = storage.default_data_dir() / sess_mod.LOG_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    for i in range(25):
        (d / f"20000101T0000{i:02d}000000.json").write_text("{}")
    s = _session(unit1_scope, sim_ref)
    drive(s)
    files = sorted(d.glob("*.json"))
    assert len(files) == sess_mod.LOG_KEEP
    assert s.log_path in files
    text = s.log_path.read_text()  # type: ignore[union-attr]
    assert len(text.encode()) <= sess_mod.LOG_MAX_BYTES
    doc = json.loads(text)
    assert doc["state"] == "done"
    assert doc["plan"]["setups"]
    assert doc["fit"]["ranges"]
    ref = next(r for r in doc["results"] if r["kind"] == "reference")
    assert {"raw_mean", "std", "dac", "ground", "v_ref", "v_read"} <= set(ref["points"][0])


def test_log_size_cap(
    unit1_scope: Scope, sim_ref: SimReference, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sess_mod, "LOG_MAX_BYTES", 3000)
    s = _session(unit1_scope, sim_ref)
    drive(s)
    assert len(s.log_path.read_bytes()) <= 3001  # type: ignore[union-attr]


def test_verify_detects_bad_calibration(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)

    def stop(x: CalibrationSession) -> bool:
        return x.state is SessionState.SAVED

    drive(s, stop=stop)
    assert s.state is SessionState.SAVED
    # meter now reads 3 % high: the verify must fail (and the session still finishes)
    v = s.view()
    assert v.setup is not None and v.setup.kind == "verify"
    r = s.submit_reading(v.sim_reading_v * 1.03)  # type: ignore[operator]
    assert r.extra["verify"] == "fail"
    while s.state is SessionState.SAVED:  # the positive verify follows
        v = s.view()
        assert v.setup is not None and v.setup.kind == "verify"
        s.submit_reading(v.sim_reading_v * 1.03)  # type: ignore[operator]
    assert s.state is SessionState.DONE
    assert s.summary()["verify"] == "fail"


def test_points_are_measured_through_raw_frontend(
    unit1_scope: Scope, sim_ref: SimReference
) -> None:
    """v_read uses gain 1 (no NL): it equals the true volts divided by the physical gain."""
    s = _session(unit1_scope, sim_ref)
    drive(s, save=False)
    phys = sim_ref.device.calibration
    for p in s.gain_points():
        g = phys.range_gain_for(p.vdiv) if p.v_ref > 0 else phys.range_gain_neg_for(p.vdiv)
        assert p.v_read * g == pytest.approx(p.v_ref, rel=0.004)
    assert np.isfinite([p.noise_counts for p in s.gain_points()]).all()
    s.abort()


def test_busy_view_is_cheap(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    s.start()
    t0 = time.perf_counter()
    for _ in range(200):
        s.view()
    assert time.perf_counter() - t0 < 1.0
    s.abort()


def test_first_point_mismatch_skips_the_rest(
    unit1_scope: Scope, sim_ref: SimReference, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sess_mod, "NO_SIGNAL_DIV", 0.01)  # let 1/10 of 2.5 div count as signal
    opts = PlanOptions(mode="quick", ranges=(1.0, 2.0))
    s = _session(unit1_scope, sim_ref, sources=(FixedSource("USB", 5.0),), opts=opts)
    s.start()
    s.skip()
    v = s.view()
    assert v.setup is not None and v.setup.volts == 5.0 and len(v.setup.points) == 2
    r = s.submit_reading(sim_ref.connect(0.5) * 10)  # x10 probe
    assert [p.code for p in r.points] == [QualityCode.READING_MISMATCH] * 2
    assert r.points[0].raw_mean is not None
    assert r.points[1].raw_mean is None and "not measured" in r.points[1].message
    s.abort()


def test_zero_only_session_keeps_gains(unit1_scope: Scope, sim_ref: SimReference) -> None:
    unit1_scope.set_calibration(units.Calibration.typical())
    gains = dict(unit1_scope.calibration.range_gain)
    s = CalibrationSession(unit1_scope, plan_calibration([]), sim=sim_ref)
    drive(s)
    assert s.state is SessionState.DONE
    cal = unit1_scope.calibration
    assert cal.source == "measured"
    assert dict(cal.range_gain) == gains  # uncalibrated="keep"
    assert s.gain_points() == ()


def test_empty_plan_goes_straight_to_fit(unit1_scope: Scope) -> None:
    s = CalibrationSession(unit1_scope, plan_calibration([], PlanOptions(zero=False)))
    v = s.start()
    assert v.state is SessionState.READY_TO_FIT and v.setup is None and v.needs == "none"
    s.abort()


def test_reading_far_from_scope_is_flagged_as_possible_typo(
    unit1_scope: Scope, sim_ref: SimReference
) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    v = s.view()
    assert v.sim_reading_v is not None
    r = s.submit_reading(v.sim_reading_v * 1.07)  # 7 % off, but inside the 15 % hard limit
    assert r.status == "warning"
    assert 4.0 < r.extra["reading_suspect"] < 12.0
    assert any("type it correctly" in msg for msg in r.messages)


def test_good_reading_is_not_flagged(unit1_scope: Scope, sim_ref: SimReference) -> None:
    s = _session(unit1_scope, sim_ref)
    _to_reference(s)
    v = s.view()
    assert v.sim_reading_v is not None
    r = s.submit_reading(v.sim_reading_v * 1.01)
    assert "reading_suspect" not in r.extra


def test_exception_after_the_file_was_written_is_recorded(
    unit1_scope: Scope, sim_ref: SimReference, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cal.json"
    storage.save_calibration(unit1_scope.calibration, path)  # an old file to back up
    s = _session(unit1_scope, sim_ref, path=path)
    drive(s, save=False)
    real = type(unit1_scope).set_calibration

    def boom(self: Scope, cal: units.Calibration, **kw: Any) -> None:
        real(self, cal, **kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(type(unit1_scope), "set_calibration", boom)
    with pytest.raises(KeyboardInterrupt):
        s.save(confirm=True)
    ps = s.partial_save
    assert ps is not None and ps.path == path and ps.backup_path is not None
    assert ps.backup_path.exists()
