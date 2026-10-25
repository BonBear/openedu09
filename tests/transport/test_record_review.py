"""Review tests for ``--record``: ``check_record_path`` refuses non-regular targets and a
failing record file never makes the transport look disconnected."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from edu09.transport import RecordingTransport, ReplayTransport
from edu09.transport.replay import check_record_path


def _pad(b: bytes) -> bytes:
    return b + bytes(64 - len(b))


def test_check_record_path_creates_parents_and_file(tmp_path: Path) -> None:
    p = check_record_path(tmp_path / "a" / "b" / "rec.jsonl")
    assert p.is_file() and p.read_text() == ""


def test_check_record_path_appends_existing(tmp_path: Path) -> None:
    p = tmp_path / "rec.jsonl"
    p.write_text("keep\n")
    check_record_path(p)
    assert p.read_text() == "keep\n"


def test_check_record_path_refuses_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        check_record_path(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_check_record_path_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="not a regular file"):
        check_record_path(fifo)  # opening it for append would block forever (no reader)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_check_record_path_symlink_to_regular_file_ok(tmp_path: Path) -> None:
    target = tmp_path / "real.jsonl"
    target.write_text("")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    check_record_path(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_check_record_path_symlink_to_fifo_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    link = tmp_path / "link.jsonl"
    link.symlink_to(fifo)
    with pytest.raises(OSError, match="not a regular file"):
        check_record_path(link)


def test_record_write_failure_does_not_raise_from_transport(tmp_path: Path) -> None:
    """Disk full etc. while recording: the worker treats transport OSError as a disconnect,
    so the wrapper must stop recording instead of raising."""

    class Broken:
        def write(self, _s: str) -> int:
            raise OSError(28, "No space left on device")

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    inner = ReplayTransport([], strict=False)
    rec = RecordingTransport(inner, tmp_path / "c.jsonl")
    rec.open()
    rec._fh.close()  # type: ignore[union-attr]
    rec._fh = Broken()  # type: ignore[assignment]
    rec.write(_pad(b"\x0f"))  # must not raise
    assert rec._fh is None
    rec.write(_pad(b"\x0f"))  # recording stays off
    rec.close()
