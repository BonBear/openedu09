"""Review edge cases for edu09.probe: device lifetime, byte validity, probe x simulator."""

from __future__ import annotations

from pathlib import Path

import pytest

from edu09 import probe
from edu09.protocol import tables
from edu09.protocol.commands import decode_config
from edu09.transport import SimTransport, TransportInfo

H1_TIMEBASES = ["2ms", "20ms", "500us", "200us", "50us", "20us", "10us"]
VALID_TB_CODES = {e.hw_code for e in tables.TIMEBASES.values()}
VALID_GAIN1 = set(tables.GAIN_CODES_CH1.values())


class RecordingSim(SimTransport):
    """Virtual-clock simulator that records full OUT payloads and open/close."""

    def __init__(self, signal: str = "sine:1khz:2vpp", **kw: object) -> None:
        sim = SimTransport.virtual(signal, seed=0)
        super().__init__(device=sim.device, sleep=sim._sleep, **kw)  # type: ignore[arg-type]
        self.payloads: list[bytes] = []
        self.opened = 0
        self.closed = 0

    def open(self) -> None:
        self.opened += 1
        super().open()

    def close(self) -> None:
        self.closed += 1
        super().close()

    def write(self, payload: bytes) -> None:
        self.payloads.append(bytes(payload))
        super().write(payload)


def _run(argv: list[str], transport: object, input_fn=lambda p: "") -> tuple[int, list[str]]:
    lines: list[str] = []
    rc = probe.main(
        [*argv, "--no-capture"],
        transport_factory=lambda a: transport,
        out=lines.append,
        input_fn=input_fn,
    )
    return rc, lines


def _assert_config_bytes_valid(payload: bytes) -> None:
    cfg = decode_config(payload)  # header 0E 80 07, flags bits 4..7 clear
    assert payload[tables.CONFIG_LENGTH :] == bytes(64 - tables.CONFIG_LENGTH)
    assert cfg.gain1 & ~tables.CH1_AC_BIT in VALID_GAIN1
    assert cfg.gain2 == tables.CH2_NEUTRAL_CFG
    assert cfg.ofs1 == cfg.ofs2 == tables.OFFSET_DAC_DEFAULT
    assert cfg.tb in VALID_TB_CODES
    assert cfg.flags & ~(tables.FLAG_TRIG_ENABLE | tables.FLAG_EDGE_FALLING) == 0


@pytest.mark.parametrize("tb", H1_TIMEBASES)
@pytest.mark.parametrize("free_run", [False, True])
def test_acquire_against_simulator(tb: str, free_run: bool) -> None:
    """The H1 acquire commands complete end-to-end against the device model (all modes)."""
    t = RecordingSim()
    argv = ["acquire", "--allow-acquire", "--timebase", tb, "--vdiv", "1", "--count", "2"]
    if free_run:
        argv.append("--no-trigger")
        if tb in ("50us", "20us", "10us"):
            rc, lines = _run(argv, t)
            assert rc == 2 and "ETS" in lines[-1] and t.opened == 0
            return
    rc, lines = _run(argv, t)
    assert rc == 0, lines
    assert sum("frame ok" in ln for ln in lines) == 2
    assert t.opened == 1 and t.closed == 1
    ops = [p[0] for p in t.payloads]
    assert set(ops) <= {tables.OP_CONFIG, tables.OP_RESET, tables.OP_ARM, tables.OP_READOUT}
    for p in t.payloads:
        if p[0] == tables.OP_CONFIG:
            _assert_config_bytes_valid(p)
        else:
            assert p[1:] == bytes(63)  # 1-byte commands are zero padded


@pytest.mark.parametrize("vdiv", ["0.1", "0.2", "0.5", "1", "2", "5"])
@pytest.mark.parametrize("coupling", ["dc", "ac"])
def test_build_config_bytes_always_valid(vdiv: str, coupling: str) -> None:
    for tb in tables.TIMEBASES:
        for trig_on in (False, True):
            for falling in (False, True):
                cfg = probe.build_config(vdiv, f"{tb}", coupling, 0x80, trig_on, falling)
                _assert_config_bytes_valid(probe.encode_config(cfg))


def test_acquire_timeout_sends_only_stop_never_release() -> None:
    t = RecordingSim()
    t.device.never_trigger = True
    rc, lines = _run(["acquire", "--allow-acquire", "--timeout", "0.05"], t)
    assert rc == 1
    ops = [p[0] for p in t.payloads]
    assert ops[-1] == tables.OP_STOP
    assert tables.OP_RELEASE not in ops and tables.OP_UNKNOWN_0C not in ops
    assert t.closed == 1


def test_mode_mismatch_still_assembles_frame() -> None:
    """If the device reports another mode than Table B, leading status must not become data."""
    t = RecordingSim()
    t.device.leading_status_reports = 3
    real = t.device._status_report

    def wrong_mode() -> bytes:
        r = bytearray(real())
        r[2] = 3
        return bytes(r)

    t.device._status_report = wrong_mode  # type: ignore[method-assign]
    orig_readout = t.device._readout

    def readout() -> None:
        orig_readout()  # realtime wire order; rewrite the leading status to claim mode 3
        q = t.device._queue
        for i in range(len(q)):
            if q[i][:2] == bytes((0x64, 0xC8)):
                q[i] = q[i][:2] + b"\x03" + q[i][3:]

    t.device._readout = readout  # type: ignore[method-assign]
    rc, lines = _run(["acquire", "--allow-acquire"], t)
    assert any("warning: expected mode 1" in ln for ln in lines)
    assert rc == 0, lines
    # all 16 data reports consumed, no status taken as data, and the HW trailing report drained
    assert t.device.trailing_reports == 1
    assert t.device.pending_reports == 0


class _Scope:
    def __init__(self) -> None:
        self.info = TransportInfo(kind="fake")
        self.opened = self.closed = False
        self.written: list[bytes] = []

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def write(self, payload: bytes) -> None:
        self.written.append(payload)

    def read(self, timeout_s: float) -> bytes | None:
        raise KeyboardInterrupt


def test_ctrl_c_mid_acquire_closes_device() -> None:
    s = _Scope()
    rc, lines = _run(["acquire", "--allow-acquire"], s)
    assert rc == 130
    assert s.closed
    assert s.written[-1][0] == tables.OP_STOP  # best-effort stop before close
    assert all(p[0] != tables.OP_RELEASE for p in s.written)


def test_log_open_failure_closes_device(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = _Scope()
    lines: list[str] = []
    rc = probe.main(
        ["version", "--capture", str(blocker / "sub" / "x.jsonl")],
        transport_factory=lambda a: s,
        out=lines.append,
    )
    assert rc == 1
    assert s.opened and s.closed and s.written == []


@pytest.mark.parametrize("cmd", ["version", "listen"])
def test_default_commands_send_only_read_class(cmd: str) -> None:
    t = RecordingSim()
    argv = [cmd] + (["--seconds", "0.05"] if cmd == "listen" else [])
    rc, _ = _run(argv, t)
    assert rc == 0
    assert all(tables.OPCODE_CLASS.get(p[0]) == "read" for p in t.payloads)
    assert t.closed == 1


def test_acquire_drains_trailing_report_before_next_arm() -> None:
    """protocol.md 11 finding 18: the trailing report is read right after each frame, so in a
    multi-acquisition run it no longer shows up as the first report after the next arm."""
    t = RecordingSim()
    rc, lines = _run(["acquire", "--allow-acquire", "--count", "3", "--no-trigger"], t)
    assert rc == 0, lines
    assert sum("frame ok" in ln for ln in lines) == 3
    assert sum("trailing report" in ln for ln in lines) == 3
    assert t.device.pending_reports == 0
    # nothing but status reports between each arm and its completion
    armed = False
    for ln in lines:
        if "OUT 0b" in ln:
            armed = True
        elif " IN " in ln and armed:
            assert "64 c8" in ln or "status" in ln.lower() or "waiting" in ln.lower(), ln
            if "64 c8" in ln:
                armed = False
