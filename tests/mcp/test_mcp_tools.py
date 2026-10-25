"""MCP tools against a sim-backed Scope (in-memory client): schemas, payloads, errors."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from mcp import Client
from mcp_helpers import call, err, ok, run_client, text_of

from edu09.device.scope import Scope
from edu09.mcp_server.server import FastMCP, ServerOptions, build_server
from edu09.mcp_server.tools import TOOL_NAMES, round_sig
from edu09.model import Timebase, TriggerMode, VoltsPerDiv
from edu09.transport.sim import SimTransport


def _floats(obj: Any) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, float):
        return [obj]
    if isinstance(obj, dict):
        return [f for v in obj.values() for f in _floats(v)]
    if isinstance(obj, list):
        return [f for v in obj for f in _floats(v)]
    return []


def _assert_4_sig(payload: Any) -> None:
    for f in _floats(payload):
        assert f == round_sig(f), f


def _tool_names(srv: FastMCP) -> list[str]:
    async def fn(c: Client) -> list[str]:
        return [t.name for t in (await c.list_tools()).tools]

    return run_client(srv, fn)


# --------------------------------------------------------------------------- listing


def test_tool_list_without_calibration(server: FastMCP) -> None:
    assert _tool_names(server) == [n for n in TOOL_NAMES if n != "calibrate"]


def test_tool_list_with_calibration_registers_it_last(mcp_scope: Scope) -> None:
    from edu09.mcp_server.cal_tools import CAL_TOOL_NAMES

    srv = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    names = _tool_names(srv)
    assert names == list(TOOL_NAMES) + list(CAL_TOOL_NAMES)  # the wizard tools follow
    assert names[len(TOOL_NAMES) - 1] == "calibrate"


def test_schemas_have_expected_params(server: FastMCP) -> None:
    async def fn(c: Client) -> dict[str, dict[str, Any]]:
        return {t.name: t.input_schema for t in (await c.list_tools()).tools}

    schemas = run_client(server, fn)
    assert set(schemas["configure"]["properties"]) >= {
        "volts_per_div",
        "coupling",
        "timebase",
        "trigger_mode",
        "trigger_edge",
        "trigger_level_v",
        "position_div",
    }
    assert "required" not in schemas["configure"] or not schemas["configure"]["required"]
    acq = schemas["acquire"]["properties"]
    assert acq["timeout_s"]["default"] == 2.0
    assert acq["max_points"]["default"] == 200
    gw = schemas["get_waveform"]["properties"]
    assert gw["waveform_id"]["default"] == "latest"
    assert gw["max_points"]["default"] == 1024
    assert schemas["set_running"]["required"] == ["running"]
    assert schemas["auto_set"]["properties"]["timeout_s"]["default"] == 5.0


def test_unknown_tool_error(server: FastMCP) -> None:
    assert err(server, "calibrate", {"confirm": True}).startswith("NotFound: ")


# --------------------------------------------------------------------------- status/caps


def test_get_status(server: FastMCP, mcp_scope: Scope) -> None:
    st = ok(server, "get_status")
    assert st["connected"] is True
    assert st["firmware"] == "1.01"
    assert st["state"] == "idle"
    assert st["simulated"] is True
    assert st["last_waveform_id"] is None
    assert st["calibration_source"] == "default"
    assert st["owner"]["kind"] == "mcp"
    assert isinstance(st["owner"]["pid"], int)
    assert st["settings"]["volts_per_div"] == 2.0
    assert st["settings"]["timebase_label"] == "2ms"
    mcp_scope.acquire()
    assert ok(server, "get_status")["last_waveform_id"] == "w1"


def test_get_capabilities(server: FastMCP) -> None:
    caps = ok(server, "get_capabilities")
    assert caps["volts_per_div"] == [float(v) for v in VoltsPerDiv]
    assert caps["timebases_s_per_div"] == [round_sig(tb.seconds) for tb in Timebase]
    assert caps["samples_per_frame"] == 1024
    assert caps["divisions_per_frame"] == pytest.approx(32.77)
    assert caps["ets_timebases"] == ["50us", "20us", "10us"]
    assert caps["limits"]["max_points"] == 2048
    assert caps["couplings"] == ["ac", "dc", "gnd"]
    assert caps["trigger_modes"] == ["auto", "normal", "none"]
    assert caps["position_div_range"] == [-4.0, 4.0]
    assert "frequency" in caps["measurements"]
    _assert_4_sig(caps)


# --------------------------------------------------------------------------- configure


def test_configure_partial(server: FastMCP, mcp_scope: Scope) -> None:
    out = ok(server, "configure", {"volts_per_div": 0.5, "timebase": "500us"})
    assert out["warnings"] == []
    s = out["settings"]
    assert s["volts_per_div"] == 0.5
    assert s["timebase"] == 0.0005
    assert s["coupling"] == "dc"  # unchanged
    assert mcp_scope.settings.ch1.volts_per_div is VoltsPerDiv.V0_5
    assert mcp_scope.settings.timebase is Timebase.US500
    out = ok(server, "configure", {"timebase": 0.002, "trigger_edge": "falling"})
    assert out["settings"]["timebase_label"] == "2ms"
    assert out["settings"]["trigger_edge"] == "falling"


def test_configure_ets_coercion_warning(server: FastMCP, mcp_scope: Scope) -> None:
    out = ok(server, "configure", {"timebase": "50us", "trigger_mode": "none"})
    assert out["settings"]["trigger_mode"] == "auto"
    assert any("equivalent-time" in w for w in out["warnings"])
    assert mcp_scope.settings.trigger.mode is TriggerMode.AUTO


def test_configure_invalid_is_atomic(server: FastMCP, mcp_scope: Scope) -> None:
    before = mcp_scope.settings
    msg = err(server, "configure", {"timebase": "5ms", "volts_per_div": 3})
    assert msg.startswith("ValueError: ")
    assert "volts_per_div" in msg
    assert mcp_scope.settings == before
    msg = err(server, "configure", {"trigger_level_v": 100.0})
    assert msg.startswith("ValueError: ") and "out of range" in msg
    msg = err(server, "configure", {"coupling": "xyz"})
    assert msg.startswith("ValueError: ")
    assert mcp_scope.settings == before


def test_configure_wrong_type_validation_error(server: FastMCP) -> None:
    msg = err(server, "configure", {"trigger_level_v": "abc"})
    assert msg.startswith("ValidationError: trigger_level_v")
    assert "Error executing tool" not in msg


# --------------------------------------------------------------------------- acquire


def test_acquire_default_payload(server: FastMCP, mcp_scope: Scope) -> None:
    out = ok(server, "acquire")
    assert out["waveform_id"] == "w1"
    assert out["triggered"] is True
    assert out["clipped"] is False
    assert out["warnings"] == []
    assert out["sample_rate_hz"] == pytest.approx(15620)
    assert out["duration_s"] == pytest.approx(0.06554)
    st = out["stats"]
    assert st["frequency_hz"] == pytest.approx(1000, rel=0.01)
    assert st["vpp"] == pytest.approx(2.0, rel=0.05)
    assert set(st) == {"min", "max", "mean", "vpp", "vrms", "frequency_hz"}
    pts = out["points"]
    assert pts["decimation"] == "minmax"
    assert pts["factor"] > 1
    assert "dt_s" not in pts  # minmax points are not evenly spaced
    assert len(pts["v"]) == len(pts["i"]) == pts["n"] <= 200
    assert pts["i"] == sorted(set(pts["i"]))
    wf = mcp_scope.store.get("w1")
    assert pts["sample_dt_s"] == round_sig(wf.dt)
    assert pts["t0_s"] == round_sig(wf.t0)
    for i, v in zip(pts["i"], pts["v"], strict=True):
        assert v == round_sig(float(wf.volts[i]))
    # extrema survive minmax decimation
    assert max(pts["v"]) == round_sig(float(wf.volts.max()))
    assert min(pts["v"]) == round_sig(float(wf.volts.min()))
    _assert_4_sig(out)


def test_acquire_max_points_full_resolution_under_32kb(server: FastMCP) -> None:
    res = call(server, "acquire", {"max_points": 2048})
    assert not res.is_error
    out = json.loads(text_of(res))
    pts = out["points"]
    assert pts["decimation"] == "none"
    assert pts["factor"] == 1
    assert "i" not in pts
    assert len(pts["v"]) == 1024
    assert pts["dt_s"] == pts["sample_dt_s"]
    assert len(res.model_dump_json(by_alias=True).encode()) < 32 * 1024


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"timeout_s": 0.0}, "ValueError: timeout_s must be in [0.1, 30]"),
        ({"timeout_s": 31}, "ValueError: timeout_s"),
        ({"max_points": 15}, "ValueError: max_points must be in [16, 2048]"),
        ({"max_points": 4096}, "ValueError: max_points"),
        ({"trigger_mode": "sometimes"}, "ValueError: "),
        ({"max_points": "many"}, "ValidationError: max_points"),
    ],
)
def test_acquire_validation(server: FastMCP, args: dict[str, Any], needle: str) -> None:
    assert err(server, "acquire", args).startswith(needle)


def test_acquire_timeout_error_message() -> None:
    t = SimTransport.virtual(seed=0)
    t.device.never_trigger = True
    with Scope.open(t, reconnect_interval_s=0.01) as scope:
        srv = build_server(scope, ServerOptions())
        msg = err(srv, "acquire", {"timeout_s": 0.1, "trigger_mode": "normal"})
    assert msg.startswith("AcquisitionTimeout: ")
    assert "trigger_mode='auto'" in msg


def test_acquire_auto_fallback_warns() -> None:
    t = SimTransport.virtual(seed=0)
    t.device.never_trigger = True
    with Scope.open(t, reconnect_interval_s=0.01) as scope:
        srv = build_server(scope, ServerOptions())
        out = ok(srv, "acquire", {"trigger_mode": "auto"})
    assert out["triggered"] is False
    assert any(w.startswith("not triggered") for w in out["warnings"])


def test_clipped_warning() -> None:
    with Scope.open(SimTransport.virtual("sine:1khz:20vpp", seed=0)) as scope:
        scope.configure(volts_per_div=0.1)
        srv = build_server(scope, ServerOptions())
        out = ok(srv, "acquire")
    assert out["clipped"] is True
    assert "clipped: increase volts_per_div" in out["warnings"]


# --------------------------------------------------------------------------- get_waveform


def test_get_waveform_no_waveform_yet(server: FastMCP) -> None:
    assert err(server, "get_waveform").startswith("NotFound: ")


def test_get_waveform_default_full(server: FastMCP, mcp_scope: Scope) -> None:
    wf = mcp_scope.acquire()
    out = ok(server, "get_waveform")
    assert out["waveform_id"] == wf.waveform_id
    assert out["decimation"] == "none"
    assert len(out["v"]) == 1024
    assert out["dt_s"] == round_sig(wf.dt)
    assert out["t0_s"] == round_sig(wf.t0)
    assert "raw" not in out
    _assert_4_sig(out)


def test_get_waveform_minmax_slice_with_raw(server: FastMCP, mcp_scope: Scope) -> None:
    wf = mcp_scope.acquire()
    out = ok(
        server,
        "get_waveform",
        {
            "waveform_id": wf.waveform_id,
            "start": 100,
            "stop": 900,
            "max_points": 64,
            "include_raw": True,
        },
    )
    assert out["start"] == 100 and out["stop"] == 900
    assert out["decimation"] == "minmax"
    assert "dt_s" not in out
    assert len(out["v"]) == len(out["i"]) == len(out["raw"]) <= 64
    assert out["t0_s"] == round_sig(float(wf.t()[100]))
    for i, v, r in zip(out["i"], out["v"], out["raw"], strict=True):
        assert 0 <= i < 800
        assert v == round_sig(float(wf.volts[100 + i]))
        assert r == int(wf.raw[100 + i])


def test_get_waveform_stride_is_evenly_spaced(server: FastMCP, mcp_scope: Scope) -> None:
    wf = mcp_scope.acquire()
    out = ok(server, "get_waveform", {"max_points": 100, "decimation": "stride"})
    assert out["decimation"] == "stride"
    assert "i" not in out
    f = out["factor"]
    assert out["dt_s"] == round_sig(wf.dt * f)
    assert out["v"] == [round_sig(float(x)) for x in wf.volts[::f]]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"start": -1}, "ValueError: start"),
        ({"stop": 1025}, "ValueError: stop"),
        ({"start": 500, "stop": 500}, "ValueError: stop (500) must be greater"),
        ({"max_points": 8}, "ValueError: max_points"),
        ({"decimation": "average"}, "ValueError: decimation"),
        ({"waveform_id": "w999"}, "NotFound: waveform 'w999'"),
        ({"waveform_id": " "}, "ValueError: waveform_id"),
    ],
)
def test_get_waveform_validation(
    server: FastMCP, mcp_scope: Scope, args: dict[str, Any], needle: str
) -> None:
    mcp_scope.acquire()
    assert err(server, "get_waveform", args).startswith(needle)


def test_evicted_waveform_message(server: FastMCP, mcp_scope: Scope) -> None:
    for _ in range(33):
        mcp_scope.acquire()
    msg = err(server, "get_waveform", {"waveform_id": "w1"})
    assert msg.startswith("NotFound: ") and "last 32" in msg
    assert err(server, "measure", {"waveform_id": "w1"}).startswith("NotFound: ")


# --------------------------------------------------------------------------- measure


def test_measure_all_and_subset(server: FastMCP, mcp_scope: Scope) -> None:
    mcp_scope.acquire()
    out = ok(server, "measure")
    res = out["results"]
    assert list(res)[:3] == ["vpp", "vmax", "vmin"]
    assert len(res) == 13
    assert res["frequency"]["valid"] is True
    assert res["frequency"]["unit"] == "Hz"
    assert res["frequency"]["value"] == pytest.approx(1000, rel=0.01)
    assert res["period"]["value"] == pytest.approx(1e-3, rel=0.01)
    _assert_4_sig(out)
    sub = ok(server, "measure", {"measurements": ["vrms", "duty"]})["results"]
    assert list(sub) == ["vrms", "duty"]
    assert sub["vrms"]["value"] == pytest.approx(math.sqrt(0.5), rel=0.05)


def test_measure_invalid_has_note() -> None:
    with Scope.open(SimTransport.virtual("dc:0.5v", seed=0)) as scope:
        scope.acquire()
        srv = build_server(scope, ServerOptions())
        res = ok(srv, "measure", {"measurements": ["frequency", "mean"]})["results"]
    assert res["frequency"]["valid"] is False
    assert res["frequency"]["value"] is None
    assert res["frequency"]["note"]
    assert res["mean"]["value"] == pytest.approx(0.5, abs=0.1)


def test_measure_unknown_name_and_despike(server: FastMCP, mcp_scope: Scope) -> None:
    mcp_scope.acquire()
    msg = err(server, "measure", {"measurements": ["vpp", "loudness"]})
    assert msg.startswith("ValueError: unknown measurement 'loudness'")
    out = ok(server, "measure", {"measurements": ["vpp"], "despike": True})
    assert out["despiked"] is True


# --------------------------------------------------------------------------- run / list


def test_set_running_and_list_waveforms(server: FastMCP, mcp_scope: Scope) -> None:
    assert ok(server, "list_waveforms") == []
    out = ok(server, "set_running", {"running": True})
    assert out == {"state": "running", "running": True}
    mcp_scope.acquire()
    mcp_scope.acquire()
    out = ok(server, "set_running", {"running": False})
    assert out["running"] is False
    assert out["state"] == "idle"
    lst = ok(server, "list_waveforms")
    assert len(lst) >= 2
    seqs = [int(w["waveform_id"][1:]) for w in lst]
    assert seqs == sorted(seqs, reverse=True)  # newest first
    assert set(lst[0]) >= {"waveform_id", "timestamp", "timebase", "volts_per_div"}
    assert lst[0]["timebase"] == 0.002
    assert lst[0]["timestamp"].endswith("Z")


def test_set_running_requires_bool(server: FastMCP) -> None:
    assert err(server, "set_running", {}).startswith("ValidationError: running")


def test_list_waveforms_structured_content(server: FastMCP, mcp_scope: Scope) -> None:
    mcp_scope.acquire()
    res = call(server, "list_waveforms")
    assert res.structured_content == {"result": json.loads(text_of(res))}


# --------------------------------------------------------------------------- auto_set


def test_auto_set_tool(server: FastMCP, mcp_scope: Scope) -> None:
    pytest.importorskip("edu09.device.autoset")
    out = ok(server, "auto_set", {"timeout_s": 10.0})
    assert set(out) == {"settings", "rationale", "waveform_id"}
    assert out["rationale"] and all(isinstance(r, str) for r in out["rationale"])
    assert out["settings"]["coupling"] == "dc"
    assert out["settings"]["trigger_mode"] == "auto"
    if out["waveform_id"] is not None:
        assert out["waveform_id"] in mcp_scope.store
    assert mcp_scope.settings.to_flat_dict()["volts_per_div"] == out["settings"]["volts_per_div"]


def test_auto_set_timeout_validation(server: FastMCP) -> None:
    assert err(server, "auto_set", {"timeout_s": 0.1}).startswith("ValueError: timeout_s")


def test_auto_set_missing_module(server: FastMCP, monkeypatch: pytest.MonkeyPatch) -> None:
    import edu09.mcp_server.tools as tools

    monkeypatch.setattr(tools, "AUTOSET_MODULE", "edu09.device._no_such_autoset")
    assert err(server, "auto_set").startswith("NotImplemented: ")


# --------------------------------------------------------------------------- calibrate


def test_calibrate_requires_flags(mcp_scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(mcp_scope, "calibrate", lambda **kw: called.append(kw))
    srv = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    for args in ({}, {"confirm": True}, {"input_disconnected": True}):
        assert err(srv, "calibrate", args).startswith("UnsafeOperation: ")
    assert called == []


def test_calibrate_on_sim_is_not_saved(mcp_scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from edu09 import units

    cal = units.Calibration(base_ofs=0x82, source="measured")
    seen: list[dict[str, Any]] = []

    def fake_calibrate(**kw: Any) -> units.Calibration:
        seen.append(kw)
        return cal

    monkeypatch.setattr(mcp_scope, "calibrate", fake_calibrate)
    srv = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    out = ok(srv, "calibrate", {"confirm": True, "input_disconnected": True})
    assert seen == [{"confirm": True, "input_disconnected": True, "save": False}]
    assert out["saved"] is False
    assert out["backup_path"] is None
    assert out["calibration"]["base_ofs"] == 0x82
    assert out["calibration_id"] == cal.calibration_id
    assert out["warnings"]


def test_calibrate_error_mapping(mcp_scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from edu09.errors import CalibrationError

    def boom(**kw: Any) -> None:
        raise CalibrationError("input not disconnected: level drifts")

    monkeypatch.setattr(mcp_scope, "calibrate", boom)
    srv = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    msg = err(srv, "calibrate", {"confirm": True, "input_disconnected": True})
    assert msg == "CalibrationError: input not disconnected: level drifts"
