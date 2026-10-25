"""H2 hardware capture of opcode 0x0D (tests/fixtures/h1/h2_live_0d.jsonl, firmware 1.01).

HW finding (protocol.md 11, H2): 0x0D never returns live ADC samples. While armed it switches the
device to a fast status stream (one report per ~1 ms) until 0x08; idle 0x0D gets no reply.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from edu09.protocol import is_complete_status, is_waiting_report, tables
from edu09.transport.replay import Record, load_records

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "h1" / "h2_live_0d.jsonl"


def _segments() -> list[tuple[Record, list[Record]]]:
    """(OUT record, IN records that followed it) in capture order."""
    out: list[tuple[Record, list[Record]]] = []
    for rec in load_records(FIXTURE):
        if rec.dir == "out":
            out.append((rec, []))
        else:
            out[-1][1].append(rec)
    return out


def test_live_replies_are_status_reports_only() -> None:
    ins = [r for r in load_records(FIXTURE) if r.dir == "in"]
    assert len(ins) > 700
    for r in ins:
        assert is_waiting_report(r.data) or is_complete_status(r.data, tables.MODE_REALTIME)
        assert not any(r.data[3:])  # never sample data


def test_live_stream_is_one_report_per_ms_and_second_0d_changes_nothing() -> None:
    segs = _segments()
    ops = [s[0].data[0] for s in segs]
    assert ops == [0x0E, 0x09, 0x0B, 0x0D, 0x0D, 0x08] * 2 + [0x09, 0x0D, 0x08]
    for k in (0, 6):
        first, second, stop = segs[k + 3][1], segs[k + 4][1], segs[k + 5][1]
        assert len(first) > 200 and len(second) > 100  # streaming continues after the 2nd 0x0D
        gaps = np.diff([r.t for r in first[-200:] + second])
        assert abs(float(np.median(gaps)) - tables.FAST_STATUS_INTERVAL_S) < 0.0002
        assert len(stop) == 1  # 0x08 ends it with one final report


def test_live_stream_completes_with_repeated_status_and_stop_reports_state() -> None:
    segs = _segments()
    # 2 ms/div free run (config tb byte 0x08): waiting... then 64 C8 01 every ms
    assert segs[6][0].data[8] == 0x08
    second_run = segs[9][1] + segs[10][1]
    assert all(is_complete_status(r.data, 1) for r in second_run)
    assert is_complete_status(segs[11][1][0].data, 1)  # post-stop report reflects completion
    # 500 ms/div run (tb 0x01) never completes: zero reports, and a zero post-stop report
    assert segs[0][0].data[8] == 0x01
    assert all(is_waiting_report(r.data) for r in segs[4][1])
    assert is_waiting_report(segs[5][1][0].data)


def test_live_when_idle_has_no_reply() -> None:
    segs = _segments()
    assert [s[0].data[0] for s in segs[-3:]] == [0x09, 0x0D, 0x08]
    assert all(not s[1] for s in segs[-3:])
