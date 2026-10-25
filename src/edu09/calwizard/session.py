"""Calibration wizard session: a state machine over a ``Scope`` (calibration_wizard.md 3).

::

    PLANNED --start()--> IN_SETUP(i) --(setup ok|warning|skipped)--> IN_SETUP(i+1) ... READY_TO_FIT
    READY_TO_FIT --fit()--> FITTED --save(confirm=True)--> SAVED --(verify setups)--> DONE
    SAVED/DONE --revert()--> DONE ; any non-terminal --abort()--> ABORTED ; bug --> FAILED

A finished setup (ok/warning) advances immediately; its result is ``view().last_result``. A
failed setup stays current (phase ``FAILED``; submit again or :meth:`CalibrationSession.retry`).
:meth:`~CalibrationSession.retry` re-runs the current setup, or - when the current one has not
been started yet - the setup just completed (its points are discarded). There is no general
"back": measurements depend on the zero step taken in the same sitting.

Measurements run as one ``Scope.run_exclusive`` job per setup with the session's abort event, so
the live view pauses once per setup and :meth:`~CalibrationSession.abort` (any thread,
non-blocking) ends a running job at its next device call (``JobAborted``). Nothing is installed
on the scope before :meth:`~CalibrationSession.save`. Every terminal state writes a session log
(``<data dir>/calibration-sessions/<stamp>.json``, at most 64 KiB, newest 20 kept).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np

from edu09 import storage, units
from edu09.calwizard import model as m
from edu09.calwizard.fit import FitResult, GainPoint, fit_calibration
from edu09.calwizard.model import (
    Plan,
    PointResult,
    QualityCode,
    SaveResult,
    SessionState,
    Setup,
    SetupPhase,
    SetupResult,
    SetupView,
    TrimmerReading,
)
from edu09.calwizard.planner import accept_interval
from edu09.device import calibration as devcal
from edu09.device.scope import Scope
from edu09.device.worker import DeviceIO
from edu09.errors import (
    AcquisitionTimeout,
    CalibrationError,
    DeviceDisconnected,
    Edu09Error,
    JobAborted,
    ProtocolError,
    UnsafeOperation,
)
from edu09.model import ChannelSettings, Coupling, VoltsPerDiv
from edu09.protocol import tables

if TYPE_CHECKING:
    from edu09.calwizard.simref import SimReference

__all__ = [
    "LOG_DIRNAME",
    "LOG_KEEP",
    "LOG_MAX_BYTES",
    "CalibrationSession",
    "active_session",
]

log = logging.getLogger(__name__)

REFERENCE_FRAMES: Final = 4
GROUND_FRAMES: Final = 2
TRIMMER_FRAMES: Final = 1
NOISE_WARN_COUNTS: Final = 2.5
NOISE_FAIL_COUNTS: Final = 6.0
FRAME_SPREAD_WARN_COUNTS: Final = 0.5
NO_SIGNAL_DIV: Final = 0.3
SANITY_REL: Final = 0.15
DRIFT_REL: Final = 0.003
DRIFT_COUNTS: Final = 0.5
TYPO_PCT: Final = 4.0
"""A reference reading differing more than this (%) from the scope is flagged as a possible typo."""
VERIFY_REL: Final = 0.005
VERIFY_DIV: Final = 0.05
MAX_READING_V: Final = tables.INPUT_ABS_MAX_V
TRIMMER_SAME_PCT: Final = 0.05
"""Trimmer trend: changes of |deviation| below this (percentage points) count as "same"."""
MAX_TRIMMER_SAMPLES: Final = 2000
LOG_DIRNAME: Final = "calibration-sessions"
LOG_KEEP: Final = 20
LOG_MAX_BYTES: Final = 64 * 1024
TOOL_NAME: Final = "edu09-calibrate"
_JOB_BASE_TIMEOUT_S: Final = 60.0
_JOB_PER_POINT_S: Final = 5.0

_DEVICE_ERRORS = (AcquisitionTimeout, DeviceDisconnected, ProtocolError, TimeoutError)

_ACTIVE: weakref.WeakKeyDictionary[Scope, CalibrationSession] = weakref.WeakKeyDictionary()
_ACTIVE_LOCK = threading.Lock()


def active_session(scope: Scope) -> CalibrationSession | None:
    """The scope's active (started, not terminal) calibration session, if any."""
    with _ACTIVE_LOCK:
        return _ACTIVE.get(scope)


def _sign(x: float) -> int:
    return -1 if x < 0 else 1


class CalibrationSession:
    """One calibration wizard run on ``scope`` following ``plan`` (see the module docstring).

    ``path``: calibration file to write on :meth:`save` (default: the scope's own file; sim and
    replay scopes without ``path`` only apply the result). ``sim``: a
    :class:`~edu09.calwizard.simref.SimReference` that plays the human with bench supply and
    multimeter (tests, demos, ``--simulate``); it never changes behaviour on hardware.
    All methods are thread-safe; measuring methods are serialised by a non-reentrant op lock
    (a concurrent call raises ``CalibrationError("calibration session busy")``).
    """

    def __init__(
        self,
        scope: Scope,
        plan: Plan,
        *,
        path: str | os.PathLike[str] | None = None,
        sim: SimReference | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(plan, Plan):
            raise TypeError(f"expected Plan, got {type(plan).__name__}")
        self._scope = scope
        self.plan = plan
        self._path = Path(path) if path is not None else None
        self._sim = sim
        self._clock = clock
        self._lock = threading.RLock()  # state
        self._oplock = threading.Lock()  # measuring ops (non-reentrant, never waited on)
        self._abort = threading.Event()
        self._state = SessionState.PLANNED
        self._index = 0
        self._phase: SetupPhase | None = None
        self._results: dict[int, SetupResult] = {}
        self._last_result: SetupResult | None = None
        self._failed_attempts: list[SetupResult] = []
        self._sim_reading: float | None = None
        self._start_cal: units.Calibration | None = None
        self._start_id: str | None = None
        self._base: units.Calibration | None = None
        self._ground_map: dict[tuple[float, int], float] = {}
        self._fit: FitResult | None = None
        self._saved: SaveResult | None = None
        self._partial_save: SaveResult | None = None
        self._saved_cal: units.Calibration | None = None
        self._apply_only = False
        self._reverted: SaveResult | None = None
        self._trim_vref: float | None = None
        self._trim: list[TrimmerReading] = []
        self._trim_log: list[dict[str, Any]] = []
        self._warnings: list[str] = []
        self._t0 = clock()
        self._created = datetime.now(UTC)
        self._log_path: Path | None = None
        self._finalized = False
        self._error: str | None = None

    # -- properties ------------------------------------------------------------------------
    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def current(self) -> SetupView | None:
        """View of the current setup, None when no setup is current."""
        v = self.view()
        return v if v.setup is not None else None

    @property
    def results(self) -> tuple[SetupResult, ...]:
        """Completed setups (ok / warning / skipped / finished verify) in plan order."""
        with self._lock:
            return tuple(self._results[i] for i in sorted(self._results))

    @property
    def fit_result(self) -> FitResult | None:
        return self._fit

    @property
    def partial_save(self) -> SaveResult | None:
        """Set when the calibration file was written but installing/saving then raised."""
        return self._partial_save

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    # -- views -----------------------------------------------------------------------------
    def _current_setup(self) -> Setup | None:
        if self._state in (SessionState.IN_SETUP, SessionState.SAVED) and self._index < len(
            self.plan.setups
        ):
            return self.plan.setups[self._index]
        return None

    def _needs(self, s: Setup | None) -> m.Needs:
        if s is None or self._phase in (None, SetupPhase.DONE, SetupPhase.SKIPPED):
            return "none"
        if self._phase is SetupPhase.LIVE:
            return "done"
        if s.kind == "zero":
            return "input_disconnected"
        return "reading_v" if s.needs_reading else "none"

    def view(self) -> SetupView:
        """Cheap snapshot (any thread)."""
        with self._lock:
            s = self._current_setup()
            return SetupView(
                state=self._state,
                index=self._index,
                total=len(self.plan.setups),
                setup=s,
                phase=self._phase if s is not None else None,
                needs=self._needs(s),
                last_result=self._last_result,
                sim_reading_v=self._sim_reading if s is not None else None,
                warnings=tuple(self._warnings),
                busy=self._oplock.locked(),
                trimmer_v_ref=self._trim_vref,
            )

    # -- op guard ----------------------------------------------------------------------------
    @contextlib.contextmanager
    def _op(self, *allowed: SessionState) -> Iterator[None]:
        if not self._oplock.acquire(blocking=False):
            raise CalibrationError("calibration session busy")
        try:
            with self._lock:
                if self._state not in allowed:
                    raise CalibrationError(
                        f"not possible in state {self._state.value} "
                        f"(needs {'/'.join(s.value for s in allowed)})"
                    )
            try:
                yield
            except JobAborted:
                self._set_terminal(SessionState.ABORTED)
                raise
            except (Edu09Error, ValueError, TimeoutError):
                raise
            except Exception as exc:  # a bug or an unexpected failure: unrecoverable
                self._error = f"{type(exc).__name__}: {exc}"
                log.exception("calibration session failed")
                self._set_terminal(SessionState.FAILED)
                raise
        finally:
            self._oplock.release()
            if self._state.terminal:
                self._finalize()

    def _check_abort(self) -> None:
        if self._abort.is_set() or self._scope.closed:
            raise JobAborted("calibration session aborted")

    def _set_terminal(self, st: SessionState) -> None:
        with self._lock:
            if not self._state.terminal:
                self._state = st
                self._phase = None

    def _finalize(self) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self._scope) is self:
                del _ACTIVE[self._scope]
        try:
            self._write_log()
        except OSError as exc:
            log.warning("could not write the calibration session log: %s", exc)

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> SetupView:
        """Register as the scope's active session and enter the first setup."""
        with self._op(SessionState.PLANNED):
            with _ACTIVE_LOCK:
                other = _ACTIVE.get(self._scope)
                if other is not None and other is not self and not other.state.terminal:
                    raise CalibrationError("another calibration session is active on this scope")
                if self._scope.closed:
                    raise CalibrationError("the scope is closed")
                _ACTIVE[self._scope] = self
            cal = self._scope.calibration
            with self._lock:
                self._start_cal = cal
                self._start_id = cal.calibration_id
                self._state = SessionState.IN_SETUP
                self._enter(0)
        return self.view()

    def abort(self) -> bool:
        """Abort (thread-safe, non-blocking). A running measurement ends with ``JobAborted`` at
        its next device call; nothing is installed or saved afterwards. Returns False if the
        session was already finished."""
        with self._lock:
            if self._state.terminal:
                return False
            self._abort.set()
            self._state = SessionState.ABORTED
            self._phase = None
            busy = self._oplock.locked()
        if not busy:
            self._finalize()
        # else: the running op's guard finalizes when it returns
        return True

    # -- navigation --------------------------------------------------------------------------
    def _enter(self, i: int) -> None:
        """Make setup ``i`` current (or move to READY_TO_FIT / DONE). Caller holds _lock."""
        setups = self.plan.setups
        self._sim_reading = None
        self._trim_vref = None
        self._trim = []
        if self._state is SessionState.IN_SETUP and (
            i >= len(setups) or setups[i].kind == "verify"
        ):
            self._index = i
            self._state = SessionState.READY_TO_FIT
            self._phase = None
            return
        if i >= len(setups):
            self._index = i
            self._phase = None
            if self._state is SessionState.SAVED:
                self._state = SessionState.DONE
            return
        self._index = i
        s = setups[i]
        self._phase = SetupPhase.AWAIT_INPUT
        if s.kind == "trimmer" and s.volts is None:
            self._phase = SetupPhase.LIVE
            self._trim_vref = 0.0
        if self._sim is not None:
            if s.kind == "zero" or s.volts is None:
                self._sim.disconnect()
            else:
                self._sim_reading = self._sim.connect(s.volts)

    def _complete(self, result: SetupResult) -> None:
        """Store a finished setup and advance. Caller holds _lock."""
        self._results[self._index] = result
        self._last_result = result
        self._enter(self._index + 1)

    def _fail(self, result: SetupResult) -> None:
        self._failed_attempts.append(result)
        self._last_result = result
        self._phase = SetupPhase.FAILED

    def retry(self) -> SetupView:
        """Re-run the current setup (discarding its points), or the just-completed one if the
        current setup has not been started."""
        with (
            self._op(
                SessionState.IN_SETUP,
                SessionState.READY_TO_FIT,
                SessionState.FITTED,
                SessionState.SAVED,
            ),
            self._lock,
        ):
            cur = self._current_setup()
            if cur is not None and self._phase in (SetupPhase.FAILED, SetupPhase.LIVE):
                self._enter(self._index)  # discard the failed attempt / trimmer samples
                return self.view()
            prev = self._index - 1
            if prev < 0 or prev not in self._results:
                raise CalibrationError("nothing to retry")
            if self._state is SessionState.SAVED and self.plan.setups[prev].kind != "verify":
                raise CalibrationError("the calibration is saved; only verify can be repeated")
            del self._results[prev]
            if self.plan.setups[prev].kind == "zero":
                self._base = None
                self._ground_map = {}
            if self._state in (SessionState.READY_TO_FIT, SessionState.FITTED):
                self._state = SessionState.IN_SETUP
                self._fit = None
            self._enter(prev)
        return self.view()

    def skip(self) -> SetupView:
        """Skip the current setup (zero skipped: the scope's offsets and model ground are used)."""
        with self._op(SessionState.IN_SETUP, SessionState.SAVED), self._lock:
            s = self._require_setup()
            self._complete(SetupResult(s.id, s.kind, "skipped", messages=("skipped",)))
        return self.view()

    def _require_setup(self, *kinds: str) -> Setup:
        s = self._current_setup()
        if s is None:
            raise CalibrationError("no setup is current")
        if kinds and s.kind not in kinds:
            raise CalibrationError(f"the current setup {s.id} is a {s.kind} step")
        return s

    # -- measuring helpers ------------------------------------------------------------------
    def _job(self, fn: Callable[[DeviceIO], Any], n_points: int) -> Any:
        self._check_abort()
        return self._scope.run_exclusive(
            fn,
            timeout_s=_JOB_BASE_TIMEOUT_S + _JOB_PER_POINT_S * max(n_points, 1),
            abort_event=self._abort,
        )

    def _meas_cal(self) -> units.Calibration:
        base = self._base if self._base is not None else self._start_cal
        assert base is not None
        return base.without_gain_correction()

    @staticmethod
    def _ch(vdiv: float, position: float) -> ChannelSettings:
        return ChannelSettings(
            volts_per_div=VoltsPerDiv.parse(vdiv), coupling=Coupling.DC, position_div=position
        )

    # -- zero -------------------------------------------------------------------------------
    def confirm_zero(self, *, input_disconnected: bool) -> SetupResult:
        """Run the zero step (offsets + ground map). Needs ``input_disconnected=True``."""
        with self._op(SessionState.IN_SETUP):
            with self._lock:
                s = self._require_setup("zero")
                if self._phase not in (SetupPhase.AWAIT_INPUT, SetupPhase.FAILED):
                    raise CalibrationError(f"setup {s.id} is not waiting for input")
            if input_disconnected is not True:
                raise UnsafeOperation(
                    "the zero step needs the input disconnected; pass input_disconnected=True"
                )
            result = self._run_zero(s)
            with self._lock:
                self._check_abort()
                if result.ok:
                    self._complete(result)
                else:
                    self._fail(result)
            return result

    def _run_zero(self, s: Setup) -> SetupResult:
        assert self._start_cal is not None
        try:
            self._check_abort()
            cal0 = devcal.measure_calibration(self._scope, abort_event=self._abort)
        except CalibrationError as exc:
            return SetupResult(s.id, "zero", "failed", messages=(str(exc),))
        except _DEVICE_ERRORS as exc:
            return SetupResult(s.id, "zero", "failed", messages=(f"device error: {exc}",))
        calm = cal0.without_gain_correction()
        pts = s.points

        def job(io: DeviceIO) -> list[tuple[int, devcal.LevelReading]]:
            out = []
            for p in pts:
                ch = self._ch(p.vdiv, p.position_div)
                dac = units.offset_dac(ch, calm)
                x = devcal.capture_window(io, ch, calm, GROUND_FRAMES)
                r = devcal.LevelReading.from_samples(x, dac, p.vdiv)
                devcal._check_reading(r, quiet=True)
                out.append((dac, r))
            return out

        try:
            levels = self._job(job, len(pts)) if pts else []
        except CalibrationError as exc:
            return SetupResult(s.id, "zero", "failed", messages=(str(exc),))
        except _DEVICE_ERRORS as exc:
            return SetupResult(s.id, "zero", "failed", messages=(f"device error: {exc}",))
        gmap: dict[tuple[float, int], float] = {}
        results: list[PointResult] = []
        max_err = 0.0
        for p, (dac, r) in zip(pts, levels, strict=True):
            model_g = float(units.gnd_counts(self._ch(p.vdiv, p.position_div), calm))
            err = r.mean - model_g
            max_err = max(max_err, abs(err))
            gmap[(p.vdiv, dac)] = r.mean
            results.append(
                PointResult(
                    p.vdiv,
                    p.position_div,
                    "ground",
                    "ok",
                    QualityCode.OK,
                    message=f"ground {r.mean:.2f} (model {model_g:.2f})",
                    dac=dac,
                    raw_mean=r.mean,
                    std=r.std,
                    frame_spread=r.frame_spread,
                    clipped=r.clipped,
                    ground=r.mean,
                    ground_source="map",
                )
            )
        old = self._start_cal
        with self._lock:
            self._base = cal0
            self._ground_map = gmap
        extra = {
            "base_ofs": {"old": old.base_ofs, "new": cal0.base_ofs},
            "gnd_k": {"old": old.gnd_fit.k, "new": cal0.gnd_fit.k},
            "gnd_b": {"old": old.gnd_fit.b, "new": cal0.gnd_fit.b},
            "range_corr": {f"{k:g}": v for k, v in sorted(cal0.range_corr.items())},
            "ground_model_err_counts": round(max_err, 3),
            "ground_points": len(gmap),
        }
        msgs = [
            f"offsets measured: base 0x{cal0.base_ofs:02X} (was 0x{old.base_ofs:02X}), "
            f"ground fit k={cal0.gnd_fit.k:.4f} b={cal0.gnd_fit.b:.2f}",
        ]
        if pts:
            msgs.append(f"ground map: {len(gmap)} points, model error max {max_err:.2f} counts")
        return SetupResult(s.id, "zero", "ok", tuple(results), tuple(msgs), None, extra)

    # -- readings -----------------------------------------------------------------------------
    def submit_reading(self, volts: float) -> SetupResult:
        """Submit the multimeter reading of the current reference / verify / trimmer setup and
        measure (trimmer: starts the live phase)."""
        if isinstance(volts, bool) or not isinstance(volts, int | float):
            raise ValueError(f"reading must be a number, got {volts!r}")
        v = float(volts)
        if not math.isfinite(v) or v == 0 or abs(v) > MAX_READING_V:
            raise ValueError(f"reading must be finite, non-zero and within +-{MAX_READING_V:g} V")
        with self._op(SessionState.IN_SETUP, SessionState.SAVED):
            with self._lock:
                s = self._require_setup("reference", "verify", "trimmer")
                if not s.needs_reading:
                    raise CalibrationError(f"setup {s.id} needs no reading")
                if self._phase not in (SetupPhase.AWAIT_INPUT, SetupPhase.FAILED):
                    raise CalibrationError(f"setup {s.id} is not waiting for a reading")
            assert s.volts is not None
            sign = _sign(s.volts)
            v_ref = sign * abs(v)
            notes: list[str] = []
            if _sign(v) != sign:
                notes.append(f"assuming {v_ref:+.4g} V (polarity of this step)")
            if s.kind == "trimmer":
                with self._lock:
                    self._check_abort()
                    self._trim_vref = v_ref
                    self._trim = []
                    self._phase = SetupPhase.LIVE
                    res = SetupResult(
                        s.id,
                        "trimmer",
                        "ok",
                        messages=(*notes, "adjust the trimmer; press Done when finished"),
                        reading_v=v_ref,
                    )
                    self._last_result = res
                return res
            if s.kind == "verify":
                result = self._run_verify(s, v_ref, notes)
            else:
                result = self._flag_typo(self._run_reference(s, v_ref, notes))
            with self._lock:
                self._check_abort()
                if result.status == "failed" and not (
                    s.kind == "verify" and result.extra.get("verify") == "fail"
                ):
                    self._fail(result)
                else:
                    self._complete(result)
            return result

    def _window_drop(
        self, s: Setup, v_ref: float
    ) -> tuple[list[m.MeasurePoint], list[PointResult]]:
        keep: list[m.MeasurePoint] = []
        dropped: list[PointResult] = []
        for p in s.points:
            d = abs(v_ref) / p.vdiv
            window = m.VERIFY_WINDOW if p.purpose == "verify" else m.target(p.purpose).accepted
            iv = accept_interval(window, p.position_div, v_ref < 0)
            if iv is not None and iv[0] - 1e-9 <= d <= iv[1] + 1e-9:
                keep.append(p)
            else:
                dropped.append(
                    PointResult(
                        p.vdiv,
                        p.position_div,
                        p.purpose,
                        "failed",
                        QualityCode.OUT_OF_WINDOW,
                        message=f"{d:.2f} div is outside this point's window",
                        hint="set the source closer to the requested value",
                        v_ref=v_ref,
                        deflection_div=v_ref / p.vdiv,
                    )
                )
        return keep, dropped

    def _classify(
        self,
        p: m.MeasurePoint,
        r: devcal.LevelReading,
        ground: float,
        ground_source: Literal["map", "model"],
        v_ref: float,
        v_read: float,
        cpd: float,
    ) -> PointResult:
        vdiv = p.vdiv
        code, status, msg, hint = QualityCode.OK, "ok", "", ""
        lo, hi = tables.ADC_MIN + devcal.CLIP_MARGIN, tables.ADC_MAX - devcal.CLIP_MARGIN
        saturated = r.clipped > devcal.MAX_CLIPPED_FRACTION or not lo < r.mean < hi
        wrong_side = _sign(v_read) != _sign(v_ref) and abs(v_read) >= NO_SIGNAL_DIV * vdiv
        if saturated and wrong_side:  # reversed leads usually clip at the other end
            code, status = QualityCode.POLARITY, "failed"
            msg = f"wrong polarity (input clipped at the {'top' if v_read > 0 else 'bottom'})"
            hint = "swap the leads at the source"
        elif saturated:
            code, status = QualityCode.SATURATED, "failed"
            msg = f"input saturated (mean raw {r.mean:.1f}, {r.clipped:.1%} clipped)"
            hint = "check the voltage and the range"
        elif r.std > NOISE_FAIL_COUNTS:
            code, status = QualityCode.NOISY, "failed"
            msg = f"too noisy ({r.std:.1f} counts rms)"
            hint = "loose connection or noisy source"
        elif abs(v_read) < NO_SIGNAL_DIV * vdiv:
            code, status = QualityCode.NO_SIGNAL, "failed"
            msg = f"no signal (read {v_read:+.4f} V)"
            hint = "source not connected? is the probe on x1?"
        elif _sign(v_read) != _sign(v_ref):
            code, status = QualityCode.POLARITY, "failed"
            msg = f"wrong polarity (read {v_read:+.4f} V, expected {v_ref:+.4f} V)"
            hint = "swap the leads at the source"
        elif abs(v_read / v_ref - 1) > SANITY_REL:
            code, status = QualityCode.READING_MISMATCH, "failed"
            msg = f"scope reads {v_read:+.4f} V, typed {v_ref:+.4f} V"
            hint = "check the typed value; is the probe on x1?"
        elif r.std > NOISE_WARN_COUNTS:
            code, status = QualityCode.NOISY, "warning"
            msg = f"noisy ({r.std:.1f} counts rms)"
            hint = "check the connection"
        elif r.frame_spread > FRAME_SPREAD_WARN_COUNTS:
            code, status = QualityCode.NOISY, "warning"
            msg = f"level moved {r.frame_spread:.2f} counts between frames"
            hint = "source drifting or settling"
        return PointResult(
            vdiv,
            p.position_div,
            p.purpose,
            status,  # type: ignore[arg-type]
            code,
            msg,
            hint,
            dac=r.dac,
            raw_mean=r.mean,
            std=r.std,
            frame_spread=r.frame_spread,
            clipped=r.clipped,
            ground=ground,
            ground_source=ground_source,
            v_ref=v_ref,
            v_read=v_read,
            deflection_div=v_ref / vdiv,
            err_pct=100.0 * (v_read / v_ref - 1.0),
        )

    def _out_of_window_result(
        self, s: Setup, v_ref: float, dropped: list[PointResult], notes: list[str]
    ) -> SetupResult:
        acc = s.accept_v
        target = s.volts if s.volts is not None else v_ref
        if acc is not None:
            msg = f"set the source to {target:+.4g} V (allowed {acc[0]:+.4g}..{acc[1]:+.4g} V)"
        else:
            msg = f"set the source to {target:+.4g} V"
        return SetupResult(
            s.id, s.kind, "failed", tuple(dropped), (*notes, msg), v_ref, {"verify": None}
        )

    def _run_reference(self, s: Setup, v_ref: float, notes: list[str]) -> SetupResult:
        keep, dropped = self._window_drop(s, v_ref)
        if not keep:
            return self._out_of_window_result(s, v_ref, dropped, notes)
        calm = self._meas_cal()
        gmap = dict(self._ground_map)
        sadc = -1.0 if calm.adc_inverted else 1.0
        cpd = calm.counts_per_div

        def measure(io: DeviceIO, p: m.MeasurePoint) -> PointResult:
            ch = self._ch(p.vdiv, p.position_div)
            dac = units.offset_dac(ch, calm)
            x = devcal.capture_window(io, ch, calm, REFERENCE_FRAMES)
            r = devcal.LevelReading.from_samples(x, dac, p.vdiv)
            g = gmap.get((p.vdiv, dac))
            src: Literal["map", "model"] = "map"
            if g is None:
                g, src = float(units.gnd_counts(ch, calm)), "model"
            v_read = (r.mean - g) * sadc * p.vdiv / cpd
            return self._classify(p, r, g, src, v_ref, v_read, cpd)

        def job(io: DeviceIO) -> tuple[list[PointResult], int | None, PointResult | None]:
            out: list[PointResult] = []
            for i, p in enumerate(keep):
                pr = measure(io, p)
                out.append(pr)
                if i == 0 and pr.code is QualityCode.READING_MISMATCH:
                    out.extend(
                        PointResult(
                            q.vdiv,
                            q.position_div,
                            q.purpose,
                            "failed",
                            QualityCode.READING_MISMATCH,
                            "not measured (the first point did not match the reading)",
                            "check the typed value; is the probe on x1?",
                            v_ref=v_ref,
                            deflection_div=v_ref / q.vdiv,
                        )
                        for q in keep[1:]
                    )
                    return out, None, None
            # drift check: re-measure the first usable point
            k = next((j for j, pr in enumerate(out) if pr.usable), None)
            return out, k, (measure(io, keep[k]) if k is not None else None)

        try:
            measured, k, again = self._job(job, len(keep) + 1)
        except _DEVICE_ERRORS as exc:
            return SetupResult(
                s.id,
                s.kind,
                "failed",
                tuple(dropped),
                (*notes, f"device error: {exc}"),
                v_ref,
            )
        msgs = list(notes)
        drift = False
        if k is not None and again is not None:
            first = measured[k]
            assert first.v_read is not None and again.v_read is not None
            dv = abs(again.v_read - first.v_read)
            lim = max(DRIFT_REL * abs(first.v_read), DRIFT_COUNTS * first.vdiv / cpd)
            if dv > lim:
                drift = True
                msgs.append(
                    f"source drifting ({dv * 1e3:.1f} mV during the step): re-read the meter"
                )
                if first.code is QualityCode.OK:
                    measured[k] = dataclasses.replace(
                        first,
                        status="warning",
                        code=QualityCode.DRIFT,
                        message=f"drift {dv * 1e3:.1f} mV",
                        hint="source drifting: re-read the meter",
                    )
        points = (*measured, *dropped)
        usable = [p for p in points if p.usable]
        if not usable:
            status: m.SetupStatus = "failed"
            msgs.extend(f"{p.vdiv:g} V/div: {p.message} ({p.hint})" for p in points if p.message)
        elif all(p.status == "ok" for p in points) and not drift:
            status = "ok"
        else:
            status = "warning"
            msgs.extend(
                f"{p.vdiv:g} V/div: {p.message}" for p in points if p.status != "ok" and p.message
            )
        return SetupResult(s.id, s.kind, status, points, tuple(msgs), v_ref)

    @staticmethod
    def _flag_typo(result: SetupResult) -> SetupResult:
        """Warn when the typed reading is far from what the scope measured (pre-session
        calibration): ``extra["reading_suspect"]`` = largest |error| in %."""
        errs = [abs(p.err_pct) for p in result.points if p.usable and p.err_pct is not None]
        if not errs or max(errs) <= TYPO_PCT:
            return result
        worst = max(errs)
        msg = f"reading differs by {worst:.1f} % from the scope: did you type it correctly?"
        return dataclasses.replace(
            result,
            status="warning" if result.status == "ok" else result.status,
            messages=(*result.messages, msg),
            extra={**result.extra, "reading_suspect": round(worst, 2)},
        )

    def _run_verify(self, s: Setup, v_ref: float, notes: list[str]) -> SetupResult:
        keep, dropped = self._window_drop(s, v_ref)
        if not keep:
            return self._out_of_window_result(s, v_ref, dropped, notes)
        cal = self._scope.calibration
        cpd = cal.counts_per_div

        def job(io: DeviceIO) -> list[tuple[PointResult, bool]]:
            out = []
            for p in keep:
                ch = self._ch(p.vdiv, p.position_div)
                dac = units.offset_dac(ch, cal)
                x = devcal.capture_window(io, ch, cal, REFERENCE_FRAMES)
                r = devcal.LevelReading.from_samples(x, dac, p.vdiv)
                v_scope = float(np.mean(units.counts_to_volts(x, ch, cal)))
                g = float(units.gnd_counts(ch, cal))
                out.append(self._verify_point(p, r, g, v_ref, v_scope, cpd))
            return out

        try:
            measured = self._job(job, len(keep))
        except _DEVICE_ERRORS as exc:
            return SetupResult(
                s.id, s.kind, "failed", tuple(dropped), (*notes, f"device error: {exc}"), v_ref
            )
        prs = [pr for pr, _ in measured]
        points = (*prs, *dropped)
        valid = [pr for pr, ok in measured if ok]
        if not valid:
            msgs = (*notes, *(f"{p.vdiv:g} V/div: {p.message} ({p.hint})" for p in points))
            return SetupResult(s.id, s.kind, "failed", points, msgs, v_ref, {"verify": None})
        passed = all(p.usable for p in valid)
        verdict = "verify passed" if passed else "verify FAILED: revert or re-run the wizard"
        msgs = (*notes, verdict, *(f"{p.vdiv:g} V/div: {p.message}" for p in prs))
        return SetupResult(
            s.id,
            s.kind,
            "ok" if passed else "failed",
            points,
            msgs,
            v_ref,
            {"verify": "pass" if passed else "fail"},
        )

    def _verify_point(
        self,
        p: m.MeasurePoint,
        r: devcal.LevelReading,
        ground: float,
        v_ref: float,
        v_scope: float,
        cpd: float,
    ) -> tuple[PointResult, bool]:
        """Verify one point; returns (result, measurement valid). A valid measurement passes
        when ``|v_scope - v_ref| <= 0.5 % |v_ref| + 0.05 div``."""
        pr = self._classify(p, r, ground, "model", v_ref, v_scope, cpd)
        if pr.status == "failed" and pr.code is not QualityCode.READING_MISMATCH:
            return pr, False  # saturated / noisy / no signal / polarity: not a verdict
        tol = VERIFY_REL * abs(v_ref) + VERIFY_DIV * p.vdiv
        ok = abs(v_scope - v_ref) <= tol
        msg = (
            f"{'pass' if ok else 'FAIL'}: {v_scope:+.4f} V vs {v_ref:+.4f} V "
            f"({100 * (v_scope / v_ref - 1):+.2f} %, limit +-{tol * 1e3:.1f} mV)"
        )
        if ok:
            st: m.PointStatus = "warning" if pr.status == "warning" else "ok"
            return dataclasses.replace(pr, status=st, message=msg), True
        return dataclasses.replace(
            pr,
            status="failed",
            code=QualityCode.READING_MISMATCH,
            message=msg,
            hint="revert or re-run the wizard",
        ), True

    # -- trimmer ------------------------------------------------------------------------------
    def trimmer_sample(self) -> TrimmerReading:
        """One live trimmer reading (~0.25 s): DC mean through the session's offsets (zero step,
        else the installed calibration) without gain correction, against the typed reference."""
        with self._op(SessionState.IN_SETUP):
            with self._lock:
                s = self._require_setup("trimmer")
                if self._phase is not SetupPhase.LIVE or self._trim_vref is None:
                    raise CalibrationError("the trimmer step is not in its live phase")
                v_ref = self._trim_vref
            p = s.points[0]
            # offsets of the zero step (or the installed ones), gain 1: shows RV1's raw gain
            # error, which is what the trimmer adjusts (the gain steps follow it)
            cal = self._meas_cal()
            ch = self._ch(p.vdiv, p.position_div)

            def job(io: DeviceIO) -> tuple[float, float, float]:
                x = devcal.capture_window(io, ch, cal, TRIMMER_FRAMES)
                return (
                    float(np.mean(units.counts_to_volts(x, ch, cal))),
                    float(x.mean()),
                    float(np.std(x)),
                )

            v_meas, raw_mean, noise = self._job(job, 1)
            with self._lock:
                self._check_abort()
                dev = v_meas - v_ref
                dev_pct = 100.0 * dev / (abs(v_ref) if v_ref != 0 else p.vdiv)
                prev = self._trim[-1] if self._trim else None
                best = min([abs(dev_pct), *(abs(t.dev_pct) for t in self._trim)])
                trend: Literal["better", "worse", "same"] = "same"
                if prev is not None:
                    delta = abs(dev_pct) - abs(prev.dev_pct)
                    if delta < -TRIMMER_SAME_PCT:
                        trend = "better"
                    elif delta > TRIMMER_SAME_PCT:
                        trend = "worse"
                tr = TrimmerReading(
                    v_meas, v_ref, dev, dev_pct, raw_mean, noise, best, trend, len(self._trim) + 1
                )
                if len(self._trim) < MAX_TRIMMER_SAMPLES:
                    self._trim.append(tr)
                    self._trim_log.append({"t": round(self._clock() - self._t0, 3), **tr.to_dict()})
            return tr

    def trimmer_done(self) -> SetupView:
        """Finish the trimmer step's live phase and advance."""
        with self._op(SessionState.IN_SETUP), self._lock:
            s = self._require_setup("trimmer")
            if self._phase is not SetupPhase.LIVE:
                raise CalibrationError("the trimmer step is not in its live phase")
            last = self._trim[-1] if self._trim else None
            extra: dict[str, Any] = {"samples": len(self._trim)}
            msgs: list[str] = []
            if last is not None:
                extra.update(
                    last_dev_pct=round(last.dev_pct, 4),
                    best_abs_dev_pct=round(last.best_abs_dev_pct, 4),
                    v_meas=round(last.v_meas, 5),
                )
                msgs.append(f"trimmer done: last deviation {last.dev_pct:+.2f} %")
            self._complete(
                SetupResult(s.id, "trimmer", "ok", (), tuple(msgs), self._trim_vref, extra)
            )
        return self.view()

    # -- fit / save / revert -------------------------------------------------------------------
    def gain_points(self) -> tuple[GainPoint, ...]:
        """Usable reference points of the completed setups (the fit's input)."""
        out: list[GainPoint] = []
        for r in self.results:
            if r.kind != "reference":
                continue
            for p in r.points:
                if p.usable and p.v_ref is not None and p.v_read is not None:
                    out.append(
                        GainPoint(
                            p.vdiv,
                            p.position_div,
                            p.v_ref,
                            p.v_read,
                            p.ground_source or "model",
                            p.std or 0.0,
                            r.setup_id,
                        )
                    )
        return tuple(out)

    def _meta(self) -> dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": self.plan.options.mode,
            "sources": [s.label for s in self.plan.sources],
            "zero": self._base is not None,
        }

    def fit(self) -> FitResult:
        """Fit the gains (preview; idempotent). Nothing is installed."""
        with self._op(SessionState.READY_TO_FIT, SessionState.FITTED):
            assert self._start_cal is not None
            base = self._base if self._base is not None else self._start_cal
            res = fit_calibration(
                base,
                self.gain_points(),
                previous=self._start_cal,
                uncalibrated=self.plan.options.uncalibrated,
                meta=self._meta(),
            )
            with self._lock:
                self._check_abort()
                self._fit = res
                self._state = SessionState.FITTED
            return res

    def _install(
        self, cal: units.Calibration, apply_only: bool
    ) -> tuple[bool, Path | None, Path | None]:
        if not apply_only:
            t0 = time.time()
            try:
                backup = self._scope.set_calibration(cal, save=True, path=self._path)
            except ValueError:
                if self._path is not None:
                    raise
                apply_only = True  # sim/replay scope without a calibration file
            except BaseException:
                self._note_partial_save(cal, t0)
                raise
            else:
                target = self._path or getattr(self._scope, "_calibration_path", None)
                return True, backup, target
        self._scope.set_calibration(cal)
        return False, None, None

    def _note_partial_save(self, cal: units.Calibration, t0: float) -> None:
        """After a failure inside set_calibration(save=True): did the file get written?"""
        target = self._path or getattr(self._scope, "_calibration_path", None)
        if target is None:
            return
        try:
            target = Path(target)
            if storage.load_calibration(target).calibration_id != cal.calibration_id:
                return
            baks = [
                f
                for f in target.parent.glob(f"{target.stem}.*.bak{target.suffix}")
                if f.stat().st_mtime >= t0 - 1.0
            ]
            backup = max(baks, key=lambda f: f.stat().st_mtime) if baks else None
        except Exception:  # noqa: BLE001 - best effort diagnostics only
            return
        self._partial_save = SaveResult(True, backup, cal.calibration_id, target)

    def save(self, *, confirm: bool, apply_only: bool = False) -> SaveResult:
        """Install the fitted calibration and (unless ``apply_only``) save it with a backup.
        Sim/replay scopes without ``path`` only apply it. Then verify setups follow."""
        with self._op(SessionState.FITTED):
            if confirm is not True:
                raise UnsafeOperation("saving replaces the calibration in use; pass confirm=True")
            assert self._fit is not None
            if self._scope.calibration.calibration_id != self._start_id:
                raise CalibrationError(
                    "calibration changed during the session (another client calibrated); restart"
                )
            self._check_abort()
            cal = self._fit.calibration
            saved, backup, target = self._install(cal, apply_only)
            res = SaveResult(saved, backup, cal.calibration_id, target)
            with self._lock:
                self._saved = res
                self._saved_cal = cal
                self._apply_only = not saved
                if not self._state.terminal:  # an abort() during the write wins (stays ABORTED)
                    self._state = SessionState.SAVED
                    self._enter(self._index)
            return res

    def revert(self) -> SaveResult:
        """Re-install (and re-save, backing up the new file) the calibration that was in use
        before the session."""
        if not self._oplock.acquire(blocking=False):
            raise CalibrationError("calibration session busy")
        try:
            with self._lock:
                if self._saved is None or self._state not in (
                    SessionState.SAVED,
                    SessionState.DONE,
                ):
                    raise CalibrationError("nothing to revert (not saved)")
                if self._reverted is not None:
                    raise CalibrationError("already reverted")
            assert self._start_cal is not None and self._saved_cal is not None
            if self._scope.calibration.calibration_id != self._saved_cal.calibration_id:
                raise CalibrationError(
                    "the calibration was changed after this session saved it; not reverting"
                )
            saved, backup, target = self._install(self._start_cal, self._apply_only)
            res = SaveResult(saved, backup, self._start_cal.calibration_id, target)
            with self._lock:
                self._reverted = res
                was_done = self._state is SessionState.DONE
                self._state = SessionState.DONE
                self._phase = None
        finally:
            self._oplock.release()
        if was_done:
            with contextlib.suppress(OSError):
                self._write_log()
        else:
            self._finalize()
        return res

    # -- summary / log --------------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Small JSON summary (MCP / log)."""
        with self._lock:
            verify = [r.extra.get("verify") for r in self._results.values() if r.kind == "verify"]
            return {
                "state": self._state.value,
                "index": self._index,
                "total": len(self.plan.setups),
                "setups": [
                    {
                        "id": r.setup_id,
                        "kind": r.kind,
                        "status": r.status,
                        "points": len(r.points),
                        "usable": sum(1 for p in r.points if p.usable),
                    }
                    for r in (self._results[i] for i in sorted(self._results))
                ],
                "fit": None
                if self._fit is None
                else {
                    "calibration_id": self._fit.calibration.calibration_id,
                    "warnings": list(self._fit.warnings),
                },
                "saved": None if self._saved is None else self._saved.to_dict(),
                "reverted": None if self._reverted is None else self._reverted.to_dict(),
                "verify": None
                if not verify
                else ("pass" if all(v == "pass" for v in verify) else "fail"),
                "log_path": None if self._log_path is None else str(self._log_path),
                "error": self._error,
                "elapsed_s": round(self._clock() - self._t0, 1),
            }

    def _log_doc(self, level: int) -> dict[str, Any]:
        compact = level >= 2
        doc: dict[str, Any] = {
            "version": 1,
            "tool": TOOL_NAME,
            "created": self._created.isoformat(timespec="seconds"),
            "state": self._state.value,
            "error": self._error,
            "transport": self._scope.transport.info.kind,
            "start_calibration_id": self._start_id,
            "plan": self.plan.to_dict() if level < 3 else None,
            "results": [r.to_dict(compact=compact) for r in self.results],
            "failed_attempts": [r.to_dict(compact=True) for r in self._failed_attempts[-20:]]
            if level < 3
            else [],
            "fit": None if self._fit is None else self._fit.to_dict(),
            "saved": None if self._saved is None else self._saved.to_dict(),
            "reverted": None if self._reverted is None else self._reverted.to_dict(),
            "trimmer_samples": self._trim_log if level == 0 else self._trim_log[-50:],
        }
        return doc

    def _write_log(self) -> None:
        d = storage.default_data_dir() / LOG_DIRNAME
        if self._log_path is None:
            stamp = self._created.astimezone().strftime("%Y%m%dT%H%M%S%f")
            self._log_path = d / f"{stamp}.json"
        text = ""
        for level in range(4):
            text = json.dumps(self._log_doc(level), indent=1, default=str)
            if len(text.encode()) <= LOG_MAX_BYTES:
                break
        else:
            doc = self._log_doc(3)
            doc["results"] = doc["results"][-10:]
            text = json.dumps(doc, default=str)[:LOG_MAX_BYTES]
        storage._atomic_write_text(self._log_path, text + "\n")
        _prune_logs(d, LOG_KEEP)


def _prune_logs(d: Path, keep: int) -> None:
    files = sorted(p for p in d.glob("*.json") if p.is_file())
    for p in files[:-keep] if len(files) > keep else []:
        with contextlib.suppress(OSError):
            p.unlink()
