"""MCP tools for the calibration wizard (calibration_wizard.md 4.3).

Registered by :func:`edu09.mcp_server.tools.register_tools` only when the server runs with
``--allow-calibration``. At most one wizard session per server; it lives in memory, is aborted
after :data:`IDLE_TIMEOUT_S` without a tool call (checked on every call) and on server shutdown
(:func:`shutdown_calibration`). Payloads are small (no raw samples, 4 significant digits, at most
:data:`MAX_RESPONSE_BYTES`).

Readings must come from the human's multimeter: the tools never fill them in (except the
simulator's meter value ``sim_reading_v`` on a simulated scope, which is labelled as such).

This module deliberately has no ``from __future__ import annotations``: the SDK builds the tool
input schemas from the runtime annotations.
"""

import importlib
import itertools
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from mcp.types import CallToolResult
from pydantic import Field

from edu09.errors import CalibrationError, UnsafeOperation

if TYPE_CHECKING:
    from edu09.calwizard import CalibrationSession, Plan, Source
    from edu09.device.scope import Scope
    from edu09.mcp_server.server import FastMCP, ServerOptions

__all__ = [
    "CAL_TOOL_NAMES",
    "HUMAN_READINGS",
    "IDLE_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "CalibrationTools",
    "register_calibration_tools",
    "shutdown_calibration",
]

log = logging.getLogger(__name__)

CALWIZARD_MODULE: Final = "edu09.calwizard"
SIMREF_MODULE: Final = "edu09.calwizard.simref"
IDLE_TIMEOUT_S: Final = 30 * 60.0
MAX_RESPONSE_BYTES: Final = 8 * 1024
MAX_SOURCES: Final = 8
MAX_READING_V: Final = 30.0
INSTRUCTION_CAP: Final = 240
"""Plan payloads shorten setup instructions to this many characters when they would exceed
:data:`MAX_RESPONSE_BYTES` (``calibration_step`` always returns the full text)."""
CAL_TOOL_NAMES: Final = (
    "calibration_plan",
    "calibration_start",
    "calibration_step",
    "calibration_submit",
    "calibration_finish",
    "calibration_abort",
)
HUMAN_READINGS: Final = (
    "Readings must be typed by the human from their multimeter; never invent or estimate them. "
    "Ask the human to perform every physical step."
)
_ATTR: Final = "_edu09_calibration_tools"


class NoSession(CalibrationError):
    """No calibration session is active (start one with calibration_start)."""


def _cw() -> Any:
    return importlib.import_module(CALWIZARD_MODULE)


def _sim_reference(scope: "Scope") -> Any:
    """``SimReference.for_scope(scope)`` on a simulated scope, else None."""
    if scope.status.transport_kind != "sim":
        return None
    try:
        mod: Any = importlib.import_module(SIMREF_MODULE)
    except ImportError:
        log.warning("simulator reference unavailable; readings must be typed")
        return None
    return mod.SimReference.for_scope(scope)


# --------------------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------------------


def _parse_sources(sources: Any) -> "tuple[Source, ...]":
    cw = _cw()
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        raise ValueError("sources must be a list of {label, volts | min_v+max_v, ...} objects")
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} sources, got {len(sources)}")
    out = []
    for i, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise ValueError(f"sources[{i}] must be an object")
        d = dict(item)
        d.setdefault("label", f"source {i + 1}")
        if "type" not in d:
            d["type"] = "fixed" if "volts" in d else "adjustable"
        if d["type"] == "adjustable":
            d.pop("tolerance_pct", None)  # the default the schema advertises; meaningless here
            d.setdefault("min_v", 0.0)
        try:
            out.append(cw.model.source_from_dict(d))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"sources[{i}]: {exc}") from None
    return tuple(cw.model.validate_sources(out))


def _plan(
    sources: Any, mode: str, max_abs_v: float, trimmer: bool, uncalibrated: str = "keep"
) -> "Plan":
    cw = _cw()
    if not isinstance(trimmer, bool):
        raise ValueError("trimmer must be true or false")
    if isinstance(max_abs_v, bool) or not isinstance(max_abs_v, int | float):
        raise ValueError("max_abs_v must be a number")
    options = cw.PlanOptions(
        mode=mode,
        max_abs_v=float(max_abs_v),
        trimmer=cw.TrimmerConfig(enabled=trimmer),
        uncalibrated=uncalibrated,
    )
    plan: Plan = cw.plan_calibration(_parse_sources(sources), options)
    return plan


def _check_reading(reading_v: Any) -> float:
    if isinstance(reading_v, bool) or not isinstance(reading_v, int | float):
        raise ValueError("reading_v must be the multimeter reading in volts (a number)")
    v = float(reading_v)
    if not math.isfinite(v) or abs(v) > MAX_READING_V:
        raise ValueError(f"reading_v must be finite and within +-{MAX_READING_V:g} V, got {v!r}")
    return v


# --------------------------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------------------------


def _size(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str))


def _shorten(text: Any, n: int = INSTRUCTION_CAP) -> Any:
    if isinstance(text, str) and len(text) > n:
        return text[: n - 1].rstrip() + "…"
    return text


def _plan_payload(plan: "Plan") -> dict[str, Any]:
    setups = [
        {
            "id": s.id,
            "kind": s.kind,
            "instruction": s.instruction,
            "source": s.source_label,
            "volts": s.volts,
            "adjustable": s.adjustable,
            "accept_v": list(s.accept_v) if s.accept_v is not None else None,
            "points": len(s.points),
            "ranges": sorted({p.vdiv for p in s.points}, reverse=True),
        }
        for s in plan.setups
    ]
    payload: dict[str, Any] = {
        "setups": setups,
        "coverage": [c.to_dict() for c in plan.coverage],
        "reconnections": plan.reconnections,
        "user_actions": plan.user_actions,
        "estimated_s": plan.estimated_s,
        "warnings": list(plan.warnings),
    }
    if _size(payload) > MAX_RESPONSE_BYTES:
        for s in setups:
            s["instruction"] = _shorten(s["instruction"])
        payload["note"] = "instructions shortened; calibration_step shows the full text"
    if _size(payload) > MAX_RESPONSE_BYTES:
        for s in setups:
            s.pop("instruction", None)
            s.pop("ranges", None)
            s.pop("accept_v", None)
        payload["note"] = "instructions omitted; calibration_step shows them"
    return payload


def _result_payload(res: Any) -> dict[str, Any]:
    return {
        "setup_id": res.setup_id,
        "kind": res.kind,
        "status": res.status,
        "points": [
            {
                "vdiv": p.vdiv,
                "position_div": p.position_div,
                "deflection_div": p.deflection_div,
                "err_pct": p.err_pct,
                "status": p.status,
                "code": str(p.code),
                "message": p.message,
            }
            for p in res.points
        ],
        "messages": list(res.messages),
        "reading_v": res.reading_v,
    }


def _capped(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink a step/result payload below :data:`MAX_RESPONSE_BYTES` (defensive)."""
    if _size(payload) <= MAX_RESPONSE_BYTES:
        return payload
    step = payload.get("step")
    targets = [payload] + ([step] if isinstance(step, dict) else [])
    for t in targets:
        setup = t.get("setup")
        if isinstance(setup, dict):
            setup["instruction"] = _shorten(setup.get("instruction"), 600)
    if _size(payload) <= MAX_RESPONSE_BYTES:
        return payload
    for t in targets:
        for key in ("last_result", "result"):
            r = t.get(key)
            if isinstance(r, dict):
                pts = r.get("points") or []
                r["points"] = [
                    {k: p.get(k) for k in ("vdiv", "status", "err_pct")} for p in pts[:12]
                ]
                r["messages"] = [_shorten(m, 160) for m in (r.get("messages") or [])[:6]]
    return payload


def _cap_warnings(items: Any, keep: int) -> Any:
    if not isinstance(items, list) or len(items) <= keep:
        return items
    return [*items[:keep], f"... and {len(items) - keep} more (see the session log)"]


def _cap_finish(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink a ``calibration_finish`` payload below :data:`MAX_RESPONSE_BYTES`: the per-range
    warnings repeat the preview's top-level ones, then the warning lists are shortened."""
    if _size(payload) <= MAX_RESPONSE_BYTES:
        return payload
    preview = payload.get("preview")
    if isinstance(preview, dict):
        for rf in (preview.get("ranges") or {}).values():
            if isinstance(rf, dict) and rf.get("warnings"):
                rf["warnings"] = [f"{len(rf['warnings'])} warning(s), see 'warnings'"]
    if "warnings" in payload and isinstance(preview, dict):
        # the save warnings repeat the fit warnings: keep only the save-specific ones
        fit_w = set(preview.get("warnings") or [])
        payload["warnings"] = [w for w in payload["warnings"] if w not in fit_w]
    for keep in (12, 6, 2, 0):
        if _size(payload) <= MAX_RESPONSE_BYTES:
            break
        if isinstance(preview, dict):
            preview["warnings"] = _cap_warnings(preview.get("warnings"), keep)
        payload["warnings"] = _cap_warnings(payload.get("warnings"), keep)
    return payload


# --------------------------------------------------------------------------------------------
# session manager
# --------------------------------------------------------------------------------------------


class CalibrationTools:
    """The (at most one) wizard session of one MCP server."""

    def __init__(
        self,
        scope: "Scope",
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ) -> None:
        self.scope = scope
        self._clock = clock
        self.idle_timeout_s = idle_timeout_s
        self._lock = threading.Lock()
        self._session: CalibrationSession | None = None
        self._session_id: str | None = None
        self._last_used = clock()
        self._ids = itertools.count(1)
        self._closed = False
        self.idle_aborted: str | None = None  # session id of the last idle-aborted session

    @property
    def session(self) -> "CalibrationSession | None":
        return self._session

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _check_idle(self) -> None:
        with self._lock:
            sess, sid = self._session, self._session_id
            idle = self._clock() - self._last_used
            if sess is None or sess.state.terminal or idle <= self.idle_timeout_s:
                self._last_used = self._clock()
                return
            self._session = None
            self._session_id = None
            self.idle_aborted = sid
            self._last_used = self._clock()
        log.warning("calibration session %s aborted after %.0f s idle", sid, idle)
        try:
            sess.abort()
        except Exception:
            log.exception("aborting the idle calibration session failed")

    def touch(self) -> None:
        """Every tool call: abort an idle session, then mark activity."""
        self._check_idle()

    def require(self) -> "CalibrationSession":
        with self._lock:
            sess = self._session
            idle_sid = self.idle_aborted
        if sess is None:
            extra = (
                f" (session {idle_sid} was aborted after {self.idle_timeout_s / 60:g} min idle)"
                if idle_sid
                else ""
            )
            raise NoSession(f"no calibration session; start one with calibration_start{extra}")
        return sess

    def start(self, plan: "Plan") -> tuple[str, "CalibrationSession"]:
        cw = _cw()
        with self._lock:
            if self._closed:
                raise CalibrationError("the MCP server is shutting down")
            cur = self._session
            if cur is not None and not cur.state.terminal:
                raise CalibrationError(
                    f"calibration session {self._session_id} is active; finish it or call "
                    "calibration_abort first"
                )
            other = cw.active_session(self.scope)
            if other is not None and not other.state.terminal:
                raise CalibrationError(
                    "another calibration session (e.g. the GUI wizard) is active on this scope"
                )
            sess = cw.CalibrationSession(self.scope, plan, sim=_sim_reference(self.scope))
            sess.start()
            sid = f"cal{next(self._ids)}"
            self._session, self._session_id = sess, sid
            self.idle_aborted = None
            self._last_used = self._clock()
        return sid, sess

    def abort(self) -> tuple[bool, Any]:
        with self._lock:
            sess = self._session
        if sess is None:
            return False, None
        aborted = False
        if not sess.state.terminal:
            aborted = bool(sess.abort())
            if not aborted:
                aborted = sess.state.terminal
        log_path = _log_path(sess)
        return aborted, log_path

    def shutdown(self) -> None:
        """Server shutdown: abort a running session (thread-safe, non-blocking). Idempotent."""
        with self._lock:
            self._closed = True
            sess, self._session = self._session, None
        if sess is not None and not sess.state.terminal:
            try:
                sess.abort()
            except Exception:
                log.exception("aborting the calibration session on shutdown failed")


def _log_path(sess: Any) -> Any:
    try:
        return sess.summary().get("log_path")
    except Exception:
        return None


def _step(sess: "CalibrationSession", live: Any = None) -> dict[str, Any]:
    d: dict[str, Any] = sess.view().to_dict()
    if live is not None:
        d["live"] = live.to_dict()
    return d


def shutdown_calibration(mcp: Any) -> None:
    """Abort the calibration session of ``mcp`` (built by ``build_server``), if any."""
    tools = getattr(mcp, _ATTR, None)
    if tools is not None:
        tools.shutdown()


def calibration_tools_of(mcp: Any) -> "CalibrationTools | None":
    """The session manager registered on ``mcp`` (tests, shutdown)."""
    return getattr(mcp, _ATTR, None)


# --------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------

SourcesArg = Annotated[
    list[dict[str, Any]] | None,
    Field(
        description="Reference sources the human has (at most 8): fixed {label, volts, "
        "tolerance_pct=5, reversible=false} or adjustable {label, min_v, max_v, "
        "reversible=false}. Volts are magnitudes. reversible=true only for floating sources "
        "(batteries, isolated supplies)."
    ),
]
ModeArg = Annotated[
    Literal["quick", "standard", "full"],
    Field(description="quick (gains only), standard (+ negative nonlinearity) or full"),
]
MaxVArg = Annotated[float, Field(description="highest |voltage| to use, 0 < x <= 30 V")]
TrimmerArg = Annotated[
    bool,
    Field(description="add the RV1 trimmer (input amplifier gain) adjustment step"),
]


def register_calibration_tools(
    mcp: "FastMCP",
    scope: "Scope",
    opts: "ServerOptions",
    *,
    ok: Callable[[Any], CallToolResult],
    tool_errors: Callable[[], Any],
    clock: Callable[[], float] = time.monotonic,
) -> CalibrationTools:
    """Register the ``calibration_*`` tools; returns the session manager (also stored on
    ``mcp`` for :func:`shutdown_calibration`). ``ok`` / ``tool_errors`` are the shared payload
    and error helpers of :mod:`.tools`."""
    tools = CalibrationTools(scope, clock=clock)
    setattr(mcp, _ATTR, tools)

    def _ok(payload: dict[str, Any]) -> CallToolResult:
        return ok(_capped(payload))

    @mcp.tool(
        description="Plan a calibration (pure, no device access): which reference voltages to "
        "connect, at which V/div, coverage per range and hints for missing ones. " + HUMAN_READINGS
    )
    def calibration_plan(
        sources: SourcesArg = None,
        mode: ModeArg = "standard",
        max_abs_v: MaxVArg = 15.0,
        trimmer: TrimmerArg = False,
    ) -> CallToolResult:
        with tool_errors():
            tools.touch()
            return _ok(_plan_payload(_plan(sources, mode, max_abs_v, trimmer)))

    @mcp.tool(
        description="Start the calibration wizard (one session at a time; the GUI's live view "
        "pauses during measurements). Returns session_id and the first step. Then loop: "
        "calibration_step -> tell the human the instruction -> calibration_submit. "
        + HUMAN_READINGS
    )
    def calibration_start(
        sources: SourcesArg = None,
        mode: ModeArg = "standard",
        max_abs_v: MaxVArg = 15.0,
        trimmer: TrimmerArg = False,
        uncalibrated: Annotated[
            Literal["keep", "typical", "unity"],
            Field(description="ranges without points: keep previous, typical values, or 1.0"),
        ] = "keep",
    ) -> CallToolResult:
        with tool_errors():
            tools.touch()
            plan = _plan(sources, mode, max_abs_v, trimmer, uncalibrated)
            sid, sess = tools.start(plan)
            return _ok(
                {
                    "session_id": sid,
                    "plan": {
                        "setups": len(plan.setups),
                        "user_actions": plan.user_actions,
                        "estimated_s": plan.estimated_s,
                        "warnings": list(plan.warnings),
                    },
                    "step": _step(sess),
                }
            )

    @mcp.tool(
        description="Current wizard step: state, index/total, setup {instruction, volts, "
        "needs: input_disconnected | reading_v | done | none}, last_result. In the trimmer's "
        "live phase every call takes one live sample ('live': measured vs reference, deviation, "
        "best so far, trend) while the human turns RV1. sim_reading_v only exists on a "
        "simulated scope. " + HUMAN_READINGS
    )
    def calibration_step() -> CallToolResult:
        with tool_errors():
            tools.touch()
            sess = tools.require()
            v = sess.view()
            live = None
            if v.setup is not None and v.setup.kind == "trimmer" and str(v.phase) == "live":
                live = sess.trimmer_sample()
            return _ok({"session_id": tools.session_id, "step": _step(sess, live)})

    @mcp.tool(
        description="Act on the current step. action='measure': zero step needs "
        "input_disconnected=true (the human confirmed the probe is disconnected); reference / "
        "verify steps need reading_v = the multimeter reading the human typed (volts, signed). "
        "'retry' re-runs the step, 'skip' skips it, 'done' ends the trimmer adjustment. "
        + HUMAN_READINGS
    )
    def calibration_submit(
        action: Annotated[
            Literal["measure", "retry", "skip", "done"],
            Field(description="measure | retry | skip | done (trimmer)"),
        ],
        reading_v: Annotated[
            float | None,
            Field(description="multimeter reading in volts, typed by the human (|v| <= 30)"),
        ] = None,
        input_disconnected: Annotated[
            bool, Field(description="zero step: the human confirmed CH1 is disconnected")
        ] = False,
    ) -> CallToolResult:
        with tool_errors():
            tools.touch()
            sess = tools.require()
            result = None
            if action == "measure":
                v = sess.view()
                if v.needs == "input_disconnected":
                    if input_disconnected is not True:
                        raise UnsafeOperation(
                            "the zero step needs the input disconnected: ask the human to "
                            "disconnect the probe, then pass input_disconnected=true"
                        )
                    result = sess.confirm_zero(input_disconnected=True)
                elif v.needs == "reading_v":
                    if reading_v is None:
                        raise ValueError(
                            "this step needs reading_v: ask the human for the multimeter reading"
                        )
                    result = sess.submit_reading(_check_reading(reading_v))
                else:
                    raise CalibrationError(
                        f"nothing to measure now (needs={v.needs}, state={v.state})"
                    )
            elif action == "retry":
                sess.retry()
            elif action == "skip":
                sess.skip()
            elif action == "done":
                sess.trimmer_done()
            else:
                raise ValueError("action must be measure, retry, skip or done")
            payload: dict[str, Any] = {"session_id": tools.session_id}
            if result is not None:
                payload["result"] = _result_payload(result)
            payload["step"] = _step(sess)
            return _ok(payload)

    @mcp.tool(
        description="After the last measurement: save=false returns the fit preview (per-range "
        "gains, before/after error). save=true with confirm=true stores it (the previous "
        "calibration file is backed up; simulated scopes only apply it) and moves on to the "
        "verify steps. Ask the human before saving."
    )
    def calibration_finish(
        save: Annotated[bool, Field(description="store the fitted calibration")] = False,
        confirm: Annotated[
            bool, Field(description="required with save=true (the human agreed to save)")
        ] = False,
    ) -> CallToolResult:
        with tool_errors():
            tools.touch()
            sess = tools.require()
            if save and confirm is not True:
                raise UnsafeOperation(
                    "saving overwrites the stored calibration (a backup is made); ask the human, "
                    "then pass save=true and confirm=true"
                )
            fit = sess.fit()
            preview = fit.to_dict()
            payload: dict[str, Any] = {"session_id": tools.session_id, "preview": preview}
            if save:
                # the session only applies it on a sim/replay scope without a calibration file
                res = sess.save(confirm=True)
                warnings = [str(w) for w in getattr(fit, "warnings", ())]
                if not res.saved:
                    warnings.append("not saved: simulated/replay scope has no calibration file")
                payload.update(
                    saved=res.saved,
                    backup_path=None if res.backup_path is None else str(res.backup_path),
                    calibration_id=res.calibration_id,
                    warnings=warnings,
                )
            payload["step"] = _step(sess)
            return _ok(_cap_finish(payload))

    @mcp.tool(
        description="Abort the calibration session: nothing more is measured or saved (a "
        "calibration already saved stays). Returns the session log path."
    )
    def calibration_abort() -> CallToolResult:
        with tool_errors():
            tools.touch()
            aborted, log_path = tools.abort()
            return _ok(
                {
                    "session_id": tools.session_id,
                    "aborted": aborted,
                    "log_path": None if log_path is None else str(log_path),
                }
            )

    return tools
