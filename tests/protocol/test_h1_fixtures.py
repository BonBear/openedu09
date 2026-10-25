"""H1 hardware captures (tests/fixtures/h1, firmware 1.01, 2026-10-24) through the codecs."""

from __future__ import annotations

from pathlib import Path

import pytest

from edu09 import probe
from edu09.protocol import (
    FrameAssembler,
    encode_arm,
    encode_readout,
    encode_reset,
    encode_stop,
    encode_version_query,
    is_complete_status,
    is_waiting_report,
    parse_status,
    parse_version,
    tables,
)
from edu09.transport import ReplayTransport
from edu09.transport.replay import load_records

H1 = Path(__file__).resolve().parents[1] / "fixtures" / "h1"


def _replay(name: str) -> ReplayTransport:
    t = ReplayTransport(H1 / name, strict=True)
    t.open()
    return t


def _drain(t: ReplayTransport) -> list[bytes]:
    out = []
    while (r := t.read(0.1)) is not None:
        out.append(r)
    return out


def _write_out_records(t: ReplayTransport, name: str, upto_op: int) -> None:
    """Write the capture's OUT records up to and including the first one with opcode ``upto_op``."""
    for rec in load_records(H1 / name):
        if rec.dir == "out":
            t.write(rec.data)
            if rec.data[0] == upto_op:
                return
    raise AssertionError(f"no 0x{upto_op:02X} in {name}")


def test_idle_listen_capture_is_empty() -> None:
    assert load_records(H1 / "listen_idle.jsonl") == []


def test_version_reply_has_cr_and_parses() -> None:
    t = _replay("version.jsonl")
    t.write(encode_version_query())
    r = t.read(0.1)
    assert r is not None and r[:5] == b"1.01\r"
    assert parse_version(r) == "1.01"
    assert t.read(0.1) is None and t.exhausted


def test_config_and_idle_stop_have_no_reply() -> None:
    for name in ("config_noreply.jsonl", "stop_idle.jsonl"):
        recs = load_records(H1 / name)
        assert [r.dir for r in recs] == ["out"]
    t = _replay("stop_idle.jsonl")
    t.write(encode_stop())
    assert t.read(0.1) is None and t.exhausted


@pytest.mark.parametrize(
    ("name", "timebase", "leading"),
    [("freerun_2ms.jsonl", 2e-3, 2), ("freerun_500us.jsonl", 500e-6, 1)],
)
def test_freerun_frame_decodes(name: str, timebase: float, leading: int) -> None:
    tb = tables.TIMEBASES[timebase]
    recs = load_records(H1 / name)
    cfg = recs[0].data
    assert cfg[tables.CFG_IDX_TB] == tb.hw_code
    assert cfg[tables.CFG_IDX_FLAGS] == 0  # free run
    t = _replay(name)
    _write_out_records(t, name, tables.OP_ARM)
    # Only the completion status, about one frame time after the arm; no waiting reports.
    pre = _drain(t)
    assert len(pre) == 1 and is_complete_status(pre[0], tb.mode)
    assert pre[0] == bytes([0x64, 0xC8, tb.mode]) + bytes(61)
    arm_t = next(r.t for r in recs if r.dir == "out" and r.data[0] == tables.OP_ARM)
    done_t = next(r.t for r in recs if r.dir == "in")
    frame_s = tables.FRAME_SIZE * tb.dt
    assert frame_s <= done_t - arm_t <= frame_s + 0.01
    t.write(encode_readout())
    asm = FrameAssembler(expected_mode=tb.mode)
    while not asm.done:
        r = t.read(0.1)
        assert r is not None, "capture ended before the frame was complete"
        asm.feed(r)
    assert t.exhausted
    assert asm.skipped_status == leading  # host-latency dependent (2 at 2 ms, 1 at 500 us)
    frame = asm.frame
    assert len(frame) == tables.FRAME_SIZE
    # Open input, 1 V/div, offset 0x80: raw 131..135, mean ~132.6 (model predicts 127).
    assert min(frame) >= 130 and max(frame) <= 136
    assert sum(frame) / len(frame) == pytest.approx(132.6, abs=0.6)


def test_trigger_timeout_yields_only_waiting_reports() -> None:
    name = "trigger_timeout_2ms.jsonl"
    recs = load_records(H1 / name)
    assert recs[0].data[tables.CFG_IDX_FLAGS] == tables.FLAG_TRIG_ENABLE
    t = _replay(name)
    t.write(recs[0].data)
    t.write(encode_reset())
    t.write(encode_arm())
    waiting = _drain(t)
    assert len(waiting) == 91  # ~3 s at ~33 ms
    assert all(is_waiting_report(r) for r in waiting)
    assert all(parse_status(r) is None for r in waiting)  # not a 64 xx mm report
    assert not any(is_complete_status(r) for r in waiting)
    # ~33 ms cadence, the first one ~33 ms after the arm
    ins = [r.t for r in recs if r.dir == "in"]
    arm_t = recs[2].t
    assert ins[0] - arm_t == pytest.approx(tables.STATUS_INTERVAL_S, abs=0.004)
    gaps = [b - a for a, b in zip(ins[:-2], ins[1:-1], strict=True)]
    assert sum(gaps) / len(gaps) == pytest.approx(tables.STATUS_INTERVAL_S, abs=0.001)
    # stop while armed: one more all-zero report, then nothing
    t.write(encode_stop())
    after = _drain(t)
    assert after == [tables.WAITING_REPORT]
    assert t.exhausted


def test_is_waiting_report_is_exact() -> None:
    assert is_waiting_report(bytes(64))
    assert not is_waiting_report(bytes(63))
    assert not is_waiting_report(bytes(63) + b"\x01")
    assert not is_waiting_report(bytes([0x64, 0x00, 1]) + bytes(61))


def test_probe_replay_labels_waiting_reports() -> None:
    lines: list[str] = []
    assert probe.main(["replay", str(H1 / "trigger_timeout_2ms.jsonl")], out=lines.append) == 0
    waiting = [ln for ln in lines if "status: waiting" in ln]
    assert len(waiting) == 91  # every zero report while armed; the one after stop is not
    assert "all-zero report" in lines[-2]  # the report after 0x08 (device no longer armed)


def test_probe_annotation_depends_on_armed_state() -> None:
    assert probe.annotate_in(bytes(64), armed=True) == "status: waiting"
    assert probe.annotate_in(bytes(64)) == "all-zero report"
    assert probe.annotate_in(bytes([0x64, 0xC8, 1]) + bytes(61)) == "status: COMPLETE mode=1"
