"""Reviewer edge-case tests for the transport layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from edu09.errors import DeviceDisconnected, ProtocolError
from edu09.protocol import tables
from edu09.transport import ReplayTransport, base
from edu09.transport.replay import Record


def test_transport_constants_match_protocol_tables() -> None:
    assert base.USB_VID == tables.USB_VID
    assert base.USB_PID == tables.USB_PID
    assert base.REPORT_SIZE == tables.REPORT_SIZE


def _pad(b: bytes) -> bytes:
    return b + bytes(64 - len(b))


def test_replay_strict_write_after_exhaustion_raises() -> None:
    t = ReplayTransport([Record(0.0, "out", _pad(b"\x0f"))], strict=True)
    t.open()
    t.write(_pad(b"\x0f"))
    assert t.exhausted
    with pytest.raises(ProtocolError):
        t.write(_pad(b"\x0f"))


def test_replay_write_wrong_size_and_closed() -> None:
    t = ReplayTransport([])
    with pytest.raises(DeviceDisconnected):
        t.write(_pad(b"\x0f"))
    t.open()
    with pytest.raises(ValueError):
        t.write(b"\x0f")
    t.close()
    with pytest.raises(DeviceDisconnected):
        t.read(0.0)


def test_replay_bad_dir_and_hex(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('{"dir": "sideways", "hex": "' + "00" * 64 + '"}\n')
    with pytest.raises(ProtocolError):
        ReplayTransport(p)
    p.write_text('{"dir": "in", "hex": 5}\n')
    with pytest.raises(ProtocolError):
        ReplayTransport(p)
