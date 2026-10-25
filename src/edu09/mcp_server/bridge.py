"""stdio <-> streamable-HTTP MCP bridge to the process that owns the EDU09 (architecture.md 3.4).

When ``edu09-mcp`` (stdio) finds the device owned by another process (usually the GUI) whose
``owner.json`` publishes an MCP URL, it serves the owner's tools and resources over stdio by
forwarding every request to that URL. An agent's MCP config (``edu09-mcp``) then works whether or
not the GUI is open, and agent and human share one device and one settings state.

Public API (used by :mod:`edu09.mcp_server.__main__`)::

    check_owner_url(url) -> str                     # ValueError unless http(s) to loopback
    resolve_owner(owner, lock_dir=None, wait_s=1.0) -> OwnerInfo | None
    probe(url, timeout_s=5.0) -> UpstreamInfo       # OwnerUnreachable if it does not answer
    Bridge(url, owner=None, lock_dir=None, info=None)
        .build_server() -> mcp.server.lowlevel.Server
        .run_stdio() -> None                        # blocks until stdin closes

Design decisions:

* **One upstream connection per request.** Each forwarded request opens a short-lived
  streamable-HTTP client session (a few ms on loopback). Nothing upstream is session-scoped
  (all state lives in the owner's ``Scope``), and it makes the bridge immune to the owner
  restarting its HTTP server: before every request ``owner.json`` is re-read and, if the owner
  now publishes a different (loopback) URL, the bridge follows it.
* **Owner gone mid-session = clear per-call error, no takeover.** If the owner exits, tool calls
  return ``"OwnerGone: ..."`` tool errors (other requests an MCP error with the same text). The
  bridge never opens the device itself mid-session: that would silently reset settings and
  waveform ids under the agent and lock the GUI out when the user restarts it. Restarting
  ``edu09-mcp`` (or the agent's MCP client) opens the device directly. At *startup* the CLI does
  fall back to opening the device when the owner went away between ``DeviceBusy`` and the probe.
* **Loopback only.** ``owner.json`` is user-writable, so a URL planted there must not make the
  bridge send the agent's requests elsewhere: only ``http(s)://`` URLs whose host is a loopback
  IP literal or ``localhost`` are used (checked at startup and on every re-read).
* **owner.json races.** It is replaced atomically, but readers can still see it missing (owner
  just starting / exiting) or locked (Windows); :func:`resolve_owner` and the per-request
  re-read retry briefly. Still missing after the retries = the owner has exited: the request
  fails with ``OwnerGone`` (the old URL is never reused: another program may listen there now).
* **Bounded upstream exchanges.** A forwarded tool call is cut off after
  ``TOOL_CALL_TIMEOUT_S`` (330 s: longer than any server-side tool timeout), other requests after
  ``REQUEST_TIMEOUT_S``; a non-streamed (JSON) upstream response larger than
  ``MAX_RESPONSE_BYTES`` (8 MiB) is refused instead of being buffered (SSE events are capped by
  the SDK).

Nothing here writes to stdout except the MCP protocol itself (stdio transport).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit

import anyio
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from edu09 import __version__
from edu09.device.lock import OwnerInfo, read_owner

__all__ = [
    "OWNER_GONE",
    "Bridge",
    "OwnerUnreachable",
    "UpstreamInfo",
    "check_owner_url",
    "probe",
    "resolve_owner",
]

log = logging.getLogger(__name__)

OWNER_GONE: Final = "OwnerGone"
"""Error class prefix of tool errors when the owner cannot be reached."""
RESOLVE_INTERVAL_S: Final = 0.05
OWNER_READ_RETRIES: Final = 3
DEFAULT_PROBE_TIMEOUT_S: Final = 5.0
REQUEST_TIMEOUT_S: Final = 30.0
"""Bound on one forwarded list/read request (connect + handshake + request)."""
TOOL_CALL_TIMEOUT_S: Final = 330.0
"""Bound on one forwarded tool call (connect + handshake + call). Longer than every server-side
tool timeout (``calibrate`` <= 300 s), so it only ends calls to a hung owner."""
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
"""Largest non-streamed (``application/json``) upstream response body the bridge accepts."""
HTTP_TIMEOUT_S: Final = 30.0
HTTP_READ_TIMEOUT_S: Final = 300.0
"""httpx connect/write/pool and per-read timeouts (the SDK's defaults: a tool call such as
``calibrate`` may take minutes and the response stream stays quiet meanwhile)."""

_T = TypeVar("_T")


class OwnerUnreachable(Exception):
    """The owner's MCP endpoint did not answer (owner exited, HTTP server stopped, ...)."""


class ResponseTooLarge(httpx2.StreamError):
    """An upstream JSON response exceeded ``MAX_RESPONSE_BYTES`` (or used a content encoding,
    which could expand it unboundedly)."""


class _CappedStream(httpx2.AsyncByteStream):
    """Response body stream that fails once more than ``limit`` bytes arrived."""

    def __init__(self, inner: httpx2.AsyncByteStream, limit: int, encoding: str) -> None:
        self._inner = inner
        self._limit = limit
        self._encoding = encoding

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._encoding not in ("", "identity"):
            raise ResponseTooLarge(
                f"refusing a {self._encoding!r}-encoded upstream response (size cannot be bounded)"
            )
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._limit:
                raise ResponseTooLarge(
                    f"upstream response larger than {self._limit} bytes; refused"
                )
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx2.AsyncBaseTransport):
    """Wraps a transport: caps the body of non-streamed (``application/json``) responses.

    The SDK buffers JSON responses completely (``response.aread()``); an owner (or whatever
    listens on the published port) could otherwise make the bridge buffer an unbounded body.
    ``text/event-stream`` responses pass through (the SDK caps each SSE event)."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        ctype = response.headers.get("content-type", "").lower()
        if not ctype.startswith("text/event-stream"):
            stream = response.stream
            assert isinstance(stream, httpx2.AsyncByteStream)
            encoding = response.headers.get("content-encoding", "").strip().lower()
            response.stream = _CappedStream(stream, self._limit, encoding)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(frozen=True)
class UpstreamInfo:
    """What the owner's server announced (mirrored by the bridge's stdio server)."""

    name: str = "edu09"
    version: str = __version__
    instructions: str | None = None


def check_owner_url(url: str) -> str:
    """Return ``url`` if it is ``http(s)://<loopback>[:port]/...``; else raise ``ValueError``.

    Loopback = an IP literal in 127.0.0.0/8 or ::1, or ``localhost``. User info in the URL
    (``http://user@...``) is refused too.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # ValueError for a malformed port
    except ValueError as exc:
        raise ValueError(f"malformed MCP URL {url!r}: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"refusing MCP URL {url!r}: scheme must be http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"refusing MCP URL {url!r}: user info is not allowed")
    if not host:
        raise ValueError(f"refusing MCP URL {url!r}: no host")
    if host.lower() != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(
                f"refusing MCP URL {url!r}: the bridge only connects to loopback addresses "
                "(127.0.0.1, ::1, localhost)"
            )
    return url


async def _read_owner_retry(
    lock_dir: Path | None, retries: int = OWNER_READ_RETRIES
) -> OwnerInfo | None:
    """``read_owner`` that retries a missing/unreadable file a few times (replace races)."""
    for attempt in range(max(1, retries)):
        owner = read_owner(lock_dir)
        if owner is not None:
            return owner
        if attempt < retries - 1:
            await anyio.sleep(RESOLVE_INTERVAL_S)
    return None


def resolve_owner(
    owner: OwnerInfo | None,
    lock_dir: str | os.PathLike[str] | None = None,
    wait_s: float = 1.0,
) -> OwnerInfo | None:
    """The owner to bridge to: ``owner`` (from ``DeviceBusy``) if it has an MCP URL, else
    ``owner.json`` re-read for up to ``wait_s`` until it names one (the owner may still be
    starting its HTTP server, or ``owner.json`` was mid-replace). Returns the last owner seen
    (possibly without a URL), or None if there is none."""
    if owner is not None and owner.mcp_url:
        return owner
    path = Path(lock_dir) if lock_dir is not None else None
    deadline = time.monotonic() + max(0.0, wait_s)
    last = owner
    while True:
        current = read_owner(path)
        if current is not None:
            last = current
            if current.mcp_url:
                return current
        if time.monotonic() >= deadline:
            return last
        time.sleep(RESOLVE_INTERVAL_S)


def _describe_error(exc: BaseException) -> str:
    """Innermost meaningful error of an (exception-group wrapped) connection failure."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _find_mcp_error(exc: BaseException) -> MCPError | None:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        if len(exc.exceptions) != 1:
            return None
        exc = exc.exceptions[0]
    return exc if isinstance(exc, MCPError) else None


@asynccontextmanager
async def _client(url: str) -> AsyncIterator[Client]:
    """An MCP client session to ``url`` that ignores proxy settings.

    httpx honours ``HTTP(S)_PROXY`` / ``ALL_PROXY`` (and, on Windows/macOS, the system proxy)
    even for loopback hosts unless ``NO_PROXY`` lists them, which would send every forwarded
    request (and the owner's waveforms) to the proxy instead of the local owner. The owner is
    always on loopback, so environment proxies are never wanted (``trust_env=False``; this also
    skips ``.netrc`` credentials). Redirects are not followed off-origin by the SDK transport.
    Non-streamed response bodies are capped at ``MAX_RESPONSE_BYTES`` (:class:`_CappedTransport`;
    uncompressed bodies only: ``Accept-Encoding: identity``).
    """
    timeout = httpx2.Timeout(HTTP_TIMEOUT_S, read=HTTP_READ_TIMEOUT_S)
    transport = _CappedTransport(httpx2.AsyncHTTPTransport(trust_env=False), MAX_RESPONSE_BYTES)
    async with (
        httpx2.AsyncClient(
            trust_env=False,
            timeout=timeout,
            transport=transport,
            headers={"Accept-Encoding": "identity"},
        ) as http,
        Client(streamable_http_client(url, http_client=http), cache=None) as c,
    ):
        yield c


async def _probe_async(url: str, timeout_s: float) -> UpstreamInfo:
    with anyio.fail_after(timeout_s):
        async with _client(url) as c:
            await c.list_tools()
            info = c.server_info
            return UpstreamInfo(
                name=info.name if info is not None and info.name else "edu09",
                version=info.version if info is not None and info.version else __version__,
                instructions=c.instructions,
            )


def probe(url: str, timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> UpstreamInfo:
    """Connect to ``url`` once (``tools/list``) and return its server info.

    Raises ``ValueError`` for a non-loopback URL and :class:`OwnerUnreachable` if the endpoint
    does not answer within ``timeout_s``. Must not be called from a running event loop.
    """
    check_owner_url(url)
    try:
        return anyio.run(_probe_async, url, timeout_s)
    except TimeoutError:
        raise OwnerUnreachable(f"no answer from {url} within {timeout_s:g} s") from None
    except Exception as exc:
        raise OwnerUnreachable(f"cannot reach {url} ({_describe_error(exc)})") from None


class Bridge:
    """A stdio MCP server forwarding tools and resources to the owner's HTTP endpoint.

    ``url`` is the owner's MCP URL (checked with :func:`check_owner_url`); ``owner`` is used in
    error messages; ``lock_dir`` is where ``owner.json`` is re-read before every request (None =
    the default lock dir; ``follow_owner=False`` disables the re-read). ``info`` (from
    :func:`probe`) sets the stdio server's name/version/instructions.
    """

    def __init__(
        self,
        url: str,
        owner: OwnerInfo | None = None,
        lock_dir: str | os.PathLike[str] | None = None,
        info: UpstreamInfo | None = None,
        *,
        follow_owner: bool = True,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        tool_timeout_s: float = TOOL_CALL_TIMEOUT_S,
    ) -> None:
        self.url = check_owner_url(url)
        self.owner = owner
        self.lock_dir = Path(lock_dir) if lock_dir is not None else None
        self.info = info if info is not None else UpstreamInfo()
        self.follow_owner = follow_owner
        self.request_timeout_s = request_timeout_s
        self.tool_timeout_s = tool_timeout_s

    # -- target resolution ----------------------------------------------------------------
    def _owner_desc(self) -> str:
        return self.owner.describe() if self.owner is not None else f"the owner at {self.url}"

    def _gone_message(self, detail: str) -> str:
        return (
            f"{OWNER_GONE}: cannot reach the EDU09 owner ({self._owner_desc()}): {detail}. If the "
            "GUI was closed or its MCP server switched off, re-enable it, or restart edu09-mcp "
            "to use the device directly."
        )

    async def _current_url(self) -> str:
        """The URL to use for the next request (follows the owner's ``owner.json``).

        Raises ``OwnerUnreachable`` if the device is now owned by a process without an MCP URL
        (or with a refused one), or if ``owner.json`` is gone (after brief retries): the owner
        has exited, and the old URL is not reused (another program may listen on that port now).
        """
        if not self.follow_owner:
            return self.url
        current = await _read_owner_retry(self.lock_dir)
        if current is None:
            raise OwnerUnreachable(self._gone_message("it has exited (no owner.json any more)"))
        if not current.mcp_url:
            same = self.owner is not None and current.pid == self.owner.pid
            if same:
                raise OwnerUnreachable(
                    f"{OWNER_GONE}: the EDU09 owner ({current.describe()}) stopped its MCP "
                    "server; re-enable it (GUI: MCP server toggle), or close that program and "
                    "restart edu09-mcp to use the device directly"
                )
            raise OwnerUnreachable(
                f"DeviceBusy: the EDU09 is now owned by {current.describe()}, which does not "
                "serve MCP; enable its MCP server, or close it and restart edu09-mcp"
            )
        if current.mcp_url != self.url:
            try:
                check_owner_url(current.mcp_url)
            except ValueError as exc:
                raise OwnerUnreachable(f"DeviceBusy: {exc}") from None
            log.info("owner MCP URL changed: %s -> %s", self.url, current.mcp_url)
            self.url = current.mcp_url
        self.owner = current
        return self.url

    async def _forward(
        self, fn: Callable[[Client], Awaitable[_T]], timeout_s: float | None = None
    ) -> _T:
        """Run ``fn`` on a fresh upstream session (whole exchange bounded by ``timeout_s``).
        Upstream MCP errors propagate unchanged; connection failures raise
        ``OwnerUnreachable`` with a ready-to-show message."""
        url = await self._current_url()
        try:
            with anyio.fail_after(timeout_s):
                async with _client(url) as client:
                    return await fn(client)
        except MCPError:
            raise
        except TimeoutError:
            raise OwnerUnreachable(
                self._gone_message(f"no answer within {timeout_s:g} s")
            ) from None
        except Exception as exc:
            if (err := _find_mcp_error(exc)) is not None:
                raise err from None
            raise OwnerUnreachable(self._gone_message(_describe_error(exc))) from None

    async def _forward_request(self, fn: Callable[[Client], Awaitable[_T]]) -> _T:
        """``_forward`` for non-tool requests: failures become MCP errors."""
        try:
            return await self._forward(fn, self.request_timeout_s)
        except OwnerUnreachable as exc:
            raise MCPError(code=types.INTERNAL_ERROR, message=str(exc)) from None

    # -- handlers ------------------------------------------------------------------------
    async def _list_tools(
        self, ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        cursor = params.cursor if params is not None else None
        return await self._forward_request(lambda c: c.list_tools(cursor=cursor))

    async def _call_tool(
        self, ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name, args = params.name, dict(params.arguments or {})
        try:
            return await self._forward(lambda c: c.call_tool(name, args), self.tool_timeout_s)
        except OwnerUnreachable as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], is_error=True
            )

    async def _list_resources(
        self, ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        cursor = params.cursor if params is not None else None
        return await self._forward_request(lambda c: c.list_resources(cursor=cursor))

    async def _list_resource_templates(
        self, ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        cursor = params.cursor if params is not None else None
        return await self._forward_request(lambda c: c.list_resource_templates(cursor=cursor))

    async def _read_resource(
        self, ctx: Any, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        return await self._forward_request(lambda c: c.read_resource(uri))

    def build_server(self) -> Server[Any]:
        """The lowlevel stdio-side server (also usable in-process with ``mcp.Client``)."""
        return Server(
            self.info.name,
            version=self.info.version,
            instructions=self.info.instructions,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_resource_templates,
            on_read_resource=self._read_resource,
        )

    async def run_stdio_async(self) -> None:
        from mcp.server.stdio import stdio_server

        server = self.build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    def run_stdio(self) -> None:
        """Serve over stdio until the client closes stdin (blocks)."""
        anyio.run(self.run_stdio_async)

    def __repr__(self) -> str:
        return f"Bridge({self.url!r}, owner={self.owner!r})"
