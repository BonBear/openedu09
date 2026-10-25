"""``edu09-mcp``: run the EDU09 MCP server (stdio by default, ``--http`` for streamable HTTP).

Examples::

    edu09-mcp                                   # stdio, real hardware
    edu09-mcp --simulate "sine:1khz:2vpp"       # stdio, simulated scope
    edu09-mcp --http --port 8765                # http://127.0.0.1:8765/mcp
    edu09-mcp --record usb.jsonl                # append every USB report to usb.jsonl (debug)
    edu09-mcp --simulate dc:0v --sim-unit unit1 --allow-calibration   # try the wizard tools

Bridge: if the device is owned by another process (e.g. the GUI) that serves MCP over HTTP
(its URL is in ``owner.json``), stdio ``edu09-mcp`` forwards every request to it instead of
failing (``bridge.py``; ``--no-bridge`` disables this). Only loopback URLs are followed.

``--allowed-host`` / ``--allow-remote`` without ``--http`` only print a warning (stdio has no
Host header to check; not fatal, so a slightly wrong agent config still starts).

stdout belongs to the MCP protocol in stdio mode: all diagnostics go to stderr.
Exit codes: 0 ok, 1 device error, 2 usage error / device busy.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from edu09.device.lock import OwnerInfo
from edu09.errors import DeviceAccessError, DeviceBusy, DeviceNotFound, Edu09Error

if TYPE_CHECKING:
    from edu09.device.scope import Scope
    from edu09.mcp_server.bridge import Bridge
    from edu09.transport.base import Transport

EXIT_DEVICE_ERROR = 1
EXIT_BUSY = 2
OPEN_ATTEMPTS = 3
"""stdio: ``Scope.open`` attempts when the owner vanishes between ``DeviceBusy`` and the
bridge probe (it released the lock, so the next open normally succeeds)."""
OWNER_URL_WAIT_S = 1.0
"""How long to wait for the owner to publish its MCP URL (it may still be starting up)."""

log = logging.getLogger("edu09.mcp")


def build_parser() -> argparse.ArgumentParser:
    from edu09.mcp_server.server import DEFAULT_HOST, DEFAULT_PORT

    p = argparse.ArgumentParser(
        prog="edu09-mcp",
        description="MCP server for the Velleman EDU09 USB oscilloscope.",
    )
    p.add_argument(
        "--http",
        action="store_true",
        help=f"serve streamable HTTP at http://HOST:PORT/mcp instead of stdio "
        f"(default {DEFAULT_HOST}:{DEFAULT_PORT})",
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="HTTP bind address (default: %(default)s)")
    p.add_argument(
        "--port", type=_port, default=DEFAULT_PORT, help="HTTP port (default: %(default)s)"
    )
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback --host (no authentication: anyone on the network can "
        "control the scope)",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="NAME[:PORT]",
        help="with --http: also accept this Host header (repeatable), e.g. the machine's LAN "
        "name or IP when using --allow-remote; the port defaults to --port",
    )
    p.add_argument(
        "--no-bridge",
        action="store_true",
        help="stdio: if another process owns the device, exit instead of forwarding to its "
        "MCP HTTP server",
    )
    p.add_argument(
        "--simulate",
        metavar="SPEC",
        nargs="?",
        const="sine:1khz:2vpp",
        default=None,
        help="use a simulated scope; SPEC like 'sine:1khz:2vpp', "
        "'square:50hz:5vpp:duty=25%%:offset=2.5v', 'dc:1.5v', 'noise:100mvrms' "
        "(default when given without SPEC: sine:1khz:2vpp)",
    )
    p.add_argument(
        "--allow-calibration",
        action="store_true",
        help="register the 'calibrate' tool and the calibration wizard tools "
        "('calibration_*'; saving still needs confirm=true, readings come from the human)",
    )
    p.add_argument(
        "--sim-unit",
        choices=["ideal", "unit1"],
        default=None,
        help="with --simulate: physical front end of the simulated scope ('unit1' = the gain "
        "errors and negative nonlinearity measured on one real unit; default: ideal)",
    )
    p.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="directory for export_csv (default: the user data dir's 'exports')",
    )
    p.add_argument(
        "--record",
        metavar="FILE",
        type=Path,
        default=None,
        help="append every USB report exchanged with the scope to FILE (JSONL; for debugging)",
    )
    p.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stderr log level (default: %(default)s)",
    )
    return p


def _busy_message(exc: DeviceBusy, http: bool, owner: OwnerInfo | None = None) -> str:
    owner = owner if owner is not None else exc.owner
    lines = [f"edu09-mcp: {exc}"]
    url = getattr(owner, "mcp_url", None)
    if url:
        lines.append(
            f"The owning process serves MCP over streamable HTTP at {url}. Point your MCP client "
            "at that URL directly"
            + (" (or run edu09-mcp without --no-bridge to forward to it)." if not http else ".")
        )
    else:
        lines.append(
            "Close the other program (or enable its MCP HTTP server, e.g. the GUI's MCP toggle) "
            "and try again."
        )
    return "\n".join(lines)


def _prepare_bridge(exc: DeviceBusy) -> tuple[int | None, Bridge | None]:
    """Resolve the owner's MCP endpoint for the stdio bridge.

    Returns ``(None, bridge)`` to run the bridge, ``(None, None)`` if the owner seems to have
    gone (caller retries ``Scope.open``), or ``(exit_code, None)`` after printing why not.
    """
    from edu09.mcp_server.bridge import (
        Bridge,
        OwnerUnreachable,
        check_owner_url,
        probe,
        resolve_owner,
    )

    owner = resolve_owner(exc.owner, wait_s=OWNER_URL_WAIT_S)
    if owner is None or not owner.mcp_url:
        if owner is None and exc.owner is None:
            return None, None  # no owner.json at all: the owner may just have exited
        print(_busy_message(exc, False, owner), file=sys.stderr)
        return EXIT_BUSY, None
    try:
        url = check_owner_url(owner.mcp_url)
    except ValueError as err:
        print(f"edu09-mcp: {exc}\nedu09-mcp: {err}", file=sys.stderr)
        return EXIT_BUSY, None
    try:
        info = probe(url)
    except OwnerUnreachable as err:
        log.info("owner MCP endpoint unreachable: %s", err)
        return None, None
    log.warning("device owned by %s: bridging stdio to %s", owner.describe(), url)
    return None, Bridge(url, owner, info=info)


def _ignored_in_bridge(args: argparse.Namespace) -> None:
    ignored = [
        flag
        for flag, on in (
            ("--allow-calibration", args.allow_calibration),
            ("--export-dir", args.export_dir is not None),
            ("--record", args.record is not None),
        )
        if on
    ]
    if ignored:
        log.warning("bridge mode: %s ignored (the owning process decides)", ", ".join(ignored))


def _port(text: str) -> int:
    """argparse type for ``--port``: 1..65535. Port 0 is refused because the published MCP URL
    (owner.json, used by the stdio bridge) must name the real port."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1..65535, got {value}")
    return value


def _recorded(transport: Transport | None, record: Path | None) -> Transport | None:
    """``transport`` (None = the first attached device, ``HidTransport``) wrapped in a
    ``RecordingTransport`` if ``--record`` was given. The wrapper keeps the inner ``info``, so
    hardware is still detected as such (owner lock, calibration file)."""
    if record is None:
        return transport
    from edu09.transport.replay import RecordingTransport

    if transport is None:
        from edu09.transport.hid_transport import HidTransport

        transport = HidTransport()
    return RecordingTransport(transport, record)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from edu09.device.scope import Scope
    from edu09.mcp_server.server import (
        ServerOptions,
        check_host,
        client_url,
        mcp_url,
        run_http,
        run_stdio,
        transport_security,
    )

    if not args.http:
        unused = [
            flag
            for flag, on in (
                ("--allowed-host", bool(args.allowed_host)),
                ("--allow-remote", args.allow_remote),
            )
            if on
        ]
        if unused:
            # not fatal (stdio has no Host header to check), but likely a mistake in the
            # client config: say so on stderr
            print(
                f"edu09-mcp: warning: {', '.join(unused)} only applies with --http; ignored",
                file=sys.stderr,
            )
    if args.http:
        try:
            check_host(args.host, args.allow_remote)
        except ValueError as exc:
            print(f"edu09-mcp: {exc}; pass --allow-remote to override", file=sys.stderr)
            return EXIT_BUSY
        if not 0 < args.port <= 65535:
            print(f"edu09-mcp: invalid --port {args.port}", file=sys.stderr)
            return EXIT_BUSY
        try:
            transport_security(args.host, args.port, args.allowed_host)
        except ValueError as exc:
            print(f"edu09-mcp: --allowed-host: {exc}", file=sys.stderr)
            return EXIT_BUSY

    opts = ServerOptions(
        allow_calibration=args.allow_calibration,
        export_dir=args.export_dir,
        owner_kind="mcp",
        log_level=args.log_level,
    )
    if args.simulate is not None:
        from edu09.transport.sim import SignalSpec, SimTransport

        try:
            spec = SignalSpec.parse(args.simulate)
        except ValueError as exc:
            print(f"edu09-mcp: --simulate: {exc}", file=sys.stderr)
            return EXIT_BUSY
    if args.sim_unit is not None and args.simulate is None:
        print("edu09-mcp: --sim-unit needs --simulate", file=sys.stderr)
        return EXIT_BUSY
    if args.record is not None:
        from edu09.transport.replay import check_record_path

        try:
            check_record_path(args.record)
        except OSError as exc:
            print(f"edu09-mcp: --record: cannot write {args.record}: {exc}", file=sys.stderr)
            return EXIT_BUSY
    scope: Scope | None = None
    try:
        for attempt in range(OPEN_ATTEMPTS):
            try:
                if args.simulate is not None:
                    sim = SimTransport(spec, seed=None)
                    if args.sim_unit is not None:
                        from edu09.transport.sim import PHYSICAL_UNITS

                        sim.device.calibration = PHYSICAL_UNITS[args.sim_unit]
                    scope = Scope.open(_recorded(sim, args.record))
                else:
                    scope = Scope.open(
                        _recorded(None, args.record),
                        owner_kind="mcp",
                        mcp_url=client_url(args.host, args.port) if args.http else None,
                    )
                break
            except DeviceBusy as exc:
                if args.http or args.no_bridge:
                    print(_busy_message(exc, args.http), file=sys.stderr)
                    return EXIT_BUSY
                code, bridge = _prepare_bridge(exc)
                if code is not None:
                    return code
                if bridge is not None:
                    _ignored_in_bridge(args)
                    with contextlib.suppress(KeyboardInterrupt):
                        bridge.run_stdio()
                    return 0
                if attempt == OPEN_ATTEMPTS - 1:
                    owner = exc.owner
                    if owner is None or not owner.mcp_url:
                        print(_busy_message(exc, False), file=sys.stderr)
                    else:
                        print(
                            f"edu09-mcp: {exc}\nedu09-mcp: the owner's MCP server at "
                            f"{owner.mcp_url} does not answer; check that program or try again.",
                            file=sys.stderr,
                        )
                    return EXIT_BUSY
                log.info("owner seems to have gone; retrying to open the device")
    except (DeviceNotFound, DeviceAccessError, Edu09Error, TimeoutError) as exc:
        print(f"edu09-mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_DEVICE_ERROR
    assert scope is not None

    try:
        if args.http:
            log.warning("serving MCP at %s (Ctrl-C to stop)", mcp_url(args.host, args.port))
            run_http(
                scope,
                opts,
                args.host,
                args.port,
                allow_remote=args.allow_remote,
                allowed_hosts=args.allowed_host,
            )
        else:
            run_stdio(scope, opts)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"edu09-mcp: {exc}", file=sys.stderr)
        return EXIT_DEVICE_ERROR
    finally:
        scope.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
