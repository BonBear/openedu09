"""Build and run the edu09 MCP server (architecture.md 3.4 and 6).

Public API (stable; used by the CLI, the GUI (T11) and the bridge (T12))::

    build_server(scope, opts=None) -> FastMCP
    run_stdio(scope, opts=None) -> None                         # blocks
    run_http(scope, opts=None, host="127.0.0.1", port=8765, *, allow_remote=False,
             allowed_hosts=()) -> None  # blocks
    start_http_in_thread(scope, opts=None, host="127.0.0.1", port=8765, *, allow_remote=False,
                         allowed_hosts=(), startup_timeout_s=10.0) -> HttpServerHandle
    transport_security(host, port, allowed_hosts=()) -> TransportSecuritySettings
    HttpServerHandle.url / .host / .port / .stop(timeout_s=5.0) / .wait(timeout_s) / .running
    mcp_url(host, port) -> str       # http://host:port/mcp
    client_url(host, port) -> str    # like mcp_url, wildcard binds -> loopback (owner.json)

``FastMCP`` is the SDK's server class (``mcp.server.mcpserver.MCPServer`` in mcp 2.x, which
renamed v1's ``FastMCP``). The HTTP server is uvicorn running FastMCP's streamable-HTTP app at
``http://<host>:<port>/mcp``; while it serves, its URL is published in ``owner.json`` via
``scope.set_mcp_url`` so a stdio ``edu09-mcp`` can bridge to it. Every HTTP start builds a fresh
server (the SDK's session manager can only run once per instance), so the GUI can toggle it.

Nothing here writes to stdout: in stdio mode stdout carries the MCP protocol.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from edu09 import __version__
from edu09.mcp_server.netaddr import ip_literal, is_loopback_ip, is_unspecified_ip

if TYPE_CHECKING:
    from edu09.device.scope import Scope

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Edu09MCP",
    "MCP_PATH",
    "SERVER_NAME",
    "FastMCP",
    "HttpServerHandle",
    "ServerOptions",
    "build_server",
    "check_host",
    "client_url",
    "mcp_url",
    "run_http",
    "run_stdio",
    "start_http_in_thread",
    "transport_security",
]

log = logging.getLogger(__name__)

SERVER_NAME: Final = "edu09"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8765
MCP_PATH: Final = "/mcp"

_INSTRUCTIONS: Final = (
    "Velleman EDU09 single-channel USB oscilloscope (8-bit, 1024 samples per frame). "
    "Typical flow: get_status / get_capabilities, configure (volts_per_div, timebase, trigger), "
    "acquire (returns stats + a decimated preview), then measure / get_waveform / export_csv on "
    "the returned waveform_id. auto_set picks settings for an unknown signal. Full-resolution "
    "data: resources edu09://waveforms/{id} (JSON) and edu09://waveforms/{id}.csv. Errors read "
    "'<ErrorClass>: <hint>'."
)


@dataclass(frozen=True)
class ServerOptions:
    """Server behaviour switches.

    * ``allow_calibration``: register the ``calibrate`` tool (CLI ``--allow-calibration``).
    * ``export_dir``: where ``export_csv`` writes (default ``storage.default_export_dir()``).
    * ``owner_kind``: reported in ``get_status.owner`` when the scope holds no owner lock
      (simulator); "mcp" for ``edu09-mcp``, "gui" for the GUI.
    * ``name`` / ``log_level``: MCP server name and the SDK's log level (stderr only).
    """

    allow_calibration: bool = False
    export_dir: Path | None = None
    owner_kind: str = "mcp"
    name: str = SERVER_NAME
    log_level: str = "WARNING"


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
    return "; ".join(parts)


class Edu09MCP(FastMCP):
    """FastMCP whose tool errors read ``"<ErrorClass>: <hint>"`` (architecture.md 6).

    The SDK prefixes deliberate tool errors with ``Error executing tool <name>:``; this strips
    it for :class:`~edu09.mcp_server.tools.ToolFailure` and condenses argument validation
    errors to ``"ValidationError: <param>: <problem>"``. Crashes keep the SDK's generic text.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        from edu09.mcp_server.tools import ToolFailure

        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            raise
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ToolFailure):
                raise ToolFailure(str(cause)) from cause
            if isinstance(cause, ValidationError):
                raise ToolFailure(f"ValidationError: {_validation_summary(cause)}") from cause
            if cause is None and str(exc).startswith("Unknown tool"):
                raise ToolFailure(f"NotFound: {exc}") from exc
            raise


def build_server(scope: Scope, opts: ServerOptions | None = None) -> FastMCP:
    """A FastMCP server exposing ``scope`` (tools + resources from :mod:`.tools`)."""
    from edu09.mcp_server.tools import register_tools

    opts = opts if opts is not None else ServerOptions()
    level = opts.log_level.upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError(f"invalid log_level {opts.log_level!r}")
    mcp: FastMCP = Edu09MCP(
        opts.name,
        instructions=_INSTRUCTIONS,
        version=__version__,
        log_level=level,  # type: ignore[arg-type]
    )
    register_tools(mcp, scope, opts)
    return mcp


def run_stdio(scope: Scope, opts: ServerOptions | None = None) -> None:
    """Serve over stdio until the client closes stdin (blocks)."""
    build_server(scope, opts).run("stdio")


# --------------------------------------------------------------------------------------------
# streamable HTTP
# --------------------------------------------------------------------------------------------


def mcp_url(host: str, port: int) -> str:
    """``http://host:port/mcp`` (IPv6 literals bracketed)."""
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{h}:{port}{MCP_PATH}"


def client_url(host: str, port: int) -> str:
    """The URL a local client uses for a server bound to ``host``: like :func:`mcp_url`, but a
    wildcard bind (``0.0.0.0`` / ``::``) maps to ``127.0.0.1`` / ``[::1]`` (a wildcard is no
    address to connect to, and the bridge only follows loopback URLs). Published in
    ``owner.json``."""
    ip = ip_literal(host)
    if ip is not None and is_unspecified_ip(host):
        return mcp_url("127.0.0.1" if ip.version == 4 else "::1", port)
    return mcp_url(host, port)


def _is_loopback(host: str) -> bool:
    """``localhost``, 127.0.0.0/8 or exactly ``::1`` - deterministic on every Python version
    (IPv4-mapped/compatible and other embedded forms are NOT loopback; see ``netaddr``)."""
    return host == "localhost" or is_loopback_ip(host)


def check_host(host: str, allow_remote: bool) -> None:
    if _is_loopback(host):
        return
    if not allow_remote:
        raise ValueError(
            f"refusing to serve MCP on non-loopback host {host!r} without allow_remote "
            "(the server has no authentication; use 127.0.0.1)"
        )
    log.warning(
        "\n"
        "################################################################################\n"
        "# WARNING: serving MCP on %s - reachable from the network WITHOUT AUTHENTICATION.\n"
        "# Anyone who can connect can control the oscilloscope. Only Host headers listed\n"
        "# by --allowed-host (plus the bind address) are accepted.\n"
        "################################################################################",
        host,
    )


_LOOPBACK_HOST_PATTERNS: Final = ("127.0.0.1", "localhost", "[::1]")


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _host_entry(entry: str, port: str) -> str:
    """Normalise an ``--allowed-host`` value to a Host-header pattern ``host:port``.

    ``name`` / ``1.2.3.4`` / ``fe80::1`` / ``[fe80::1]`` get the server port appended;
    ``name:8080`` / ``[::1]:8080`` / ``name:*`` (any port) are kept.
    """
    e = entry.strip()
    if not e or any(c in e for c in "/@ \t?#") or e.count("[") > 1:
        raise ValueError(f"invalid allowed host {entry!r} (expected NAME, NAME:PORT or IP)")
    if e.startswith("["):
        close = e.find("]")
        if close < 0:
            raise ValueError(f"invalid allowed host {entry!r} (unclosed '[')")
        rest = e[close + 1 :]
        if not rest:
            return f"{e}:{port}"
        p = rest[1:] if rest.startswith(":") else None
    elif e.count(":") == 1:
        p = e.split(":", 1)[1]
    elif ":" in e:  # bare IPv6 literal
        return f"[{e}]:{port}"
    else:
        return f"{e}:{port}"
    if p is None or not (p == "*" or (p.isdigit() and 0 < int(p) <= 65535)):
        raise ValueError(f"invalid port in allowed host {entry!r}")
    return e


def transport_security(
    host: str, port: int, allowed_hosts: Sequence[str] = ()
) -> TransportSecuritySettings:
    """Explicit DNS-rebinding protection (Host/Origin checks) for the HTTP server.

    The SDK only enables it automatically for a few loopback bind addresses and disables it
    for anything else, so it is always passed explicitly here:

    * loopback bind: ``127.0.0.1:*``, ``localhost:*``, ``[::1]:*`` (+ the bind host, any port);
    * non-loopback bind (``allow_remote``): the bind host with the server port (unless it is a
      wildcard address like ``0.0.0.0``, which never appears in a Host header) and the
      loopback names with the server port;
    * plus every ``allowed_hosts`` entry (see :func:`_host_entry`).

    Allowed origins are ``http://`` + each allowed host. ``port=0`` (pick a free port) uses
    ``:*`` because the port is unknown until bind.
    """
    port_s = str(port) if port else "*"
    own: str | None
    if _is_loopback(host):
        hosts = [f"{h}:*" for h in _LOOPBACK_HOST_PATTERNS]
        own = f"{_bracket(host)}:*"
    else:
        hosts = [f"{h}:{port_s}" for h in _LOOPBACK_HOST_PATTERNS]
        own = None if is_unspecified_ip(host) else f"{_bracket(host)}:{port_s}"
    if own is not None and own not in hosts:
        hosts.append(own)
    for entry in allowed_hosts:
        h = _host_entry(entry, port_s)
        if h not in hosts:
            hosts.append(h)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


class HttpServerHandle:
    """A streamable-HTTP MCP server running in a background thread.

    ``url`` is the client URL (``http://127.0.0.1:8765/mcp``; :func:`client_url`, so a wildcard
    bind publishes a loopback URL). ``stop()`` shuts uvicorn down,
    joins the thread and clears the URL from ``owner.json``; it is idempotent and safe to call
    from any thread except the server thread itself.
    """

    def __init__(
        self,
        server: Any,
        thread: threading.Thread,
        scope: Scope,
        host: str,
        port: int,
        errors: list[BaseException],
    ) -> None:
        self._server = server
        self.thread = thread
        self._scope = scope
        self.host = host
        self.port = port
        self.url = client_url(host, port)
        self._errors = errors
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def wait(self, timeout_s: float | None = None) -> bool:
        """Block until the server thread exits; True if it did."""
        self.thread.join(timeout_s)
        return not self.thread.is_alive()

    def stop(self, timeout_s: float = 5.0) -> None:
        with self._stop_lock:
            first = not self._stopped
            self._stopped = True
        if first:
            try:
                self._scope.set_mcp_url(None)
            except Exception:
                log.exception("could not clear the MCP URL in owner.json")
        self._server.should_exit = True
        self.thread.join(timeout_s / 2)
        if self.thread.is_alive():
            self._server.force_exit = True
            self.thread.join(timeout_s / 2)
        if self.thread.is_alive():
            log.warning("MCP HTTP server thread did not stop within %.1f s", timeout_s)

    def __enter__(self) -> HttpServerHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return f"HttpServerHandle({self.url!r}, running={self.running})"


def start_http_in_thread(
    scope: Scope,
    opts: ServerOptions | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
    allowed_hosts: Sequence[str] = (),
    startup_timeout_s: float = 10.0,
) -> HttpServerHandle:
    """Start the MCP server over streamable HTTP in a daemon thread and return once it listens.

    ``port=0`` picks a free port (see ``handle.port`` / ``handle.url``). Publishes the URL with
    ``scope.set_mcp_url``. Host/Origin headers are always checked (:func:`transport_security`;
    ``allowed_hosts`` adds names clients may use, e.g. the machine's LAN name with
    ``allow_remote``). Raises ``OSError`` if the server cannot start (e.g. port in use) and
    ``ValueError`` for a non-loopback host without ``allow_remote`` or a bad allowed host.
    """
    import uvicorn

    check_host(host, allow_remote)
    if not (isinstance(port, int) and 0 <= port <= 65535):
        raise ValueError(f"invalid port {port!r}")
    security = transport_security(host, port, allowed_hosts)
    if not _is_loopback(host):
        log.warning("accepted Host headers: %s", ", ".join(security.allowed_hosts))
    mcp = build_server(scope, opts)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH, host=host, transport_security=security
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # never reconfigure the host process' logging (GUI) or touch stdout
        log_level="warning",
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            server.run()
        except BaseException as exc:  # SystemExit from a failed bind, ...
            errors.append(exc)

    thread = threading.Thread(target=_run, name="edu09-mcp-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout_s
    while not server.started:
        if not thread.is_alive():
            err = errors[0] if errors else None
            raise OSError(
                f"could not start the MCP HTTP server on {host}:{port}"
                + (f" ({type(err).__name__}: {err})" if err is not None else "")
            )
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(2.0)
            raise OSError(f"MCP HTTP server on {host}:{port} did not start in time")
        time.sleep(0.01)
    actual_port = port
    with contextlib.suppress(AttributeError, IndexError, OSError):  # uvicorn internals
        actual_port = int(server.servers[0].sockets[0].getsockname()[1])
    handle = HttpServerHandle(server, thread, scope, host, actual_port, errors)
    scope.set_mcp_url(handle.url)
    log.info("MCP server listening on %s", handle.url)
    return handle


def run_http(
    scope: Scope,
    opts: ServerOptions | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
    allowed_hosts: Sequence[str] = (),
) -> None:
    """Serve over streamable HTTP until Ctrl-C (blocks; same code path as the GUI thread)."""
    handle = start_http_in_thread(
        scope, opts, host, port, allow_remote=allow_remote, allowed_hosts=allowed_hosts
    )
    try:
        while not handle.wait(0.5):
            pass
    except KeyboardInterrupt:
        log.info("stopping MCP HTTP server")
    finally:
        handle.stop()
