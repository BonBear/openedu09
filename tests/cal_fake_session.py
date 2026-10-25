"""A scripted stand-in for ``edu09.calwizard.CalibrationSession`` (same public API) for the GUI
and MCP front-end tests. It never touches the device: measurements return synthetic results.

``gate``: if set to a ``threading.Event``, measuring calls block until it is set (or the
session is aborted), so tests can abort mid-measurement. ``fail_next``: the next measurement
fails (phase FAILED, retryable).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from edu09.calwizard import model as m
from edu09.calwizard.fit import FitResult, RangeFit
from edu09.errors import CalibrationError, JobAborted, UnsafeOperation

_ACTIVE: dict[int, FakeSession] = {}


class FakeSession:
    instances: list[FakeSession] = []

    def __init__(
        self, scope: Any, plan: m.Plan, *, path: Any = None, sim: Any = None, clock: Any = None
    ) -> None:
        self.scope = scope
        self.plan = plan
        self.path = path
        self.sim = sim
        self.state = m.SessionState.PLANNED
        self.index = 0
        self.phase: m.SetupPhase | None = None
        self.last_result: m.SetupResult | None = None
        self._results: list[m.SetupResult] = []
        self.calls: list[str] = []
        self.gate: threading.Event | None = None
        self.fail_next = False
        self.trim_n = 0
        self.aborted = threading.Event()
        self.saved: m.SaveResult | None = None
        self._op = threading.Lock()
        FakeSession.instances.append(self)

    # -- helpers -------------------------------------------------------------------------
    @property
    def results(self) -> tuple[m.SetupResult, ...]:
        return tuple(self._results)

    @property
    def current(self) -> m.SetupView | None:
        return self.view() if self.state == m.SessionState.IN_SETUP else None

    def _setup(self) -> m.Setup | None:
        if self.state not in (m.SessionState.IN_SETUP, m.SessionState.SAVED):
            return None
        if self.index >= len(self.plan.setups):
            return None
        return self.plan.setups[self.index]

    def _needs(self, s: m.Setup | None) -> str:
        if s is None:
            return "none"
        if s.kind == "zero":
            return "input_disconnected"
        if s.kind == "trimmer" and self.phase == m.SetupPhase.LIVE:
            return "done"
        if s.needs_reading:
            return "reading_v"
        return "none"

    def view(self) -> m.SetupView:
        s = self._setup()
        sim_v = None
        if self.sim is not None and s is not None and s.volts is not None:
            sim_v = round(s.volts * 1.001, 3)
        return m.SetupView(
            state=self.state,
            index=self.index,
            total=len(self.plan.setups),
            setup=s,
            phase=self.phase if s is not None else None,
            needs=self._needs(s),  # type: ignore[arg-type]
            last_result=self.last_result,
            sim_reading_v=sim_v,
            busy=self._op.locked(),
        )

    def _enter(self, name: str) -> None:
        if not self._op.acquire(blocking=False):
            raise CalibrationError("calibration session busy")
        self.calls.append(name)
        if self.state.terminal:
            self._op.release()
            raise CalibrationError(f"session is {self.state}")

    def _wait_gate(self) -> None:
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if self.aborted.is_set():
                    break
        if self.aborted.is_set():
            self.state = m.SessionState.ABORTED
            _ACTIVE.pop(id(self.scope), None)
            raise JobAborted("calibration aborted")

    def _advance(self) -> None:
        self.index += 1
        self.phase = m.SetupPhase.AWAIT_INPUT
        setups = self.plan.setups
        if self.state == m.SessionState.IN_SETUP:
            if self.index >= len(setups) or setups[self.index].kind == "verify":
                self.state = m.SessionState.READY_TO_FIT
        elif self.state == m.SessionState.SAVED and self.index >= len(setups):
            self.state = m.SessionState.DONE
            _ACTIVE.pop(id(self.scope), None)

    def _result(self, s: m.Setup, v: float | None) -> m.SetupResult:
        failed = self.fail_next
        self.fail_next = False
        pts = tuple(
            m.PointResult(
                vdiv=p.vdiv,
                position_div=p.position_div,
                purpose=p.purpose,
                status="failed" if failed else "ok",
                code=m.QualityCode.NO_SIGNAL if failed else m.QualityCode.OK,
                message="source not connected?" if failed else "",
                deflection_div=None if v is None else v / p.vdiv,
                err_pct=None if v is None else 0.1,
            )
            for p in s.points
        )
        res = m.SetupResult(s.id, s.kind, "failed" if failed else "ok", pts, (), v)
        self._results.append(res)
        self.last_result = res
        if failed:
            self.phase = m.SetupPhase.FAILED
        return res

    # -- API -----------------------------------------------------------------------------
    def start(self) -> m.SetupView:
        if _ACTIVE.get(id(self.scope)) is not None:
            raise CalibrationError("another calibration session is active on this scope")
        _ACTIVE[id(self.scope)] = self
        self.state = m.SessionState.IN_SETUP
        self.phase = m.SetupPhase.AWAIT_INPUT
        return self.view()

    def confirm_zero(self, *, input_disconnected: bool) -> m.SetupResult:
        if input_disconnected is not True:
            raise UnsafeOperation("input must be disconnected")
        self._enter("confirm_zero")
        try:
            self._wait_gate()
            s = self._setup()
            assert s is not None and s.kind == "zero"
            res = self._result(s, None)
            if res.ok:
                self._advance()
            return res
        finally:
            self._op.release()

    def submit_reading(self, volts: float) -> m.SetupResult:
        self._enter(f"submit_reading({volts:g})")
        try:
            self._wait_gate()
            s = self._setup()
            assert s is not None and s.needs_reading
            res = self._result(s, volts)
            if res.ok:
                if s.kind == "trimmer":
                    self.phase = m.SetupPhase.LIVE
                else:
                    self._advance()
            return res
        finally:
            self._op.release()

    def trimmer_sample(self) -> m.TrimmerReading:
        self._enter("trimmer_sample")
        try:
            self._wait_gate()
            self.trim_n += 1
            return m.TrimmerReading(1.49, 1.5, -0.01, -0.67, 175.0, 0.4, 0.5, "better", self.trim_n)
        finally:
            self._op.release()

    def trimmer_done(self) -> m.SetupView:
        self._enter("trimmer_done")
        try:
            self._advance()
            return self.view()
        finally:
            self._op.release()

    def retry(self) -> m.SetupView:
        self._enter("retry")
        self.phase = m.SetupPhase.AWAIT_INPUT
        self._op.release()
        return self.view()

    def skip(self) -> m.SetupView:
        self._enter("skip")
        try:
            s = self._setup()
            if s is not None:
                self._results.append(m.SetupResult(s.id, s.kind, "skipped"))
            self._advance()
            return self.view()
        finally:
            self._op.release()

    def fit(self) -> FitResult:
        if self.state not in (m.SessionState.READY_TO_FIT, m.SessionState.FITTED):
            raise CalibrationError(f"cannot fit in state {self.state}")
        self.calls.append("fit")
        self.state = m.SessionState.FITTED
        cal = self.scope.calibration
        rf = RangeFit(1.0, 0.996, 0.989, (), 1, 1, 0, 0.4, 0.02, "fitted")
        return FitResult(cal, {1.0: rf}, ("demo warning",), previous=cal)

    def save(self, *, confirm: bool, apply_only: bool = False) -> m.SaveResult:
        if confirm is not True:
            raise UnsafeOperation("save needs confirm=True")
        if self.state != m.SessionState.FITTED:
            raise CalibrationError(f"cannot save in state {self.state}")
        self.calls.append(f"save(apply_only={apply_only})")
        self.state = m.SessionState.SAVED
        target = self.path if self.path is not None else self.scope._calibration_path
        saved = not apply_only and target is not None  # like the session: sim without a file
        path = Path(target) if saved else None
        self.saved = m.SaveResult(saved, None, self.scope.calibration.calibration_id, path)
        # move to the first verify setup
        setups = self.plan.setups
        while self.index < len(setups) and setups[self.index].kind != "verify":
            self.index += 1
        self.phase = m.SetupPhase.AWAIT_INPUT
        if self.index >= len(setups):
            self.state = m.SessionState.DONE
            _ACTIVE.pop(id(self.scope), None)
        return self.saved

    def revert(self) -> m.SaveResult:
        self.calls.append("revert")
        return m.SaveResult(False, None, "previous", None)

    def abort(self) -> bool:
        self.calls.append("abort")
        if self.state.terminal:
            return False
        self.aborted.set()
        if not self._op.locked():
            self.state = m.SessionState.ABORTED
            _ACTIVE.pop(id(self.scope), None)
        return True

    def summary(self) -> dict[str, Any]:
        return {"state": str(self.state), "log_path": "/tmp/fake-session-log.json"}


def install(monkeypatch: Any) -> type[FakeSession]:
    """Replace ``edu09.calwizard.CalibrationSession`` / ``active_session`` with the fake."""
    import edu09.calwizard as cw

    FakeSession.instances = []
    _ACTIVE.clear()
    monkeypatch.setattr(cw, "CalibrationSession", FakeSession)
    monkeypatch.setattr(cw, "active_session", lambda scope: _ACTIVE.get(id(scope)))
    return FakeSession
