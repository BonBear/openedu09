"""Helpers for the MCP tests (in-memory or HTTP MCP client calls)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp import Client
from mcp.types import CallToolResult


def run_client[T](srv: Any, fn: Callable[[Client], Awaitable[T]]) -> T:
    """Connect an MCP client (in-process server or HTTP URL), run ``fn(client)``."""

    async def main() -> T:
        async with Client(srv) as client:
            return await fn(client)

    return anyio.run(main)


def call(srv: Any, tool: str, args: dict[str, Any] | None = None) -> CallToolResult:
    async def fn(c: Client) -> CallToolResult:
        return await c.call_tool(tool, args or {})

    return run_client(srv, fn)


def text_of(res: CallToolResult) -> str:
    block = res.content[0]
    return block.text  # type: ignore[union-attr]


def ok(srv: Any, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call ``tool`` and return its parsed JSON payload; fails on a tool error."""
    res = call(srv, tool, args)
    assert not res.is_error, text_of(res)
    return json.loads(text_of(res))


def err(srv: Any, tool: str, args: dict[str, Any] | None = None) -> str:
    """Call ``tool`` expecting a tool error; return the message."""
    res = call(srv, tool, args)
    assert res.is_error, text_of(res)
    return text_of(res)
