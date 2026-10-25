"""``edu09-gui``: QApplication bootstrap. Owns the Scope and the optional in-process MCP server.

Usage::

    edu09-gui                                  # first attached EDU09
    edu09-gui --simulate "square:1khz:4vpp"    # real-time simulator, no hardware
    edu09-gui --mcp-http [--port 8765]         # also serve MCP at http://127.0.0.1:8765/mcp
    edu09-gui --record usb.jsonl               # append every USB report to usb.jsonl (debug)
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from edu09.device.scope import Scope
from edu09.errors import DeviceAccessError, DeviceBusy, DeviceNotFound, Edu09Error
from edu09.gui.main_window import DEFAULT_MCP_HOST, DEFAULT_MCP_PORT, MainWindow
from edu09.gui.qt_bridge import ScopeQtAdapter

__all__ = [
    "build_parser",
    "describe_busy",
    "main",
    "make_transport",
    "qt_platform_problem",
    "xcb_cursor_available",
]

log = logging.getLogger(__name__)

SKIP_QT_CHECK_ENV = "EDU09_SKIP_QT_CHECK"
"""Set to 1 to skip the Linux xcb-cursor pre-flight check (if it misdetects the library)."""
XCB_CURSOR_SONAME = "libxcb-cursor.so.0"
EXIT_QT_PLATFORM = 3


def _dlopen_ok(name: str) -> bool:
    try:
        ctypes.CDLL(name)
    except OSError:
        return False
    return True


def xcb_cursor_available(
    find_library: Callable[[str], str | None] | None = None,
    dlopen: Callable[[str], bool] | None = None,
) -> bool:
    """True if ``libxcb-cursor`` can be loaded (Qt >= 6.5's xcb plugin needs it).

    ``ctypes.util.find_library`` first; it misses libraries outside the ldconfig cache, so a
    ``dlopen`` of the soname is the fallback."""
    find = ctypes.util.find_library if find_library is None else find_library
    if find("xcb-cursor") is not None:
        return True
    return (_dlopen_ok if dlopen is None else dlopen)(XCB_CURSOR_SONAME)


def _qt_platform(env: Mapping[str, str]) -> str | None:
    """The platform plugin Qt will most likely load: ``"xcb"``, another name, or None (none
    usable, e.g. no display at all: not a missing-library problem).

    ``QT_QPA_PLATFORM`` may be a ``;``-separated fallback list (``"wayland;xcb"``; entries may
    carry ``:options``): Qt tries the entries in order, so ``xcb`` only matters when the entries
    before it fail. A ``wayland*`` entry is taken as usable only with ``WAYLAND_DISPLAY`` set
    (Wayland session variables without a Wayland display, e.g. an X-only login of a Wayland
    desktop or ``ssh -X``, leave Qt on xcb); any other entry (offscreen, eglfs, ...) is taken
    as usable. Unset: Wayland if ``WAYLAND_DISPLAY`` is set, else xcb if ``DISPLAY`` is set.
    """
    wayland_display = bool(env.get("WAYLAND_DISPLAY"))
    requested = env.get("QT_QPA_PLATFORM", "").strip()
    if requested:
        for entry in requested.split(";"):
            name = entry.split(":")[0].strip().lower()
            if not name:
                continue
            if name.startswith("wayland") and not wayland_display:
                continue  # Qt falls through to the next entry
            return name
        return None
    if wayland_display:
        return "wayland"
    return "xcb" if env.get("DISPLAY") else None


def qt_platform_problem(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    have_xcb_cursor: Callable[[], bool] | None = None,
) -> str | None:
    """Pre-flight check before creating the ``QApplication``.

    On Linux, Qt's xcb (X11) platform plugin aborts the process (core dump) when
    ``libxcb-cursor0`` is missing. Detect that case and return a helpful message instead
    (``None`` = no known problem; nothing changes when the library is present).
    """
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if not plat.startswith("linux") or env.get(SKIP_QT_CHECK_ENV, "") not in ("", "0"):
        return None
    if _qt_platform(env) != "xcb":
        return None
    wayland = bool(env.get("WAYLAND_DISPLAY"))
    if (xcb_cursor_available if have_xcb_cursor is None else have_xcb_cursor)():
        return None
    lines = [
        "edu09-gui cannot start: Qt's X11 (xcb) platform plugin needs the libxcb-cursor "
        "library, which is not installed.",
        "",
        "Install it and start edu09-gui again:",
        "  Debian/Ubuntu:  sudo apt install libxcb-cursor0",
        "  Fedora:         sudo dnf install xcb-util-cursor",
        "  Arch:           sudo pacman -S xcb-util-cursor",
        "  openSUSE:       sudo zypper install libxcb-cursor0",
    ]
    if wayland:
        lines += ["", "Or use Qt's Wayland backend:  QT_QPA_PLATFORM=wayland edu09-gui"]
    lines += ["", f"(Set {SKIP_QT_CHECK_ENV}=1 to skip this check.)"]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edu09-gui", description="Velleman EDU09 oscilloscope GUI")
    p.add_argument(
        "--simulate",
        metavar="SPEC",
        help='use the real-time simulator instead of hardware, e.g. "sine:1khz:2vpp"',
    )
    p.add_argument(
        "--mcp-http",
        action="store_true",
        help=f"start the MCP server (streamable HTTP on {DEFAULT_MCP_HOST}) at launch",
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_MCP_PORT, help="MCP HTTP port (default %(default)s)"
    )
    p.add_argument("--no-run", action="store_true", help="do not start acquiring at launch")
    p.add_argument(
        "--record",
        metavar="FILE",
        help="append every USB report exchanged with the scope to FILE (JSONL; for debugging, "
        "replayable with ReplayTransport)",
    )
    p.add_argument("--quit-after", type=float, metavar="S", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def make_transport(simulate: str | None, record: str | None = None) -> Any:
    """``None`` -> open the first attached device; else a real-time ``SimTransport``.
    ``record``: wrap the transport (``HidTransport`` for hardware) in a ``RecordingTransport``
    appending to that JSONL file.

    (The only place the GUI touches the transport layer.)
    """
    inner: Any
    if simulate is None:
        if record is None:
            return None
        from edu09.transport.hid_transport import HidTransport

        inner = HidTransport()
    else:
        from edu09.transport.sim import SignalSpec, SimTransport

        inner = SimTransport(SignalSpec.parse(simulate))
    if record is None:
        return inner
    from edu09.transport.replay import RecordingTransport

    return RecordingTransport(inner, record)


def describe_busy(exc: DeviceBusy) -> str:
    owner = exc.owner
    if owner is None:
        return f"The EDU09 is in use by another program.\n\n{exc}"
    lines = [
        "The EDU09 is already in use by another program:",
        f"  kind: {getattr(owner, 'kind', '?')}",
        f"  pid: {getattr(owner, 'pid', '?')}",
    ]
    if getattr(owner, "started", None):
        lines.append(f"  since: {owner.started}")
    url = getattr(owner, "mcp_url", None)
    if url:
        lines.append(f"\nIt serves MCP at {url}; AI agents can use it through that server.")
    lines.append("\nClose that program and start edu09-gui again.")
    return "\n".join(lines)


def _qapp_instance() -> Any:
    return QApplication.instance()


def _open_scope(args: argparse.Namespace) -> Scope | None:
    """Open the device, asking to retry on not-found errors. ``None`` = give up."""
    while True:
        try:
            return Scope.open(make_transport(args.simulate, args.record), owner_kind="gui")
        except DeviceBusy as exc:
            QMessageBox.critical(None, "EDU09 busy", describe_busy(exc))
            return None
        except (DeviceNotFound, DeviceAccessError, TimeoutError, Edu09Error) as exc:
            hint = ""
            if isinstance(exc, DeviceAccessError) and sys.platform.startswith("linux"):
                hint = "\n\nOn Linux install packaging/linux/99-velleman-edu09.rules (udev)."
            answer = QMessageBox.warning(
                None,
                "EDU09 not available",
                f"{type(exc).__name__}: {exc}{hint}\n\nPlug in the oscilloscope and retry, "
                'or start with --simulate "sine:1khz:2vpp".',
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Retry,
            )
            if answer != QMessageBox.StandardButton.Retry:
                return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.simulate is not None:
        try:
            make_transport(args.simulate)
        except ValueError as exc:
            parser.error(f"--simulate: {exc}")
    if args.record is not None:
        from edu09.transport.replay import check_record_path

        try:
            check_record_path(args.record)
        except OSError as exc:
            parser.error(f"--record: cannot write {args.record}: {exc}")
        log.info("recording USB reports to %s", args.record)

    existing = _qapp_instance()
    if existing is None:
        problem = qt_platform_problem()
        if problem is not None:
            print(problem, file=sys.stderr)
            return EXIT_QT_PLATFORM

    import pyqtgraph as pg

    pg.setConfigOptions(antialias=True)
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv[:1])
    app.setApplicationName("edu09-gui")
    app.setOrganizationName("edu09")

    scope = _open_scope(args)
    if scope is None:
        return 2
    adapter: ScopeQtAdapter | None = None
    win: MainWindow | None = None
    try:
        adapter = ScopeQtAdapter(scope)
        win = MainWindow(adapter, mcp_port=args.port)
        win.show()
        if not args.no_run:
            adapter.run()
        if args.mcp_http:
            win.set_mcp_enabled(True)
        # let Ctrl-C in the terminal close the window (Python handles signals between Qt events)
        window = win
        signal.signal(signal.SIGINT, lambda *_a: window.close())
        tick = QTimer()
        tick.timeout.connect(lambda: None)
        tick.start(200)
        if args.quit_after is not None:
            QTimer.singleShot(int(args.quit_after * 1000), win.close)
        rc = app.exec()
    finally:
        # never leaves the device open, also if building the window failed
        try:
            if win is not None:
                win.shutdown()
            elif adapter is not None:
                adapter.shutdown(wait=False)
        finally:
            scope.close()
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
