"""EDU09 hardware probe: talk to the scope with raw reports and log everything as JSONL.

Uses only ``edu09.protocol`` and ``edu09.transport`` (never ``edu09.device``), so it keeps working
while the higher layers are broken. Read-only by default (architecture.md 5, 7.1):

* ``read`` class opcodes (0x0F version) are always allowed;
* ``acquire`` class opcodes (0x08 0x09 0x0A 0x0B 0x0D 0x0E) need ``--allow-acquire``;
* anything else (0x0C, 0x14, unknown) needs ``--allow-unknown`` AND a typed confirmation.
  A payload is also "unknown" when it is a 0x0E config that fails strict validation, or any
  other opcode carrying non-zero bytes after byte 0 (see ``payload_problem``).

Every OUT/IN report is appended to ``captures/<timestamp>-<cmd>.jsonl`` (format of
``edu09.transport.replay``; replayable with ``ReplayTransport`` and ``edu09-probe replay FILE``).

Installed as ``edu09-probe`` (``edu09.probe:main``); from a source checkout
``python tools/probe.py`` is a thin shim around the same ``main``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from edu09.errors import Edu09Error, UnsafeOperation
from edu09.model import Timebase, VoltsPerDiv
from edu09.protocol import (
    FrameAssembler,
    HwConfig,
    decode_config,
    encode_arm,
    encode_config,
    encode_readout,
    encode_reset,
    encode_stop,
    encode_version_query,
    is_waiting_report,
    parse_status,
    parse_version,
    tables,
)
from edu09.transport import HidTransport, Transport, list_devices
from edu09.transport.replay import load_records

REPORT_SIZE = tables.REPORT_SIZE
CONFIRM_PHRASE = "I UNDERSTAND"
EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_INTERRUPTED = 0, 1, 2, 130

Printer = Callable[[str], None]
InputFn = Callable[[str], str]


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def parse_hex(parts: Sequence[str]) -> bytes:
    """``["0E", "80,07"]``, ``["0x0e8007"]`` ... -> bytes. Raises ``ValueError``."""
    text = " ".join(parts)
    text = re.sub(r"0x", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s,:;-]+", "", text)
    if not text:
        raise ValueError("no hex bytes given")
    if len(text) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", text):
        raise ValueError(f"not a valid hex byte string: {' '.join(parts)!r}")
    return bytes.fromhex(text)


def pad_report(data: bytes) -> bytes:
    """Zero-pad a payload (no report-ID byte) to 64 bytes. ``ValueError`` if longer."""
    if not data:
        raise ValueError("empty payload")
    if len(data) > REPORT_SIZE:
        raise ValueError(f"payload is {len(data)} bytes; max {REPORT_SIZE} (no report-ID byte)")
    return data + bytes(REPORT_SIZE - len(data))


def fmt_hex(data: bytes) -> str:
    """Hex with the trailing zero padding collapsed."""
    end = len(data)
    while end > 1 and data[end - 1] == 0:
        end -= 1
    s = data[:end].hex(" ")
    return s + (f" (+{len(data) - end} x00)" if end < len(data) else "")


def classify(op: int) -> str:
    return str(tables.OPCODE_CLASS.get(op, "unknown"))


def config_problem(payload: bytes) -> str | None:
    """Strict check of a (padded) 0x0E config payload. Returns a reason, or None if it is sane."""
    if payload[: len(tables.CONFIG_HEADER)] != tables.CONFIG_HEADER:
        return f"config header is {payload[:3].hex(' ')}, expected {tables.CONFIG_HEADER.hex(' ')}"
    if any(payload[tables.CONFIG_LENGTH :]):
        return f"non-zero bytes after config byte {tables.CONFIG_LENGTH - 1}"
    gain1 = payload[tables.CFG_IDX_CH1]
    if gain1 & ~tables.CH1_AC_BIT not in tables.GAIN_CODES_CH1.values():
        return f"CH1 gain byte 0x{gain1:02X} is not a Table A code"
    gain2 = payload[tables.CFG_IDX_CH2]
    ch2_ok = {
        g | extra
        for g in tables.GAIN_CODES_CH2.values()
        for extra in (0, tables.CH2_DC_BIT, tables.CH2_GND_BIT)
    } | {tables.CH2_NEUTRAL_CFG}
    if gain2 not in ch2_ok:
        return f"CH2 gain byte 0x{gain2:02X} is not a known CH2 code"
    tb = payload[tables.CFG_IDX_TB]
    if tb not in {e.hw_code for e in tables.TIMEBASES.values()}:
        return f"timebase code 0x{tb:02X} is not in Table B"
    flags = payload[tables.CFG_IDX_FLAGS]
    if flags & ~tables.FLAGS_VALID_MASK:
        return f"flags byte 0x{flags:02X} has reserved bits set"
    return None


def payload_problem(payload: bytes) -> str | None:
    """Why a padded payload must be treated as "unknown" class even if its opcode is known."""
    op = payload[0]
    if op == tables.OP_CONFIG:
        return config_problem(payload)
    if classify(op) != "unknown" and any(payload[1:]):
        return f"opcode 0x{op:02X} takes no arguments but has non-zero bytes after byte 0"
    return None


def annotate_in(data: bytes, *, armed: bool = False) -> str:
    """Short human annotation of an IN report.

    An all-zero report is the HW "waiting" status only while armed (after 0x0B, before 0x0A /
    0x08 / 0x09); otherwise it is just labelled as all-zero.
    """
    if is_waiting_report(data):
        return "status: waiting" if armed else "all-zero report"
    st = parse_status(data)
    if st is not None and st.flag == tables.STATUS_COMPLETE:
        return f"status: COMPLETE mode={st.mode}"
    if st is not None:
        return f"status-like (flag=0x{st.flag:02X} mode={st.mode})"
    try:
        return f"ascii-version? {parse_version(data)!r}"
    except Edu09Error:
        return ""


def armed_after(op: int, armed: bool) -> bool:
    """Probe-side view of the device's armed state after the host sent opcode ``op``."""
    if op == tables.OP_ARM:
        return True
    if op in (tables.OP_READOUT, tables.OP_STOP, tables.OP_RESET):
        return False
    return armed


def annotate_out(data: bytes) -> str:
    op = data[0]
    names = {
        tables.OP_STOP: "stop",
        tables.OP_RESET: "reset",
        tables.OP_READOUT: "readout",
        tables.OP_ARM: "arm",
        tables.OP_LIVE: "live (fast status stream)",
        tables.OP_CONFIG: "config",
        tables.OP_VERSION: "version",
        tables.OP_RELEASE: "release",
    }
    return f"{names.get(op, 'UNKNOWN')} [{classify(op)}]"


def build_config(
    vdiv: str, timebase: str, coupling: str, trigger: int, trigger_on: bool, falling: bool
) -> HwConfig:
    """Raw HwConfig from CLI choices (offsets 0x80, neutral CH2). Raises ValueError."""
    v = float(VoltsPerDiv.parse(vdiv))
    tb = Timebase.parse(timebase)
    gain1 = tables.GAIN_CODES_CH1[v]
    if coupling == "ac":
        gain1 |= tables.CH1_AC_BIT
    flags = (tables.FLAG_TRIG_ENABLE if trigger_on else 0) | (
        tables.FLAG_EDGE_FALLING if falling else 0
    )
    return HwConfig(
        gain1=gain1,
        trig=trigger,
        tb=tables.TIMEBASES[tb.seconds].hw_code,
        flags=flags,
    )


# --------------------------------------------------------------------------------------------
# session: transport + safety gate + logging
# --------------------------------------------------------------------------------------------
class Session:
    """Owns an open transport. Gates writes by opcode class, logs JSONL and pretty-prints."""

    def __init__(
        self,
        transport: Transport,
        log_path: Path | None,
        *,
        allow_acquire: bool = False,
        allow_unknown: bool = False,
        out: Printer = print,
        input_fn: InputFn = input,
    ) -> None:
        self.transport = transport
        self.log_path = log_path
        self.allow_acquire = allow_acquire
        self.allow_unknown = allow_unknown
        self._print = out
        self._input = input_fn
        self._fh: IO[str] | None = None
        self._t0 = time.monotonic()
        self._confirmed: set[bytes] = set()
        self.armed = False
        """True after 0x0B until 0x0A/0x08/0x09 (labels all-zero reports "status: waiting")."""

    def __enter__(self) -> Session:
        self.transport.open()
        try:
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115
                self._print(f"# logging to {self.log_path}")
        except BaseException:
            self.transport.close()  # __exit__ does not run when __enter__ raises
            raise
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        self.transport.close()

    def say(self, text: str) -> None:
        self._print(text)

    def log(self, direction: str, data: bytes, note: str = "") -> None:
        t = time.monotonic()
        if self._fh is not None:
            rec: dict[str, Any] = {"t": t, "dir": direction, "hex": data.hex()}
            if note:
                rec["note"] = note
            self._fh.write(json.dumps(rec) + "\n")
            self._fh.flush()
        tag = "OUT" if direction == "out" else " IN"
        extra = f"  # {note}" if note else ""
        self._print(f"[{t - self._t0:9.4f}] {tag} {fmt_hex(data)}{extra}")

    def log_comment(self, text: str) -> None:
        """Record a ``#`` line in the JSONL (skipped by ``load_records``) and print it."""
        if self._fh is not None:
            self._fh.write(f"# {text}\n")
            self._fh.flush()
        self._print(f"# {text}")

    def check_allowed(self, payload: bytes) -> None:
        """Raise ``UnsafeOperation`` unless the payload's safety class is enabled."""
        payload = pad_report(payload)
        op = payload[0]
        problem = payload_problem(payload)
        cls = "unknown" if problem else classify(op)
        if cls == "read":
            return
        if cls == "acquire":
            if not self.allow_acquire:
                raise UnsafeOperation(
                    f"opcode 0x{op:02X} is an acquire-class command; pass --allow-acquire"
                )
            return
        why = f" ({problem})" if problem else ""
        if not self.allow_unknown:
            raise UnsafeOperation(
                f"opcode 0x{op:02X} is unknown/unsafe{why}; pass --allow-unknown "
                f"(and type the confirmation)"
            )
        if payload in self._confirmed:
            return
        self._print(
            f"WARNING: payload {fmt_hex(payload)}{why} has never been verified on hardware "
            f"and may misconfigure or hang the device."
        )
        try:
            answer = self._input(f"Type '{CONFIRM_PHRASE}' to send it: ")
        except EOFError:
            answer = ""
        if answer.strip() != CONFIRM_PHRASE:
            raise UnsafeOperation(f"confirmation not given; opcode 0x{op:02X} NOT sent")
        self._confirmed.add(payload)

    def send(self, payload: bytes, note: str = "") -> None:
        payload = pad_report(payload)
        self.check_allowed(payload)
        try:
            self.transport.write(payload)
        except Exception as e:
            self.log_comment(f"write failed: {e} (payload {payload.hex()})")
            raise
        self.armed = armed_after(payload[0], self.armed)
        self.log("out", payload, note or annotate_out(payload))

    def recv(self, timeout_s: float, note: str = "") -> bytes | None:
        data = self.transport.read(timeout_s)
        if data is not None:
            self.log("in", data, note or annotate_in(data, armed=self.armed))
        return data


# --------------------------------------------------------------------------------------------
# subcommands (each returns an exit code)
# --------------------------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace, out: Printer, hid_module: Any | None = None) -> int:
    devices = list_devices(hid_module)
    if not devices:
        out("no EDU09 device found (VID 0x10CF, PID 0xED90)")
        return EXIT_FAIL
    for d in devices:
        out(
            f"{d.path}  vid=0x{d.vid:04X} pid=0x{d.pid:04X} interface={d.interface} "
            f"serial={d.serial!r} product={d.product!r} manufacturer={d.manufacturer!r}"
        )
    return EXIT_OK


def cmd_version(s: Session, args: argparse.Namespace) -> int:
    s.send(encode_version_query(), "version query")
    data = s.recv(args.timeout)
    if data is None:
        s.say("# no reply")
        return EXIT_FAIL
    s.say(f"firmware version: {parse_version(data)!r}")
    return EXIT_OK


def cmd_listen(s: Session, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.seconds
    n = 0
    while time.monotonic() < deadline:
        if s.recv(min(0.1, max(0.001, deadline - time.monotonic()))) is not None:
            n += 1
    s.say(f"# listen finished: {n} report(s) in {args.seconds:g} s")
    return EXIT_OK


def drain(s: Session, timeout_s: float = 0.2, limit: int = 64) -> int:
    n = 0
    while n < limit and s.recv(timeout_s, "reply") is not None:
        n += 1
    return n


def cmd_config(s: Session, args: argparse.Namespace) -> int:
    payload = parse_hex(args.hex)
    if payload[0] == tables.OP_CONFIG:
        try:
            cfg = decode_config(pad_report(payload))
        except ValueError as e:
            raise UnsafeOperation(f"not a decodable config payload ({e}); NOT sent") from e
        s.say(f"# decoded config: {cfg}")
    s.send(payload, "config (raw)")
    n = drain(s, args.timeout)
    s.say(f"# {n} reply report(s)")
    return EXIT_OK


def cmd_send(s: Session, args: argparse.Namespace) -> int:
    s.send(parse_hex(args.hex))
    n = drain(s, args.timeout)
    s.say(f"# {n} reply report(s)")
    return EXIT_OK


def acquire_config(args: argparse.Namespace) -> HwConfig:
    """Validated config for ``acquire``. Raises ``ValueError`` / ``UnsafeOperation``."""
    cfg = build_config(
        args.vdiv,
        args.timebase,
        args.coupling,
        args.trigger_level,
        not args.no_trigger,
        args.falling,
    )
    tb = tables.TIMEBASES[Timebase.parse(args.timebase).seconds]
    if args.no_trigger and tb.mode != tables.MODE_REALTIME:
        raise UnsafeOperation(
            f"--no-trigger is not allowed with ETS timebase {args.timebase} (ETS needs a "
            f"trigger to interleave samples; free run would never complete)"
        )
    return cfg


def cmd_acquire(s: Session, args: argparse.Namespace) -> int:
    try:
        return _acquire(s, args)
    except KeyboardInterrupt:
        s.say("# interrupted; sending stop")
        with contextlib.suppress(Exception):
            s.send(encode_stop(), "stop after interrupt")
        return EXIT_INTERRUPTED


def _acquire(s: Session, args: argparse.Namespace) -> int:
    cfg = acquire_config(args)
    tb = tables.TIMEBASES[Timebase.parse(args.timebase).seconds]
    frame_s = tables.FRAME_SIZE * tb.dt
    wait_s = args.timeout if args.timeout is not None else max(3.0, 1.5 * frame_s)
    s.say(f"# config: {cfg}  mode={tb.mode}  frame={frame_s:.4g} s  trigger wait={wait_s:g} s")
    failures = 0
    for i in range(args.count):
        tag = f"#{i + 1}"
        s.send(encode_config(cfg), f"{tag} config")
        s.send(encode_reset(), f"{tag} reset")
        s.send(encode_arm(), f"{tag} arm")
        t_arm = time.monotonic()
        deadline = t_arm + wait_s
        complete = False
        seen_mode = tb.mode
        other = waiting = 0
        while time.monotonic() < deadline:
            data = s.recv(min(0.05, max(0.001, deadline - time.monotonic())))
            if data is None:
                continue
            st = parse_status(data)
            if st is not None and st.complete:
                complete = True
                seen_mode = st.mode
                s.say(
                    f"# {tag} status complete mode={st.mode} after {time.monotonic() - t_arm:.3f} s"
                )
                if st.mode != tb.mode:
                    s.say(f"# warning: expected mode {tb.mode}")
                break
            if is_waiting_report(data):
                waiting += 1
            else:
                other += 1
        if not complete:
            s.say(
                f"# {tag} no completion status in {wait_s:g} s "
                f"({waiting} waiting, {other} other report(s))"
            )
            s.send(encode_stop(), f"{tag} stop after timeout")
            drain(s, 0.2)
            failures += 1
            continue
        s.send(encode_readout(), f"{tag} readout")
        # Skip leading status of the mode the device actually reported (H1 verifies Table B modes;
        # with tb.mode a mismatching leading status report would be taken as data).
        asm = FrameAssembler(expected_mode=seen_mode)
        frame_deadline = time.monotonic() + max(2.0, 2 * frame_s)
        while not asm.done and time.monotonic() < frame_deadline:
            data = s.recv(0.2, f"{tag} data {asm.reports_received}")
            asm.feed(data)
        if not asm.done:
            s.say(f"# {tag} frame incomplete: {asm.reports_received}/16 reports")
            failures += 1
            continue
        f = asm.frame
        # HW (protocol.md 11, finding 18): one stale data-like report follows the frame; read it
        # here so it does not show up after the next arm
        for _ in range(tables.READOUT_TRAILING_REPORTS):
            if s.recv(0.05, f"{tag} trailing report") is None:
                break
        s.say(
            f"# {tag} frame ok: min={min(f)} max={max(f)} mean={sum(f) / len(f):.1f} "
            f"first={f[0]} last={f[-1]}"
        )
    return EXIT_OK if failures == 0 else EXIT_FAIL


def cmd_replay(args: argparse.Namespace, out: Printer) -> int:
    """Pretty-print a capture offline (no device)."""
    records = load_records(args.file)
    t0 = records[0].t if records else 0.0
    n_in = n_out = 0
    armed = False
    for r in records:
        if r.dir == "out":
            n_out += 1
            ann = annotate_out(r.data)
            armed = armed_after(r.data[0], armed)
        else:
            n_in += 1
            ann = annotate_in(r.data, armed=armed)
        tag = "OUT" if r.dir == "out" else " IN"
        notes = " ".join(x for x in (ann, r.note) if x)
        out(f"[{r.t - t0:9.4f}] {tag} {fmt_hex(r.data)}" + (f"  # {notes}" if notes else ""))
    out(f"# {len(records)} record(s): {n_out} out, {n_in} in")
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--path", help="HID path (see 'list')")
    p.add_argument("--serial", help="HID serial number")
    p.add_argument("--timeout", type=float, default=None, help="read timeout / wait in seconds")
    p.add_argument("--captures-dir", default="captures", help="directory for JSONL logs")
    p.add_argument("--capture", help="explicit JSONL log file (overrides --captures-dir)")
    p.add_argument("--no-capture", action="store_true", help="do not write a JSONL log")
    p.add_argument("--allow-acquire", action="store_true", help="permit acquire-class opcodes")
    p.add_argument(
        "--allow-unknown",
        action="store_true",
        help="permit unknown opcodes (0x0C, 0x14, ...); still asks for typed confirmation",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="edu09-probe",
        description="Read-only-by-default raw probe for the Velleman EDU09 scope.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = _common()
    sub.add_parser("list", help="enumerate HID devices (does not open them)")
    sub.add_parser("version", parents=[c], help="query firmware version (0x0F)")
    p = sub.add_parser("listen", parents=[c], help="read only; send nothing")
    p.add_argument("--seconds", type=float, default=5.0)
    p = sub.add_parser("config", parents=[c], help="send raw config bytes (needs --allow-acquire)")
    p.add_argument("hex", nargs="+", metavar="HEX", help="e.g. 0E 80 07 0A 04 80 80 80 08 02")
    p = sub.add_parser("send", parents=[c], help="send one raw report (opcode-gated)")
    p.add_argument("hex", nargs="+", metavar="HEX", help="payload without report-ID byte")
    p = sub.add_parser("acquire", parents=[c], help="config, arm, wait for status, read 16 reports")
    p.add_argument("--timebase", default="2ms", help="e.g. 2ms, 500us (default 2ms)")
    p.add_argument("--vdiv", default="1", help="V/div: 0.1 0.2 0.5 1 2 5 (default 1)")
    p.add_argument("--coupling", choices=("dc", "ac"), default="dc")
    p.add_argument("--trigger-level", type=lambda x: int(x, 0), default=0x80, metavar="BYTE")
    p.add_argument("--no-trigger", action="store_true", help="free run (trigger flag off)")
    p.add_argument("--falling", action="store_true", help="falling edge (flag bit 2)")
    p.add_argument("--count", type=int, default=1, help="number of acquisitions")
    p = sub.add_parser("replay", help="pretty-print a JSONL capture offline")
    p.add_argument("file")
    return ap


def default_log_path(args: argparse.Namespace) -> Path | None:
    if args.no_capture:
        return None
    if args.capture:
        return Path(args.capture)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(args.captures_dir) / f"{stamp}-{args.cmd}.jsonl"


def preflight(args: argparse.Namespace, s: Session) -> None:
    """Check safety gates BEFORE the device is touched."""
    if args.cmd in ("config", "send"):
        payload = pad_report(parse_hex(args.hex))
        if args.cmd == "config" and payload[0] == tables.OP_CONFIG:
            try:
                decode_config(payload)
            except ValueError as e:
                raise UnsafeOperation(f"not a decodable config payload ({e}); NOT sent") from e
        s.check_allowed(payload)
    elif args.cmd == "acquire":
        s.check_allowed(encode_config(acquire_config(args)))


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[argparse.Namespace], Transport] | None = None,
    out: Printer = print,
    input_fn: InputFn = input,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "list":
            return cmd_list(args, out)
        if args.cmd == "replay":
            return cmd_replay(args, out)
        if args.cmd == "version" and args.timeout is None:
            args.timeout = 1.0
        if args.timeout is None and args.cmd != "acquire":
            args.timeout = 0.5
        transport: Transport
        if transport_factory is not None:
            transport = transport_factory(args)
        else:
            transport = HidTransport(
                path=args.path, serial=args.serial, allow_unknown_opcodes=args.allow_unknown
            )
        session = Session(
            transport,
            default_log_path(args),
            allow_acquire=args.allow_acquire,
            allow_unknown=args.allow_unknown,
            out=out,
            input_fn=input_fn,
        )
        preflight(args, session)
        with session:
            handlers: dict[str, Callable[[Session, argparse.Namespace], int]] = {
                "version": cmd_version,
                "listen": cmd_listen,
                "config": cmd_config,
                "send": cmd_send,
                "acquire": cmd_acquire,
            }
            return handlers[args.cmd](session, args)
    except UnsafeOperation as e:
        out(f"refused: {e}")
        return EXIT_REFUSED
    except (Edu09Error, ValueError, OSError) as e:
        out(f"error: {e}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        out("interrupted")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
