"""MCP review additions: hostile inputs and the tool surface."""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client, MCPError
from mcp_helpers import call, err, run_client, text_of

from edu09.device.scope import Scope
from edu09.mcp_server.server import FastMCP

JUNK_IDS = [
    "",
    "  ",
    "../../etc/passwd",
    "w1.csv",
    "w0",
    "w-1",
    "w" + "9" * 400,
    "latest\x00",
    "W1",
]


@pytest.mark.parametrize("wid", JUNK_IDS)
@pytest.mark.parametrize("tool", ["get_waveform", "measure", "export_csv"])
def test_junk_waveform_ids_are_clean_tool_errors(
    server: FastMCP, mcp_scope: Scope, tool: str, wid: str
) -> None:
    mcp_scope.acquire()
    res = call(server, tool, {"waveform_id": wid})
    assert res.is_error, text_of(res)
    msg = text_of(res)
    assert ": " in msg.split("\n")[0] and "Traceback" not in msg
    assert msg.split(":")[0] in {"NotFound", "ValueError", "ValidationError"}


@pytest.mark.parametrize("wid", [w for w in JUNK_IDS[2:] if w != "w1.csv"])  # w1.csv is valid
def test_junk_waveform_resources_raise_mcp_errors(
    server: FastMCP, mcp_scope: Scope, wid: str
) -> None:
    mcp_scope.acquire()

    async def fn(c: Client) -> None:
        for suffix in ("", ".csv"):
            with pytest.raises(MCPError):
                await c.read_resource(f"edu09://waveforms/{wid}{suffix}")

    run_client(server, fn)


@pytest.mark.parametrize(
    "args",
    [
        {"timeout_s": 1e308},
        {"timeout_s": -1},
        {"max_points": 2**63},
        {"max_points": 10**30},
        {"trigger_mode": "x" * 10_000},
    ],
)
def test_acquire_extreme_args(server: FastMCP, args: dict[str, Any]) -> None:
    msg = err(server, "acquire", args)
    assert msg.split(":")[0] in {"ValueError", "ValidationError"}, msg


def test_no_tool_accepts_raw_bytes_or_opcodes(server: FastMCP) -> None:
    """No parameter name suggests raw device access; the surface is the documented one."""

    async def fn(c: Client) -> dict[str, set[str]]:
        return {
            t.name: set((t.input_schema or {}).get("properties", {}))
            for t in (await c.list_tools()).tools
        }

    surface = run_client(server, fn)
    banned = {"opcode", "bytes", "hex", "data", "payload", "command", "report", "raw_bytes"}
    for name, params in surface.items():
        assert not params & banned, (name, params)
    assert "calibrate" not in surface and "send" not in surface
