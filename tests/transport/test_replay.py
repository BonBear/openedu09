from __future__ import annotations

import json
from pathlib import Path

import pytest

from edu09.errors import DeviceDisconnected, ProtocolError
from edu09.transport import RecordingTransport, ReplayTransport
from edu09.transport.replay import load_records


def _pad(b: bytes) -> bytes:
    return b + bytes(64 - len(b))


def _write_capture(path: Path) -> None:
    rows = [
        ("out", _pad(b"\x0f"), "version"),
        ("in", _pad(b"1.01"), ""),
        ("out", _pad(b"\x0b"), ""),
        ("in", _pad(b"\x64\xc8\x01"), ""),
    ]
    with open(path, "w") as f:
        f.write("# comment\n\n")
        for i, (d, data, note) in enumerate(rows):
            f.write(json.dumps({"t": float(i), "dir": d, "hex": data.hex(), "note": note}) + "\n")


def test_replay_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    _write_capture(p)
    t = ReplayTransport(p, strict=True)
    with pytest.raises(DeviceDisconnected):
        t.read(0.1)
    t.open()
    assert t.read(0.1) is None  # out record pending
    t.write(_pad(b"\x0f"))
    assert t.read(0.1)[:4] == b"1.01"  # type: ignore[index]
    t.write(_pad(b"\x0b"))
    assert t.read(0.1)[:3] == b"\x64\xc8\x01"  # type: ignore[index]
    assert t.exhausted and t.read(0.1) is None
    t.rewind()
    assert not t.exhausted


def test_strict_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    _write_capture(p)
    t = ReplayTransport(p, strict=True)
    t.open()
    with pytest.raises(ProtocolError):
        t.write(_pad(b"\x09"))


def test_lenient_ignores_unmatched_write(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    _write_capture(p)
    t = ReplayTransport(p)
    t.open()
    t.write(_pad(b"\x09"))  # matches slot, not compared
    assert t.read(0.1)[:4] == b"1.01"  # type: ignore[index]
    t.write(_pad(b"\x77"))  # next is out: consumed
    t.write(_pad(b"\x77"))  # next is in: ignored
    assert t.read(0.1) is not None


def test_bad_records(tmp_path: Path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text('{"dir": "in", "hex": "00"}\n')
    with pytest.raises(ProtocolError):
        load_records(p)
    p.write_text("not json\n")
    with pytest.raises(ProtocolError):
        load_records(p)


def test_recording_wraps_replay(tmp_path: Path) -> None:
    src = tmp_path / "c.jsonl"
    _write_capture(src)
    out = tmp_path / "sub" / "rec.jsonl"
    rec = RecordingTransport(ReplayTransport(src), out)
    rec.open()
    rec.write(_pad(b"\x0f"))
    assert rec.read(0.1) is not None
    assert rec.read(0.1) is None  # timeout not recorded
    rec.close()
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["dir"] for x in lines] == ["out", "in"]
    assert bytes.fromhex(lines[1]["hex"])[:4] == b"1.01"
    assert all(isinstance(x["t"], float) for x in lines)
    # recorded file replays
    assert len(load_records(out)) == 2


def test_recording_failed_inner_write_not_recorded(tmp_path: Path) -> None:
    class Failing(ReplayTransport):
        def write(self, payload: bytes) -> None:
            raise DeviceDisconnected("x")

    cap = tmp_path / "c.jsonl"
    rec = RecordingTransport(Failing([]), cap)
    rec.open()
    with pytest.raises(DeviceDisconnected):
        rec.write(_pad(b"\x0f"))
    rec.close()
    assert cap.read_text() == ""


def test_recording_open_failure_closes_inner(tmp_path: Path) -> None:
    inner = ReplayTransport([])
    bad = tmp_path / "file"
    bad.write_text("x")
    rec = RecordingTransport(inner, bad / "sub" / "c.jsonl")  # parent is a file
    with pytest.raises(OSError):
        rec.open()
    assert not inner._open
