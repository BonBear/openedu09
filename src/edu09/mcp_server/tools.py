"""MCP tools and resources for one :class:`~edu09.device.scope.Scope` (architecture.md 6).

``register_tools(mcp, scope, opts)`` is shared by stdio, HTTP and the GUI's in-process server.

Payload rules: every float is rounded to 4 significant digits (NaN/inf -> null); point arrays
are capped (``max_points <= 2048``) so a tool response stays well under 32 KB; stats and
measurements always use the full 1024-sample frame. Errors are raised as tool errors whose
message is ``"<ErrorClass>: <hint>"``. Settings are reported in the flat form whose keys are
the ``configure`` parameters.

Decimated point arrays (``acquire.points`` and ``get_waveform``) are described by:

* ``t0_s``: time (s, relative to the trigger) of sample ``start`` (the first sample of the range).
* ``sample_dt_s``: time between two ADC samples.
* ``dt_s``: spacing between consecutive returned points, present only when they are evenly
  spaced (no decimation, or ``stride``); then ``t[k] = t0_s + k * dt_s``.
* ``i``: present only for ``minmax`` decimation (points are not evenly spaced): sample offsets
  from ``start``; then ``t[k] = t0_s + i[k] * sample_dt_s``.

This module deliberately has no ``from __future__ import annotations``: the SDK builds the tool
input schemas from the runtime annotations.
"""

import contextlib
import json
import logging
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import numpy as np
import numpy.typing as npt
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from edu09 import analysis, storage, units
from edu09.errors import (
    AcquisitionTimeout,
    Cancelled,
    DeviceAccessError,
    DeviceBusy,
    DeviceDisconnected,
    DeviceNotFound,
    UnsafeOperation,
)
from edu09.model import (
    POSITION_DIV_RANGE,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    VoltsPerDiv,
    Waveform,
)

if TYPE_CHECKING:
    from edu09.device.scope import Scope
    from edu09.mcp_server.server import FastMCP, ServerOptions

__all__ = [
    "CSV_COLUMNS",
    "ToolFailure",
    "LIMITS",
    "TOOL_NAMES",
    "check_export_filename",
    "error_message",
    "register_tools",
    "round_sig",
]

log = logging.getLogger(__name__)

SIG_DIGITS: Final = 4
SAMPLES_PER_FRAME: Final = 1024
MAX_POINTS: Final = 2048
MIN_POINTS: Final = 16
ACQUIRE_TIMEOUT_RANGE: Final = (0.1, 30.0)
AUTOSET_TIMEOUT_RANGE: Final = (0.5, 60.0)
LIMITS: Final[dict[str, Any]] = {
    "max_points": MAX_POINTS,
    "min_points": MIN_POINTS,
    "acquire_timeout_s": list(ACQUIRE_TIMEOUT_RANGE),
    "auto_set_timeout_s": list(AUTOSET_TIMEOUT_RANGE),
    "response_target_bytes": 32 * 1024,
    "stored_waveforms": storage.DEFAULT_STORE_CAPACITY,
}
CSV_COLUMNS: Final = list(storage.CSV_COLUMNS)
#: Tools in registration order (``calibrate`` and the ``calibration_*`` wizard tools of
#: ``cal_tools.CAL_TOOL_NAMES`` only with ``allow_calibration``).
TOOL_NAMES: Final = (
    "get_status",
    "get_capabilities",
    "configure",
    "acquire",
    "get_waveform",
    "measure",
    "spectrum",
    "export_csv",
    "set_running",
    "list_waveforms",
    "auto_set",
    "calibrate",
)
CLIPPED_WARNING: Final = "clipped: increase volts_per_div"
SPECTRUM_MAX_POINTS: Final = SAMPLES_PER_FRAME + 1
SPECTRUM_MAX_PEAKS: Final = 32
SPECTRUM_DB_FLOOR_RANGE: Final = (-200.0, 0.0)
DESPIKE_IGNORED_WARNING: Final = (
    "despike ignored: only for ETS timebases (50/20/10 us/div); it corrupts realtime frames"
)

# --------------------------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------------------------


def round_sig(x: float | None, digits: int = SIG_DIGITS) -> float | None:
    """Round to ``digits`` significant digits; None/NaN/inf -> None."""
    if x is None:
        return None
    f = float(x)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return 0.0
    return float(f"{f:.{digits}g}")


def _rounded(obj: Any) -> Any:
    """Recursively round floats (bools/ints/strings untouched; numpy scalars converted)."""
    if isinstance(obj, bool | int | str) or obj is None:
        return obj
    if isinstance(obj, float | np.floating):
        return round_sig(float(obj))
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, dict):
        return {str(k): _rounded(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_rounded(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _rounded(obj.tolist())
    return obj


def _round_list(a: Any) -> list[float | None]:
    return [round_sig(v) for v in np.asarray(a, dtype=np.float64).tolist()]


def _settings_payload(s: Settings) -> dict[str, Any]:
    d = s.to_flat_dict()
    d["timebase_label"] = s.timebase.label
    return d


def _iso(wf: Waveform) -> str:
    return wf.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _waveform_warnings(wf: Waveform) -> list[str]:
    out: list[str] = []
    if wf.clipped:
        out.append(CLIPPED_WARNING)
    if not wf.triggered and wf.settings.trigger.mode is not TriggerMode.NONE:
        out.append(
            "not triggered: auto mode fell back to free run; check trigger_level_v "
            "(e.g. the signal mean) and trigger_edge"
        )
    return out


def _points(
    wf: Waveform,
    start: int,
    stop: int,
    max_points: int,
    method: Literal["minmax", "stride"],
    include_raw: bool = False,
) -> dict[str, Any]:
    """Decimated slice ``[start, stop)`` (see the module docstring for the time convention)."""
    volts = wf.volts[start:stop]
    dec = analysis.decimate(volts, max_points, method)
    t = wf.t()
    out: dict[str, Any] = {
        "start": start,
        "stop": stop,
        "t0_s": round_sig(float(t[start])) if stop > start else None,
        "sample_dt_s": round_sig(wf.dt),
        "decimation": method if dec.factor > 1 else "none",
        "factor": dec.factor,
        "n": int(dec.values.size),
    }
    if dec.factor == 1 or method == "stride":
        out["dt_s"] = round_sig(wf.dt * dec.factor)
    else:
        out["i"] = [int(i) for i in dec.indices.tolist()]
    out["v"] = _round_list(dec.values)
    if include_raw:
        out["raw"] = [int(r) for r in wf.raw[start:stop][dec.indices].tolist()]
    return out


def _full_waveform(wf: Waveform) -> dict[str, Any]:
    """Full-resolution JSON for the ``edu09://waveforms/{id}`` resource."""
    return {
        "waveform_id": wf.waveform_id,
        "timestamp": _iso(wf),
        "settings": _settings_payload(wf.settings),
        "sample_rate_hz": round_sig(wf.sample_rate_hz),
        "dt_s": round_sig(wf.dt),
        "t0_s": round_sig(wf.t0),
        "trigger_index": wf.trigger_index,
        "triggered": wf.triggered,
        "clipped": wf.clipped,
        "acq_mode": wf.acq_mode,
        "calibration_id": wf.calibration_id,
        "n": wf.n,
        "v": _round_list(wf.volts),
        "raw": [int(r) for r in wf.raw.tolist()],
    }


# --------------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------------

_HINTS: Final[dict[type[BaseException], str]] = {
    AcquisitionTimeout: "try trigger_mode='auto', a trigger_level_v inside the signal range, "
    "or a longer timeout_s",
    Cancelled: "the acquisition was stopped (set_running(false) or another client)",
    DeviceDisconnected: "the scope is unplugged; reconnect the USB cable (the driver reconnects "
    "automatically) and retry",
    DeviceNotFound: "no EDU09 found; check the USB cable",
    DeviceAccessError: "no permission to open the device (Linux: install the udev rule)",
    UnsafeOperation: "refused for safety",
}


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, storage.WaveformNotFound):
        return "NotFound"
    return type(exc).__name__


def error_message(exc: BaseException) -> str:
    """``"<ErrorClass>: <human hint>"`` for a tool error."""
    msg = str(exc).strip()
    if isinstance(exc, DeviceBusy) and exc.owner is not None:
        msg = msg or f"EDU09 is in use by {exc.owner.describe()}"
    for cls, hint in _HINTS.items():
        if isinstance(exc, cls) and hint not in msg:
            msg = f"{msg}; {hint}" if msg else hint
            break
    return f"{_error_class(exc)}: {msg or 'no details'}"


class ToolFailure(ToolError):
    """A tool failure whose message is already ``"<ErrorClass>: <hint>"``; the server class
    (``server.Edu09MCP``) strips the SDK's ``Error executing tool <name>:`` prefix from it."""


@contextlib.contextmanager
def _tool_errors() -> Iterator[None]:
    """Map every failure to ``ToolFailure("<ErrorClass>: <hint>")`` (shown to the model)."""
    try:
        yield
    except ToolFailure:
        raise
    except Exception as exc:
        log.info("tool failed: %s", error_message(exc), exc_info=exc)
        raise ToolFailure(error_message(exc)) from exc


def _ok(payload: Any) -> CallToolResult:
    """Success result: compact JSON text (keeps responses small) + the same structured data."""
    data = _rounded(payload)
    text = json.dumps(data, separators=(",", ":"), allow_nan=False)
    structured = data if isinstance(data, dict) else {"result": data}
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured
    )


@contextlib.contextmanager
def _resource_errors() -> Iterator[None]:
    try:
        yield
    except ResourceError:
        raise
    except storage.WaveformNotFound as exc:
        raise ResourceNotFoundError(error_message(exc)) from exc
    except Exception as exc:
        raise ResourceError(error_message(exc)) from exc


def _bad(msg: str) -> ValueError:
    return ValueError(msg)


def _check_range(name: str, value: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _bad(f"{name} must be a number in [{lo:g}, {hi:g}], got {value!r}")
    if not lo <= value <= hi:
        raise _bad(f"{name} must be in [{lo:g}, {hi:g}], got {value:g}")
    return float(value)


def _check_int(name: str, value: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{name} must be an integer in [{lo}, {hi}], got {value!r}")
    if not lo <= value <= hi:
        raise _bad(f"{name} must be in [{lo}, {hi}], got {value}")
    return value


# --------------------------------------------------------------------------------------------
# export path safety
# --------------------------------------------------------------------------------------------

_WINDOWS_RESERVED: Final = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{c}" for c in "123456789¹²³"}
    | {f"lpt{c}" for c in "123456789¹²³"}
)
_FORBIDDEN_CHARS: Final = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
MAX_FILENAME_LEN: Final = 200


def check_export_filename(filename: str) -> str:
    """Validate a plain ``*.csv`` file name (no directories, portable on Windows and Linux).

    Rejects path separators, ``.``/``..``, drive letters and NTFS alternate data streams
    (``:``), characters invalid on Windows, trailing dots/spaces, Windows reserved device names
    (``CON``, ``NUL``, ``COM1``, ... with any extension) and names without a ``.csv`` suffix.
    """
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename must be a non-empty string such as 'capture.csv'")
    if len(filename) > MAX_FILENAME_LEN:
        raise ValueError(f"filename longer than {MAX_FILENAME_LEN} characters")
    if filename in (".", ".."):
        raise ValueError(f"invalid filename {filename!r}")
    bad = _FORBIDDEN_CHARS.search(filename)
    if bad:
        raise ValueError(
            f"filename {filename!r} contains {bad.group()!r}; use a plain file name like "
            "'capture.csv' (no directories, drive letters or streams)"
        )
    if filename != filename.strip() or filename.endswith("."):
        raise ValueError(f"filename {filename!r} must not start/end with spaces or end with '.'")
    if not filename.lower().endswith(".csv") or len(filename) <= len(".csv"):
        raise ValueError(f"filename {filename!r} must end with '.csv', e.g. 'capture.csv'")
    stem = filename.split(".", 1)[0].rstrip(" ").lower()
    if stem in _WINDOWS_RESERVED:
        raise ValueError(f"filename {filename!r} uses a reserved Windows device name")
    return filename


def _default_export_name(wf: Waveform) -> str:
    ts = wf.timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"edu09_{wf.waveform_id}_{ts}.csv"


# --------------------------------------------------------------------------------------------
# spectrum helpers
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _SpectrumInput:
    """Waveform view with replaced samples (despiked) for :func:`analysis.spectrum_waveform`."""

    volts: npt.NDArray[np.float64]
    dt: float
    clipped: bool
    settings: Settings


def _peak_payload(p: analysis.SpectrumPeak) -> dict[str, Any]:
    return {"freq_hz": p.freq_hz, "vrms": p.vrms, "dbv": p.dbv}


def _spectrum_points(spec: analysis.Spectrum, max_points: int, db_floor: float) -> dict[str, Any]:
    """dBV per bin, floored at ``db_floor``; max-decimated (peaks survive) to ``max_points``.

    Bin ``k`` is at ``f0_hz + k * df_hz``. When there are more bins than ``max_points`` the bins
    are split into ``max_points`` contiguous near-equal buckets and each bucket's strongest bin
    is returned; ``i`` then holds the bin index of each point.
    """
    db = np.maximum(spec.dbv, db_floor)
    m = int(db.size)
    out: dict[str, Any] = {"f0_hz": 0.0, "df_hz": spec.resolution_hz, "n": m}
    if m <= max_points:
        out["decimation"] = "none"
        out["dbv"] = _round_list(db)
        return out
    # max_points near-equal contiguous buckets (sizes differ by at most one bin)
    starts = np.linspace(0, m, max_points + 1).astype(np.intp)[:-1]
    bucket = np.cumsum(np.isin(np.arange(m), starts)) - 1
    order = np.lexsort((-db, bucket))  # by bucket, strongest first (ties: lowest bin)
    idx = order[np.searchsorted(bucket[order], np.arange(max_points))]
    out.update({"n": max_points, "decimation": "max", "i": [int(i) for i in idx.tolist()]})
    out["dbv"] = _round_list(db[idx])
    return out


# --------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------


def _capabilities() -> dict[str, Any]:
    timebases = []
    for tb in Timebase:
        e = units.timebase_entry(tb)
        timebases.append(
            {
                "label": tb.label,
                "s_per_div": tb.seconds,
                "sample_rate_hz": e.sample_rate_hz,
                "ets": tb.is_ets,
                "frame_duration_s": SAMPLES_PER_FRAME / e.sample_rate_hz,
            }
        )
    ref = units.timebase_entry(Timebase.MS2)
    return {
        "model": "Velleman EDU09 (single channel, 8-bit ADC)",
        "volts_per_div": [float(v) for v in VoltsPerDiv],
        "timebases_s_per_div": [tb.seconds for tb in Timebase],
        "timebases": timebases,
        "couplings": [c.value for c in Coupling],
        "trigger_modes": [m.value for m in TriggerMode],
        "trigger_edges": [e.value for e in TriggerEdge],
        "position_div_range": list(POSITION_DIV_RANGE),
        "samples_per_frame": SAMPLES_PER_FRAME,
        "divisions_per_frame": ref.divisions_per_frame,
        "screen_divisions": 8,
        "ets_timebases": [tb.label for tb in Timebase if tb.is_ets],
        "ets_note": "equivalent-time sampling (50/20/10 us/div) needs a repetitive, triggered "
        "signal; trigger_mode 'none' is not allowed there",
        "measurements": list(analysis.MEASUREMENTS),
        "decimation_methods": ["minmax", "stride"],
        "spectrum_windows": list(analysis.WINDOWS),
        "limits": dict(LIMITS),
    }


def register_tools(mcp: "FastMCP", scope: "Scope", opts: "ServerOptions") -> None:
    """Register every tool and resource on ``mcp`` (``auto_set``/``calibrate`` last)."""

    export_dir = Path(opts.export_dir) if opts.export_dir is not None else None

    def status_payload() -> dict[str, Any]:
        st = scope.status
        d = st.to_dict()
        d["settings"] = _settings_payload(st.settings)
        if d.get("owner") is None:
            d["owner"] = {"kind": opts.owner_kind, "pid": os.getpid()}
        d["stored_waveforms"] = len(scope.store)
        return d

    def calibration_warnings() -> list[str]:
        """``[ScopeStatus.calibration_warning]`` for an uncalibrated hardware scope, else []."""
        w = scope.status.calibration_warning
        return [w] if w is not None else []

    def get_wf(waveform_id: str) -> Waveform:
        if not isinstance(waveform_id, str) or not waveform_id.strip():
            raise ValueError("waveform_id must be 'latest' or an id such as 'w12'")
        return scope.store.get(waveform_id.strip())

    # -- status / capabilities ------------------------------------------------------------
    @mcp.tool(
        description="Scope status: connection, firmware, state (idle|running|armed|disconnected), "
        "current settings (flat, same keys as configure), last_waveform_id, calibration source, "
        "calibrated (false: no calibration file, a warning says how to calibrate), simulated "
        "flag and the device owner process."
    )
    def get_status() -> CallToolResult:
        with _tool_errors():
            return _ok(status_payload())

    @mcp.tool(
        description="Static capabilities: allowed volts_per_div, timebases (s/div, sample rate, "
        "ETS), couplings, trigger modes/edges, position range, frame size, measurement names "
        "and payload limits."
    )
    def get_capabilities() -> CallToolResult:
        with _tool_errors():
            return _ok(_capabilities())

    # -- configure ------------------------------------------------------------------------
    @mcp.tool(
        description="Partial settings update, validated atomically (all or nothing; omitted "
        "parameters keep their value). Returns the new settings and coercion warnings."
    )
    def configure(
        volts_per_div: Annotated[
            float | str | None,
            Field(description="V/div: 0.1, 0.2, 0.5, 1, 2, 5 (or '500mV')"),
        ] = None,
        coupling: Annotated[str | None, Field(description="'ac', 'dc' or 'gnd'")] = None,
        timebase: Annotated[
            float | str | None,
            Field(description="time/div, e.g. '2ms', '50us' or seconds such as 0.002"),
        ] = None,
        trigger_mode: Annotated[
            str | None,
            Field(description="'auto' (free run after a timeout), 'normal' (wait) or 'none'"),
        ] = None,
        trigger_edge: Annotated[str | None, Field(description="'rising' or 'falling'")] = None,
        trigger_level_v: Annotated[
            float | None, Field(description="trigger level in volts (must be on screen)")
        ] = None,
        position_div: Annotated[
            float | None, Field(description="vertical position in divisions, -4..4")
        ] = None,
        trigger_auto_timeout_s: Annotated[
            float | None, Field(description="auto mode: seconds before free run")
        ] = None,
    ) -> CallToolResult:
        with _tool_errors():
            partial = {
                "volts_per_div": volts_per_div,
                "coupling": coupling,
                "timebase": timebase,
                "trigger_mode": trigger_mode,
                "trigger_edge": trigger_edge,
                "trigger_level_v": trigger_level_v,
                "position_div": position_div,
                "trigger_auto_timeout_s": trigger_auto_timeout_s,
            }
            new, warnings = scope.configure_with_warnings(source="mcp", **partial)
            return _ok({"settings": _settings_payload(new), "warnings": list(warnings)})

    # -- acquisition ----------------------------------------------------------------------
    @mcp.tool(
        description="Single-shot acquisition (while running: the next new frame). Returns "
        "waveform_id, stats on the full 1024 samples and a decimated preview 'points': "
        "t0_s = time of the first sample (relative to the trigger); evenly spaced points carry "
        "dt_s (t[k] = t0_s + k*dt_s); minmax-decimated points are NOT evenly spaced and carry "
        "'i' sample offsets instead (t[k] = t0_s + i[k]*sample_dt_s)."
    )
    def acquire(
        timeout_s: Annotated[float, Field(description="trigger wait, 0.1..30 s")] = 2.0,
        max_points: Annotated[int, Field(description="preview points, 16..2048")] = 200,
        trigger_mode: Annotated[
            str | None,
            Field(description="override for this shot: 'auto', 'normal' or 'none'"),
        ] = None,
    ) -> CallToolResult:
        with _tool_errors():
            timeout = _check_range("timeout_s", timeout_s, *ACQUIRE_TIMEOUT_RANGE)
            mp = _check_int("max_points", max_points, MIN_POINTS, MAX_POINTS)
            mode = TriggerMode.parse(trigger_mode) if trigger_mode is not None else None
            wf = scope.acquire(timeout_s=timeout, trigger_mode=mode)
            return _ok(
                {
                    "waveform_id": wf.waveform_id,
                    "timestamp": _iso(wf),
                    "triggered": wf.triggered,
                    "clipped": wf.clipped,
                    "settings": _settings_payload(wf.settings),
                    "sample_rate_hz": wf.sample_rate_hz,
                    "duration_s": wf.duration_s,
                    "stats": analysis.stats_waveform(wf),
                    "points": _points(wf, 0, wf.n, mp, "minmax"),
                    "warnings": _waveform_warnings(wf) + calibration_warnings(),
                }
            )

    @mcp.tool(
        description="Samples of a stored waveform ('latest' or 'w<n>'; the store keeps the last "
        "32). start/stop select a sample index range; decimation 'minmax' (keeps peaks; points "
        "not evenly spaced -> 'i' offsets from start) or 'stride' (evenly spaced -> dt_s). "
        "Time of a point: t0_s + k*dt_s, or t0_s + i[k]*sample_dt_s."
    )
    def get_waveform(
        waveform_id: str = "latest",
        start: Annotated[int, Field(description="first sample index (0..1023)")] = 0,
        stop: Annotated[int, Field(description="end sample index, exclusive (1..1024)")] = 1024,
        max_points: Annotated[int, Field(description="16..2048")] = 1024,
        decimation: Annotated[str, Field(description="'minmax' or 'stride'")] = "minmax",
        include_raw: Annotated[bool, Field(description="also return raw ADC counts")] = False,
    ) -> CallToolResult:
        with _tool_errors():
            mp = _check_int("max_points", max_points, MIN_POINTS, MAX_POINTS)
            if decimation not in ("minmax", "stride"):
                raise ValueError(f"decimation must be 'minmax' or 'stride', got {decimation!r}")
            wf = get_wf(waveform_id)
            n = wf.n
            a = _check_int("start", start, 0, n - 1)
            b = _check_int("stop", stop, 1, n)
            if b <= a:
                raise ValueError(f"stop ({b}) must be greater than start ({a})")
            method: Literal["minmax", "stride"] = "minmax" if decimation == "minmax" else "stride"
            payload: dict[str, Any] = {"waveform_id": wf.waveform_id}
            payload.update(_points(wf, a, b, mp, method, include_raw))
            payload["warnings"] = _waveform_warnings(wf)
            return _ok(payload)

    @mcp.tool(
        description="Measurements on the full stored waveform. Names: "
        + ", ".join(analysis.MEASUREMENTS)
        + " (all if omitted). Frequency/period/duty need >= 2 periods; rise/fall use 10-90 %. "
        "Invalid results have valid=false and a note. despike=true removes isolated one-sample "
        "outliers first (useful on ETS timebases 50/20/10 us; harmful on undersampled signals)."
    )
    def measure(
        waveform_id: str = "latest",
        measurements: list[str] | None = None,
        despike: bool = False,
    ) -> CallToolResult:
        with _tool_errors():
            wf = get_wf(waveform_id)
            volts = analysis.despike(wf.volts) if despike else wf.volts
            res = analysis.measure(volts, wf.dt, measurements)
            results: dict[str, Any] = {}
            for name, m in res.items():
                item: dict[str, Any] = {"value": m.value, "unit": m.unit, "valid": m.valid}
                if m.note:
                    item["note"] = m.note
                results[name] = item
            return _ok(
                {
                    "waveform_id": wf.waveform_id,
                    "results": results,
                    "despiked": despike,
                    "warnings": _waveform_warnings(wf),
                }
            )

    @mcp.tool(
        description="FFT amplitude spectrum of the full stored frame (1024 samples -> 513 bins, "
        "0..fs/2, resolution fs/1024). window: "
        + ", ".join(analysis.WINDOWS)
        + " (flattop for accurate amplitudes, hann/blackman to separate close tones). Returns "
        "dc_v, fundamental {freq_hz, vrms, dbv}, thd_pct (harmonics 2..10 below fs/2), the "
        "strongest peaks and points: dBV per bin floored at db_floor, bin k at f0_hz + k*df_hz "
        "(when max-decimated 'i' gives each point's bin). No anti-alias filter: on realtime "
        "timebases components above fs/2 fold back (see warnings). ETS frames (50/20/10 us) are "
        "valid only for repetitive signals; despike=true (ETS only) removes isolated outliers."
    )
    def spectrum(
        waveform_id: str = "latest",
        window: Annotated[str, Field(description="hann, hamming, blackman, flattop, rect")] = (
            "hann"
        ),
        max_points: Annotated[int, Field(description="16..1025 spectrum points")] = 512,
        peaks: Annotated[int, Field(description="0..32 strongest peaks")] = 8,
        db_floor: Annotated[float, Field(description="dBV floor for points (-200..0)")] = -80.0,
        despike: Annotated[bool, Field(description="despike first (ETS frames only)")] = False,
    ) -> CallToolResult:
        with _tool_errors():
            if not isinstance(window, str) or window.strip().lower() not in analysis.WINDOWS:
                raise ValueError(
                    f"window must be one of {', '.join(analysis.WINDOWS)}, got {window!r}"
                )
            mp = _check_int("max_points", max_points, MIN_POINTS, SPECTRUM_MAX_POINTS)
            npk = _check_int("peaks", peaks, 0, SPECTRUM_MAX_PEAKS)
            floor = _check_range("db_floor", db_floor, *SPECTRUM_DB_FLOOR_RANGE)
            if not isinstance(despike, bool):
                raise ValueError("despike must be true or false")
            wf = get_wf(waveform_id)
            extra: list[str] = []
            src: analysis.SampledSignal = wf
            despiked = False
            if despike and wf.settings.timebase.is_ets:
                src = _SpectrumInput(analysis.despike(wf.volts), wf.dt, wf.clipped, wf.settings)
                despiked = True
            elif despike:
                extra.append(DESPIKE_IGNORED_WARNING)
            spec = analysis.spectrum_waveform(src, window=window, max_peaks=npk)
            warnings = list(spec.warnings) + extra
            warnings += [w for w in _waveform_warnings(wf) if w != CLIPPED_WARNING]
            fund = spec.fundamental
            return _ok(
                {
                    "waveform_id": wf.waveform_id,
                    "window": spec.window,
                    "fs_hz": spec.fs_hz,
                    "resolution_hz": spec.resolution_hz,
                    "n": spec.n,
                    "dc_v": spec.dc_v,
                    "fundamental": _peak_payload(fund) if fund is not None else None,
                    "thd_pct": spec.thd_pct,
                    "peaks": [_peak_payload(p) for p in spec.peaks],
                    "points": _spectrum_points(spec, mp, floor),
                    "despiked": despiked,
                    "warnings": warnings,
                }
            )

    @mcp.tool(
        description="Write a stored waveform as CSV (columns t_s, volts, raw; '#' header lines "
        "carry the settings) into the server's export directory. filename must be a plain "
        "'name.csv' (no directories); default edu09_<id>_<UTC time>.csv. Existing files are "
        "only replaced with overwrite=true."
    )
    def export_csv(
        waveform_id: str = "latest",
        filename: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        with _tool_errors():
            wf = get_wf(waveform_id)
            name = check_export_filename(
                filename if filename is not None else _default_export_name(wf)
            )
            target = storage.resolve_export_path(name, export_dir)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f"{name!r} exists and is not a regular file")
            if target.exists() and not overwrite:
                raise FileExistsError(f"{name!r} already exists; pass overwrite=true to replace it")
            path = storage.export_csv(wf, target)
            return _ok(
                {
                    "waveform_id": wf.waveform_id,
                    "path": str(path),
                    "rows": wf.n,
                    "columns": CSV_COLUMNS,
                }
            )

    @mcp.tool(
        description="Start (true) or stop (false) continuous acquisition. While running, "
        "frames feed the GUI and 'latest'; acquire returns the next new frame."
    )
    def set_running(running: bool) -> CallToolResult:
        with _tool_errors():
            if not isinstance(running, bool):
                raise ValueError("running must be true or false")
            scope.set_running(running)
            st = scope.status
            return _ok({"state": st.state, "running": st.running})

    @mcp.tool(description="Stored waveforms (newest first; the store keeps the last 32).")
    def list_waveforms() -> CallToolResult:
        with _tool_errors():
            out = []
            for wf in reversed(scope.store.snapshot()):
                out.append(
                    {
                        "waveform_id": wf.waveform_id,
                        "timestamp": _iso(wf),
                        "timebase": wf.settings.timebase.seconds,
                        "timebase_label": wf.settings.timebase.label,
                        "volts_per_div": float(wf.settings.ch1.volts_per_div),
                        "triggered": wf.triggered,
                        "clipped": wf.clipped,
                    }
                )
            return _ok(out)

    # -- resources ------------------------------------------------------------------------
    @mcp.resource("edu09://status", mime_type="application/json", description="get_status")
    def status_resource() -> str:
        with _resource_errors():
            return json.dumps(_rounded(status_payload()))

    @mcp.resource(
        "edu09://capabilities", mime_type="application/json", description="get_capabilities"
    )
    def capabilities_resource() -> str:
        return json.dumps(_rounded(_capabilities()))

    # the .csv template must be registered before the JSON one (first match wins)
    @mcp.resource(
        "edu09://waveforms/{waveform_id}.csv",
        mime_type="text/csv",
        description="Stored waveform as CSV (t_s, volts, raw), full resolution",
    )
    def waveform_csv_resource(waveform_id: str) -> str:
        with _resource_errors():
            return storage.waveform_to_csv(get_wf(waveform_id))

    @mcp.resource(
        "edu09://waveforms/{waveform_id}",
        mime_type="application/json",
        description="Stored waveform as JSON: all 1024 samples (v in volts, raw counts), "
        "dt_s, t0_s (time of sample 0 relative to the trigger), settings",
    )
    def waveform_resource(waveform_id: str) -> str:
        with _resource_errors():
            return json.dumps(_full_waveform(get_wf(waveform_id)), separators=(",", ":"))

    # -- auto-set and calibration (registered last) -----------------------------------------
    @mcp.tool(
        description="Automatic setup for an unknown signal (100 mV..30 V, > 50 Hz): sets DC "
        "coupling, trigger auto, the smallest unclipped V/div, a timebase showing 4-20 periods "
        "and the trigger level at the mean. Returns settings, the rationale and a waveform_id."
    )
    def auto_set(
        timeout_s: Annotated[float, Field(description="overall budget, 0.5..60 s")] = 5.0,
    ) -> CallToolResult:
        with _tool_errors():
            timeout = _check_range("timeout_s", timeout_s, *AUTOSET_TIMEOUT_RANGE)
            from edu09.device import autoset

            res = autoset.auto_set(scope, timeout_s=timeout)
            wf = getattr(res, "waveform", None)
            return _ok(
                {
                    "settings": _settings_payload(res.settings),
                    "rationale": [str(r) for r in res.rationale],
                    "waveform_id": wf.waveform_id if wf is not None else None,
                }
            )

    if opts.allow_calibration:

        @mcp.tool(
            description="Re-measure the calibration (offsets/gains) and store it (the previous "
            "file is backed up). DISCONNECT THE INPUT first; requires confirm=true and "
            "input_disconnected=true. Takes several seconds."
        )
        def calibrate(confirm: bool = False, input_disconnected: bool = False) -> CallToolResult:
            with _tool_errors():
                if not (confirm is True and input_disconnected is True):
                    raise UnsafeOperation(
                        "calibration overwrites the stored calibration and needs the input "
                        "disconnected; pass confirm=true and input_disconnected=true"
                    )
                cal = scope.calibrate(confirm=True, input_disconnected=True, save=False)
                warnings: list[str] = []
                backup: Path | None = None
                saved = False
                if scope.status.transport_kind == "hid":
                    backup = scope.set_calibration(cal, save=True)
                    saved = True
                else:
                    warnings.append("not saved: simulated/replay scope has no calibration file")
                return _ok(
                    {
                        "calibration": cal.to_dict(),
                        "calibration_id": cal.calibration_id,
                        "saved": saved,
                        "backup_path": str(backup) if backup is not None else None,
                        "warnings": warnings,
                    }
                )

        from edu09.mcp_server.cal_tools import register_calibration_tools

        register_calibration_tools(mcp, scope, ok=_ok, tool_errors=_tool_errors)
