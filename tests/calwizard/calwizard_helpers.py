"""Helpers for the calibration wizard tests (simulated UNIT1 scope with ADC noise, a driver)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from edu09.calwizard.model import SessionState
from edu09.calwizard.session import CalibrationSession
from edu09.device.scope import Scope
from edu09.transport.sim import PHYSICAL_UNITS, SimTransport, SimulatedDevice, VirtualClock

ADC_NOISE_COUNTS = 0.7
"""HW: ~0.7 counts rms at every range. Without it a DC level is quantised without dither and gains
are only accurate to ~0.5 count (``SimulatedDevice(adc_noise_counts=...)``)."""


def make_scope(unit: str = "unit1", *, seed: int = 1, dither: bool = True) -> Scope:
    clock = VirtualClock()
    dev = SimulatedDevice(
        "dc:0v",
        clock=clock,
        calibration=PHYSICAL_UNITS[unit],
        seed=seed,
        adc_noise_counts=ADC_NOISE_COUNTS if dither else 0.0,
    )
    return Scope.open(SimTransport(device=dev, sleep=clock.sleep), reconnect_interval_s=0.01)


def drive(
    session: CalibrationSession,
    *,
    reading: Callable[[Any], float] | None = None,
    save: bool = True,
    stop: Callable[[CalibrationSession], bool] | None = None,
    max_steps: int = 200,
) -> CalibrationSession:
    """Run a session like a user who types the simulated meter readings (``reading(view)`` may
    override them). Failed setups are skipped. Stops at DONE / terminal / ``stop(session)``."""
    if session.state is SessionState.PLANNED:
        session.start()
    for _ in range(max_steps):
        if stop is not None and stop(session):
            return session
        v = session.view()
        if v.state.terminal:
            return session
        if v.state is SessionState.READY_TO_FIT:
            session.fit()
            if not save:
                return session
            session.save(confirm=True)
            continue
        if v.state is SessionState.FITTED:
            return session
        if v.needs == "input_disconnected":
            r = session.confirm_zero(input_disconnected=True)
        elif v.needs == "reading_v":
            val = reading(v) if reading is not None else v.sim_reading_v
            assert val is not None
            r = session.submit_reading(val)
        elif v.needs == "done":
            session.trimmer_sample()
            session.trimmer_done()
            continue
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected view {v}")
        if (
            r.status == "failed"
            and session.view().phase is not None
            and (
                session.view().setup is not None and session.view().setup.id == r.setup_id  # type: ignore[union-attr]
            )
        ):
            session.skip()
    raise AssertionError("session did not finish")
