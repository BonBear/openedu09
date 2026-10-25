"""Tests for edu09.protocol.reports: version, status and FrameAssembler."""

from __future__ import annotations

import pytest

from edu09.errors import ProtocolError
from edu09.protocol.reports import (
    FrameAssembler,
    Status,
    is_complete_status,
    parse_status,
    parse_version,
)


def _report(prefix: bytes, fill: int = 0) -> bytes:
    return prefix + bytes([fill]) * (64 - len(prefix))


# Hardware-like version reply: "1.01" followed by uninitialised garbage.
VERSION_REPLY = b"1.01" + bytes(range(0xA0, 0xA0 + 60))


def test_parse_version_hw_reply() -> None:
    assert parse_version(VERSION_REPLY) == "1.01"


def test_parse_version_strips_trailing_nul_and_space() -> None:
    assert parse_version(_report(b"1.1\x00")) == "1.1"
    assert parse_version(_report(b"2.0 ")) == "2.0"


def test_parse_version_accepts_bytearray_and_memoryview() -> None:
    assert parse_version(bytearray(VERSION_REPLY)) == "1.01"
    assert parse_version(memoryview(VERSION_REPLY)) == "1.01"


@pytest.mark.parametrize("reply", [b"1.0", _report(b"\xff\x01\x02\x03"), _report(b""), b""])
def test_parse_version_rejects_garbage(reply: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_version(reply)


def test_parse_status_complete() -> None:
    r = _report(b"\x64\xc8\x01", fill=0x55)
    st = parse_status(r)
    assert st == Status(complete=True, mode=1, raw=r)
    assert st is not None
    assert st.flag == 0xC8
    assert len(st.raw) == 64


def test_parse_status_waiting() -> None:
    st = parse_status(_report(b"\x64\x00\x03"))
    assert st is not None
    assert not st.complete
    assert st.mode == 3


def test_parse_status_not_a_status() -> None:
    assert parse_status(_report(b"\x63\xc8\x01")) is None
    assert parse_status(VERSION_REPLY) is None


def test_parse_status_errors() -> None:
    with pytest.raises(ProtocolError):
        parse_status(b"")
    with pytest.raises(ProtocolError):
        parse_status(b"\x64\xc8")


@pytest.mark.parametrize(
    ("r", "expected_mode", "result"),
    [
        (_report(b"\x64\xc8\x01"), None, True),
        (_report(b"\x64\xc8\x04"), None, True),
        (_report(b"\x64\xc8\x05"), None, False),  # mode not in table
        (_report(b"\x64\xc8\x02"), 2, True),
        (_report(b"\x64\xc8\x02"), 1, False),
        (_report(b"\x64\xc7\x01"), None, False),
        (_report(b"\x65\xc8\x01"), None, False),
        (b"\x64\xc8", None, False),
    ],
)
def test_is_complete_status(r: bytes, expected_mode: int | None, result: bool) -> None:
    assert is_complete_status(r, expected_mode) is result


def _frame_reports(first_byte: int = 0x10) -> list[bytes]:
    frame = bytes((i * 7 + 3) & 0xFF for i in range(1024))
    frame = bytes([first_byte]) + frame[1:]
    return [frame[i : i + 64] for i in range(0, 1024, 64)]


def test_frame_assembler_plain() -> None:
    reports = _frame_reports()
    asm = FrameAssembler()
    results = [asm.feed(r) for r in reports]
    assert results == [False] * 15 + [True]
    assert asm.done
    assert asm.frame == b"".join(reports)
    assert asm.reports_received == 16
    assert asm.skipped_status == 0


def test_frame_assembler_skips_leading_status_reports() -> None:
    reports = _frame_reports()
    asm = FrameAssembler(expected_mode=1)
    status = _report(b"\x64\xc8\x01", fill=0x99)
    assert asm.feed(status) is False
    assert asm.feed(status) is False
    for r in reports:
        asm.feed(r)
    assert asm.skipped_status == 2
    assert asm.frame == b"".join(reports)


def test_frame_assembler_first_data_byte_0x64_is_data() -> None:
    # R1: EDU09.exe would discard this report; we count it as data.
    reports = _frame_reports(first_byte=0x64)
    asm = FrameAssembler(expected_mode=1)
    for r in reports:
        asm.feed(r)
    assert asm.done
    assert asm.frame[0] == 0x64
    assert asm.skipped_status == 0


def test_frame_assembler_waiting_status_counts_as_data() -> None:
    # Only exact 64 C8 mm is skipped; 64 00 01 is data.
    asm = FrameAssembler(expected_mode=1)
    asm.feed(_report(b"\x64\x00\x01"))
    assert asm.reports_received == 1


def test_frame_assembler_status_with_other_mode_is_data() -> None:
    asm = FrameAssembler(expected_mode=2)
    asm.feed(_report(b"\x64\xc8\x01"))
    assert asm.reports_received == 1


def test_frame_assembler_status_after_data_is_data() -> None:
    reports = _frame_reports()
    status = _report(b"\x64\xc8\x01")
    asm = FrameAssembler()
    asm.feed(reports[0])
    asm.feed(status)
    assert asm.reports_received == 2
    assert asm.skipped_status == 0


def test_frame_assembler_too_many_status() -> None:
    asm = FrameAssembler(max_leading_status=3)
    status = _report(b"\x64\xc8\x01")
    for _ in range(3):
        asm.feed(status)
    with pytest.raises(ProtocolError):
        asm.feed(status)


def test_frame_assembler_errors_and_reset() -> None:
    asm = FrameAssembler()
    with pytest.raises(ProtocolError):
        _ = asm.frame
    with pytest.raises(ProtocolError):
        asm.feed(bytes(63))
    reports = _frame_reports()
    for r in reports:
        asm.feed(r)
    with pytest.raises(ProtocolError):
        asm.feed(reports[0])
    asm.reset()
    assert not asm.done
    assert asm.reports_received == 0
    for r in reports:
        asm.feed(r)
    assert asm.frame == b"".join(reports)


def test_frame_assembler_feed_none_is_no_progress() -> None:
    asm = FrameAssembler(expected_mode=1)
    assert asm.feed(None) is False
    assert (asm.reports_received, asm.skipped_status) == (0, 0)
    reports = _frame_reports()
    for r in reports[:5]:
        asm.feed(r)
    assert asm.feed(None) is False
    assert asm.reports_received == 5
    for r in reports[5:]:
        asm.feed(r)
    assert asm.done


def test_frame_assembler_feed_none_after_done_does_not_raise() -> None:
    asm = FrameAssembler()
    for r in _frame_reports():
        asm.feed(r)
    assert asm.feed(None) is False
    assert asm.done


def test_parse_version_cr_terminated() -> None:
    hw = bytes.fromhex("312e30310dd3f16be2ef62e2fa35b9bc") + bytes(48)  # H1 capture prefix
    assert parse_version(hw) == "1.01"
    assert parse_version(_report(b"1.10a\r", fill=0x41)) == "1.10a"  # terminator wins
    assert parse_version(_report(b"1.01", fill=0x41)) == "1.01"  # unterminated: 4 bytes
