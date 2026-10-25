"""Review tests for the calibration wizard: abort latency on a real-time simulator, abort/save
race, CLI ``--simulate`` with ADC noise, fit robustness and the simulator's ADC noise knob.

Never opens the real device (SimTransport only); user dirs are isolated by the root conftest.
"""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import storage
from edu09.calwizard import cli
from edu09.calwizard.fit import GainPoint, fit_calibration
from edu09.calwizard.model import AdjustableSource, PlanOptions, SessionState
from edu09.calwizard.planner import plan_calibration
from edu09.calwizard.session import CalibrationSession, active_session
from edu09.calwizard.simref import SimReference
from edu09.device.scope import Scope
from edu09.errors import JobAborted
from edu09.transport.sim import UNIT1, SimTransport, SimulatedDevice, VirtualClock
from edu09.units import Calibration, deflection_to_volts, volts_to_deflection

BENCH = AdjustableSource("bench", 0.0, 15.0, reversible=True)


# --------------------------------------------------------------------------- abort latency


@pytest.fixture
def rt_scope() -> Iterator[Scope]:
    """Real-time simulator (frames take their real duration: 5 ms/div ~ 164 ms per frame)."""
    dev = SimulatedDevice("dc:0v", calibration=UNIT1, seed=3, adc_noise_counts=0.7)
    scope = Scope.open(SimTransport(device=dev), reconnect_interval_s=0.05)
    try:
        yield scope
    finally:
        scope.close()


def test_abort_stops_an_inflight_measurement_quickly(rt_scope: Scope, tmp_path: Path) -> None:
    """abort() from another thread ends a running reference measurement at its next device
    call (well before the 4-frame x N-point job would finish), installs and saves nothing."""
    before = rt_scope.calibration
    plan = plan_calibration([BENCH], PlanOptions(mode="standard"))
    ref = SimReference.for_scope(rt_scope, noise_v=0.0)
    s = CalibrationSession(rt_scope, plan, path=tmp_path / "cal.json", sim=ref)
    s.start()
    s.skip()  # zero
    # pick the first reference setup with 2 points: 2 x (4 frames) + drift frame set ~ 2 s
    while len(s.view().setup.points) < 2:  # type: ignore[union-attr]
        s.skip()
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            s.submit_reading(s.view().sim_reading_v)  # type: ignore[arg-type]
        except BaseException as exc:
            out["exc"] = exc

    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 5
    while not s.view().busy and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.25)  # inside the first capture
    t0 = time.monotonic()
    assert s.abort() is True
    t.join(5)
    elapsed = time.monotonic() - t0
    assert not t.is_alive()
    assert isinstance(out.get("exc"), JobAborted)
    assert elapsed < 0.6, f"abort took {elapsed:.2f} s"
    assert s.state is SessionState.ABORTED
    assert rt_scope.calibration == before
    assert not (tmp_path / "cal.json").exists()
    assert active_session(rt_scope) is None
    # the device is usable right away
    assert rt_scope.acquire(timeout_s=3.0) is not None


# --------------------------------------------------------------------------- abort vs save race


def _ready_session(tmp_path: Path) -> tuple[Scope, CalibrationSession]:
    clock = VirtualClock()
    dev = SimulatedDevice("dc:0v", clock=clock, calibration=UNIT1, seed=1, adc_noise_counts=0.7)
    scope = Scope.open(SimTransport(device=dev, sleep=clock.sleep), reconnect_interval_s=0.01)
    ref = SimReference.for_scope(scope, noise_v=0.0)
    plan = plan_calibration([BENCH], PlanOptions(mode="quick", ranges=(1.0,)))
    s = CalibrationSession(scope, plan, path=tmp_path / "cal.json", sim=ref)
    s.start()
    while s.view().state is SessionState.IN_SETUP:
        v = s.view()
        if v.needs == "input_disconnected":
            s.confirm_zero(input_disconnected=True)
        else:
            s.submit_reading(v.sim_reading_v)  # type: ignore[arg-type]
    s.fit()
    return scope, s


def test_abort_during_save_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abort() that arrives while save() writes must leave the session terminal (not SAVED
    and still registered as the scope's active session, which would block every later wizard
    on this scope)."""
    scope, s = _ready_session(tmp_path)
    try:
        real = scope.set_calibration

        def racing(*a: Any, **kw: Any) -> Any:
            res = real(*a, **kw)
            assert s.abort() is True  # e.g. the GUI's Cancel while the file is written
            return res

        monkeypatch.setattr(scope, "set_calibration", racing)
        s.save(confirm=True)  # the write happened; the abort keeps the session terminal
        assert s.state.terminal, s.state
        assert active_session(scope) is None
        assert s.summary()["saved"] is not None  # it was written; the summary says so
        # a new session can start on this scope
        plan = plan_calibration([], PlanOptions())
        CalibrationSession(scope, plan).start()
    finally:
        scope.close()


# --------------------------------------------------------------------------- CLI --simulate


def _run_cli(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()

    def ask(prompt: str) -> str:
        raise EOFError

    return cli.main(argv, ask=ask, out=out), out.getvalue()


def test_cli_simulate_uses_adc_noise_and_recovers_unit1(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    code, text = _run_cli(
        [
            "--simulate",
            "--sim-unit",
            "unit1",
            "--sim-clock",
            "virtual",
            "--yes",
            "--adjustable",
            "0..15,rev,label=bench",
            "--calibration",
            str(path),
        ]
    )
    assert code == 0, text
    assert "verify passed" in text
    cal = storage.load_calibration(path)
    for v in (0.1, 0.2, 0.5, 1.0, 2.0):  # 5 V/div positive is a 2.4 div fallback point
        assert cal.range_gain_for(v) == pytest.approx(UNIT1.range_gain_for(v), abs=0.002), v
        assert cal.range_gain_neg_for(v) == pytest.approx(UNIT1.range_gain_neg_for(v), abs=0.003)
    assert cal.range_gain_for(5.0) == pytest.approx(UNIT1.range_gain_for(5.0), abs=0.006)
    # the 0.5 V/div expansion was fitted (6 div below ground within 0.03 div)
    c = -float(volts_to_deflection(-3.0, 0.5, UNIT1))
    assert float(deflection_to_volts(-c, 0.5, cal)) / 0.5 == pytest.approx(-6.0, abs=0.03)


def test_cli_simulate_device_has_adc_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[SimulatedDevice] = []
    real_open = Scope.open

    def spy(transport: Any = None, **kw: Any) -> Scope:
        seen.append(transport.device)
        return real_open(transport, **kw)

    monkeypatch.setattr(Scope, "open", staticmethod(spy))
    args = cli._build_parser().parse_args(["--simulate", "--sim-clock", "virtual"])
    scope, _sim = cli._open_scope(args)
    try:
        assert seen and seen[0].adc_noise_counts == cli.SIM_ADC_NOISE_COUNTS > 0
    finally:
        scope.close()


# --------------------------------------------------------------------------- sim noise knob


def _dc_raw(dev: SimulatedDevice, scope: Scope, volts: float) -> np.ndarray:
    dev.signal = dev.signal.parse(f"dc:{volts}v")
    from edu09.device import calibration as devcal
    from edu09.model import ChannelSettings, VoltsPerDiv

    ch = ChannelSettings(VoltsPerDiv.V2)
    return scope.run_exclusive(
        lambda io: devcal.capture_window(io, ch, scope.calibration, 4), timeout_s=30
    )


def test_sim_adc_noise_dithers_and_default_is_noiseless() -> None:
    clock = VirtualClock()
    quiet = SimulatedDevice("dc:0v", clock=clock, seed=1)
    assert quiet.adc_noise_counts == 0.0
    noisy = SimulatedDevice("dc:0v", clock=clock, seed=1, adc_noise_counts=0.7)
    with pytest.raises(ValueError):
        SimulatedDevice("dc:0v", adc_noise_counts=-1)
    with pytest.raises(ValueError):
        SimulatedDevice("dc:0v", adc_noise_counts=float("nan"))
    for dev, noisy_expected in ((quiet, False), (noisy, True)):
        scope = Scope.open(SimTransport(device=dev, sleep=clock.sleep), reconnect_interval_s=0.01)
        try:
            x = _dc_raw(dev, scope, 0.31)  # 4.96 counts above ground at 2 V/div
            if noisy_expected:
                assert 0.5 < float(x.std()) < 0.9
                # the dithered mean resolves the fraction of a count
                assert float(x.mean()) - 127.0 == pytest.approx(0.31 * 32 / 2, abs=0.1)
            else:
                assert float(x.std()) == 0.0
        finally:
            scope.close()


# --------------------------------------------------------------------------- fit robustness


def _gp(vdiv: float, div: float, err_pct: float = 0.0, phys: Calibration = UNIT1) -> GainPoint:
    v_ref = div * vdiv
    v_read = float(volts_to_deflection(v_ref, vdiv, phys)) * vdiv / 32.0
    return GainPoint(vdiv, 0.0, v_ref * (1 + err_pct / 100), v_read, setup_id="S")


def test_fit_single_outlier_is_flagged() -> None:
    """Least squares has no outlier rejection: a mistyped reading (2 % off) shifts the gain;
    the residual warning must point at it so the user can retry."""
    pts = [_gp(1.0, 5.0), _gp(1.0, 2.5), _gp(1.0, 5.5, err_pct=2.0)]
    res = fit_calibration(Calibration(), pts, previous=Calibration())
    rf = res.ranges[1.0]
    assert rf.after_err_pct_max is not None and rf.after_err_pct_max > 0.3
    assert any("off after the fit" in w for w in rf.warnings)


def test_fit_compression_below_threshold_not_stored() -> None:
    """Documented spec behaviour: only a positive excess (expansion) creates knots; a pure
    compression (e < 0) is left to the residual warnings."""
    comp = Calibration(
        range_gain_neg={0.5: 0.97},
        neg_nonlinearity={0.5: ((4.0, 0.0), (6.0, -0.3))},
    )
    pts = [_gp(0.5, -3.0, phys=comp), _gp(0.5, -2.0, phys=comp), _gp(0.5, -6.0, phys=comp)]
    res = fit_calibration(Calibration(), pts, previous=Calibration())
    assert res.calibration.nonlinearity_for(0.5) == ()
    assert any("off after the fit" in w for w in res.ranges[0.5].warnings)
