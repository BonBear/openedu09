"""MCP resources: status, capabilities, waveforms as JSON and CSV."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client, MCPError
from mcp_helpers import run_client

from edu09.device.scope import Scope
from edu09.mcp_server.server import FastMCP
from edu09.mcp_server.tools import round_sig


def _read(srv: FastMCP, uri: str) -> tuple[str, str | None]:
    async def fn(c: Client) -> tuple[str, str | None]:
        res = await c.read_resource(uri)
        block = res.contents[0]
        return block.text, block.mime_type  # type: ignore[union-attr]

    return run_client(srv, fn)


def _read_error(srv: FastMCP, uri: str) -> str:
    async def fn(c: Client) -> str:
        with pytest.raises(MCPError) as ei:
            await c.read_resource(uri)
        return str(ei.value)

    return run_client(srv, fn)


def test_resource_listing(server: FastMCP) -> None:
    async def fn(c: Client) -> tuple[list[str], list[str]]:
        res = [str(r.uri) for r in (await c.list_resources()).resources]
        tpl = [t.uri_template for t in (await c.list_resource_templates()).resource_templates]
        return res, tpl

    res, tpl = run_client(server, fn)
    assert sorted(res) == ["edu09://capabilities", "edu09://status"]
    assert sorted(tpl) == ["edu09://waveforms/{waveform_id}", "edu09://waveforms/{waveform_id}.csv"]


def test_status_and_capabilities_resources(server: FastMCP) -> None:
    text, mime = _read(server, "edu09://status")
    assert mime == "application/json"
    st = json.loads(text)
    assert st["connected"] is True and st["simulated"] is True
    caps = json.loads(_read(server, "edu09://capabilities")[0])
    assert caps["samples_per_frame"] == 1024


@pytest.mark.parametrize("wid", ["w1", "latest"])
def test_waveform_json_resource(server: FastMCP, mcp_scope: Scope, wid: str) -> None:
    wf = mcp_scope.acquire()
    text, mime = _read(server, f"edu09://waveforms/{wid}")
    assert mime == "application/json"
    d: dict[str, Any] = json.loads(text)
    assert d["waveform_id"] == wf.waveform_id
    assert d["n"] == 1024
    assert len(d["v"]) == len(d["raw"]) == 1024
    assert d["raw"] == [int(x) for x in wf.raw]
    assert d["v"][10] == round_sig(float(wf.volts[10]))
    assert d["dt_s"] == round_sig(wf.dt)
    assert d["t0_s"] == round_sig(wf.t0)
    assert d["trigger_index"] == wf.trigger_index
    assert d["settings"]["timebase_label"] == "2ms"


def test_waveform_csv_resource(server: FastMCP, mcp_scope: Scope) -> None:
    wf = mcp_scope.acquire()
    text, mime = _read(server, f"edu09://waveforms/{wf.waveform_id}.csv")
    assert mime == "text/csv"
    lines = text.splitlines()
    assert lines[0].startswith("#")
    header = [ln for ln in lines if not ln.startswith("#")]
    assert header[0] == "t_s,volts,raw"
    assert len(header) == 1 + 1024
    assert int(header[1].split(",")[2]) == int(wf.raw[0])


def test_waveform_resource_not_found(server: FastMCP, mcp_scope: Scope) -> None:
    assert "NotFound" in _read_error(server, "edu09://waveforms/latest")
    mcp_scope.acquire()
    msg = _read_error(server, "edu09://waveforms/w42")
    assert msg.startswith("NotFound: ") and "last 32" in msg
    assert "NotFound" in _read_error(server, "edu09://waveforms/w42.csv")
