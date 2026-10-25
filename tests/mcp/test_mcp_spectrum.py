"""MCP ``spectrum`` tool (docs/specs/spectrum.md, task S1). Sim-backed only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from mcp import Client
from mcp_helpers import call, err, ok, run_client, text_of

from edu09.analysis import CLIPPED_WARNING, ETS_WARNING
from edu09.device.scope import Scope
from edu09.mcp_server.server import FastMCP, ServerOptions, build_server
from edu09.mcp_server.tools import DESPIKE_IGNORED_WARNING, TOOL_NAMES, round_sig
from edu09.transport.sim import SimTransport


def _floats(obj: Any) -> Iterator[float]:
    if isinstance(obj, float):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _floats(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _floats(v)


def _sim_server(signal: str, **configure: Any) -> tuple[Scope, FastMCP]:
    scope = Scope.open(SimTransport.virtual(signal, seed=0), reconnect_interval_s=0.01)
    if configure:
        scope.configure(**configure)
    return scope, build_server(scope, ServerOptions())


@pytest.fixture
def sine_server() -> Iterator[FastMCP]:
    """5 kHz sine, 2 Vpp, 200 us/div realtime (156.25 kHz)."""
    scope, srv = _sim_server("sine:5khz:2vpp", timebase="200us", volts_per_div=0.5)
    try:
        scope.acquire()
        yield srv
    finally:
        scope.close()


def test_registered_after_measure(server: FastMCP) -> None:
    async def fn(c: Client) -> list[str]:
        return [t.name for t in (await c.list_tools()).tools]

    names = run_client(server, fn)
    assert "spectrum" in names
    assert names.index("spectrum") == names.index("measure") + 1
    assert TOOL_NAMES.index("spectrum") == TOOL_NAMES.index("measure") + 1
    assert ok(server, "get_capabilities")["spectrum_windows"] == [
        "hann",
        "hamming",
        "blackman",
        "flattop",
        "rect",
    ]


def test_no_waveform_yet(server: FastMCP) -> None:
    assert err(server, "spectrum").startswith("NotFound: ")


def test_default_payload(sine_server: FastMCP) -> None:
    out = ok(sine_server, "spectrum")
    assert out["waveform_id"] == "w1"
    assert out["window"] == "hann"
    assert out["n"] == 1024
    assert out["fs_hz"] == pytest.approx(156_250, rel=1e-3)
    assert out["resolution_hz"] == pytest.approx(152.6)
    assert out["fundamental"]["freq_hz"] == pytest.approx(5_000, rel=0.01)
    assert out["fundamental"]["vrms"] == pytest.approx(0.7071, rel=0.03)
    assert set(out["fundamental"]) == {"freq_hz", "vrms", "dbv"}
    assert 1 <= len(out["peaks"]) <= 8
    assert out["peaks"][0] == out["fundamental"]
    pts = out["points"]
    assert pts["f0_hz"] == 0.0
    assert pts["df_hz"] == out["resolution_hz"]
    # default 512 points < 513 bins: max-decimated, one bucket holds two bins
    assert pts["decimation"] == "max"
    assert len(pts["dbv"]) == len(pts["i"]) == pts["n"] == 512
    assert pts["i"] == sorted(set(pts["i"]))
    assert pts["i"][0] in (0, 1) and pts["i"][-1] in (511, 512)
    assert min(pts["dbv"]) >= -80.0
    assert out["despiked"] is False
    assert out["warnings"] == [
        "aliasing: components above fs/2 = 78.12 kHz fold back (input bandwidth ~200 kHz)"
    ]
    for f in _floats(out):
        assert f == round_sig(f), f


def test_points_max_decimated_keeps_peak(sine_server: FastMCP) -> None:
    full = ok(sine_server, "spectrum", {"max_points": 1025})["points"]
    assert full["decimation"] == "none" and len(full["dbv"]) == 513
    small = ok(sine_server, "spectrum", {"max_points": 16})["points"]
    assert small["decimation"] == "max"
    assert len(small["dbv"]) == len(small["i"]) == small["n"] <= 16
    assert small["i"] == sorted(small["i"])
    assert max(small["dbv"]) == max(full["dbv"])  # the 5 kHz peak survives
    k = small["i"][small["dbv"].index(max(small["dbv"]))]
    assert k * small["df_hz"] == pytest.approx(5_000, rel=0.02)


def test_db_floor_peaks_window(sine_server: FastMCP) -> None:
    out = ok(sine_server, "spectrum", {"db_floor": -20.0, "peaks": 0, "window": "flattop"})
    assert min(out["points"]["dbv"]) == -20.0
    assert out["peaks"] == []
    assert out["fundamental"] is not None
    assert out["window"] == "flattop"
    assert out["fundamental"]["vrms"] == pytest.approx(0.7071, rel=0.02)


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"window": "kaiser"}, "ValueError: window must be one of"),
        ({"max_points": 15}, "ValueError: max_points must be in [16, 1025]"),
        ({"max_points": 1026}, "ValueError: max_points"),
        ({"peaks": 33}, "ValueError: peaks must be in [0, 32]"),
        ({"peaks": -1}, "ValueError: peaks"),
        ({"db_floor": 1.0}, "ValueError: db_floor"),
        ({"db_floor": -201.0}, "ValueError: db_floor"),
        ({"waveform_id": "w99"}, "NotFound: "),
    ],
)
def test_validation(sine_server: FastMCP, args: dict[str, Any], needle: str) -> None:
    assert err(sine_server, "spectrum", args).startswith(needle)


def test_despike_ignored_on_realtime(sine_server: FastMCP) -> None:
    out = ok(sine_server, "spectrum", {"despike": True})
    assert out["despiked"] is False
    assert DESPIKE_IGNORED_WARNING in out["warnings"]


def test_ets_frame_warns_and_despikes() -> None:
    scope, srv = _sim_server(
        "square:30khz:4vpp:2v", timebase="50us", volts_per_div=1.0, trigger_level_v=2.0
    )
    try:
        scope.acquire()
        out = ok(srv, "spectrum", {"despike": True})
    finally:
        scope.close()
    assert out["despiked"] is True
    assert ETS_WARNING in out["warnings"]
    assert not any(w.startswith("aliasing") for w in out["warnings"])  # fs = 625 kHz
    assert DESPIKE_IGNORED_WARNING not in out["warnings"]
    assert out["fundamental"]["freq_hz"] == pytest.approx(30_000, rel=0.01)
    assert out["dc_v"] == pytest.approx(2.0, abs=0.2)


def test_clipped_frame_warns_once() -> None:
    scope, srv = _sim_server("sine:1khz:20vpp", volts_per_div=0.1)
    try:
        scope.acquire()
        out = ok(srv, "spectrum")
    finally:
        scope.close()
    assert CLIPPED_WARNING in out["warnings"]
    assert sum(w.startswith("clipped") for w in out["warnings"]) == 1


def test_payload_under_32kb(sine_server: FastMCP) -> None:
    res = call(sine_server, "spectrum", {"max_points": 1025, "peaks": 32, "db_floor": -200.0})
    assert not res.is_error
    text = text_of(res)
    assert len(text.encode()) < 32 * 1024
    assert len(json.loads(text)["points"]["dbv"]) == 513
