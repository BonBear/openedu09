"""JSONL capture recording and replay.

Record format, one JSON object per line:
    {"t": <monotonic seconds, float>, "dir": "out"|"in", "hex": "<128 hex chars>", "note": "<str>"}
``hex`` is the 64-byte payload (no report-ID byte). ``note`` is optional. Blank lines and lines
starting with ``#`` are ignored on load.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from edu09.errors import DeviceDisconnected, ProtocolError
from edu09.transport.base import REPORT_SIZE, Transport, TransportInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Record:
    t: float
    dir: str
    data: bytes
    note: str = ""


def load_records(path: str | Path) -> list[Record]:
    recs: list[Record] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                o = json.loads(line)
                d = o["dir"]
                data = bytes.fromhex(o["hex"])
                if d not in ("in", "out"):
                    raise ValueError(f"bad dir {d!r}")
                if len(data) != REPORT_SIZE:
                    raise ValueError(f"expected {REPORT_SIZE} bytes, got {len(data)}")
                recs.append(Record(float(o.get("t", 0.0)), d, data, str(o.get("note", ""))))
            except (ValueError, KeyError, TypeError) as e:
                raise ProtocolError(f"{path}:{n}: bad capture record: {e}") from e
    return recs


class ReplayTransport:
    """Plays back a capture. Reads return the next "in" record; writes consume "out" records.

    Cursor rules: ``read`` returns the next record if it is an "in" record, otherwise (next is an
    "out" record the host has not written yet, or the capture is exhausted) None (timeout).
    ``write`` consumes the next record if it is "out" (strict: payload must match, else
    ProtocolError); if the next record is "in", strict raises ProtocolError, non-strict ignores
    the write. Never sleeps.
    """

    def __init__(self, source: str | Path | list[Record], strict: bool = False) -> None:
        self._records = load_records(source) if isinstance(source, (str, Path)) else list(source)
        self._strict = strict
        self._pos = 0
        self._open = False
        self.info = TransportInfo(
            kind="replay", path=str(source) if not isinstance(source, list) else ""
        )

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._records)

    @property
    def position(self) -> int:
        return self._pos

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def rewind(self) -> None:
        self._pos = 0

    def write(self, payload: bytes) -> None:
        if not self._open:
            raise DeviceDisconnected("replay transport is not open")
        if len(payload) != REPORT_SIZE:
            raise ValueError(f"payload must be exactly {REPORT_SIZE} bytes, got {len(payload)}")
        rec = self._records[self._pos] if self._pos < len(self._records) else None
        if rec is not None and rec.dir == "out":
            if self._strict and rec.data != bytes(payload):
                raise ProtocolError(
                    f"replay mismatch at record {self._pos}: expected {rec.data[:10].hex()}, "
                    f"got {bytes(payload)[:10].hex()}"
                )
            self._pos += 1
        elif self._strict:
            raise ProtocolError(f"unexpected write at record {self._pos} (unread input or end)")

    def read(self, timeout_s: float) -> bytes | None:
        if not self._open:
            raise DeviceDisconnected("replay transport is not open")
        if self._pos < len(self._records) and self._records[self._pos].dir == "in":
            data = self._records[self._pos].data
            self._pos += 1
            return data
        return None


def check_record_path(path: str | os.PathLike[str]) -> Path:
    """Make sure ``path`` can be appended to (creates missing parent directories) and return
    it; for CLI ``--record`` options, so a bad path fails before the device is opened.
    Raises ``OSError``."""
    p = Path(path)
    if p.is_dir():
        raise IsADirectoryError(f"{p} is a directory")
    if p.exists() and not p.is_file():  # FIFO (open would block), device node, socket, ...
        raise OSError(f"{p} is not a regular file")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8"):
        pass
    return p


class RecordingTransport:
    """Wraps another transport and appends every OUT report and every received IN report to a
    JSONL file (format in the module docstring). Timeouts are not recorded. Every record is
    flushed at once (a crash loses nothing); reopening (reconnect) appends to the same file.
    ``info`` is the inner transport's, so ``Scope.open`` still treats a wrapped ``HidTransport``
    as hardware (owner lock, calibration file)."""

    def __init__(self, inner: Transport, path: str | Path) -> None:
        self._inner = inner
        self._path = Path(path)
        self._fh: IO[str] | None = None
        self.info = inner.info

    @property
    def inner(self) -> Transport:
        """The wrapped transport (read-only; e.g. to reach a ``SimTransport``'s device)."""
        return self._inner

    def _log(self, direction: str, data: bytes, note: str = "") -> None:
        if self._fh is None:
            return
        rec: dict[str, Any] = {"t": time.monotonic(), "dir": direction, "hex": data.hex()}
        if note:
            rec["note"] = note
        try:
            self._fh.write(json.dumps(rec) + "\n")
            self._fh.flush()
        except (OSError, ValueError) as exc:
            # a debugging aid must not look like a device I/O failure to the worker (an OSError
            # from the transport = disconnect): stop recording instead
            fh, self._fh = self._fh, None
            with contextlib.suppress(OSError, ValueError):
                fh.close()
            log.warning("recording to %s stopped: %s", self._path, exc)

    def open(self) -> None:
        self._inner.open()
        self.info = self._inner.info
        if self._fh is None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError:
                self._inner.close()
                raise

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        self._inner.close()

    def write(self, payload: bytes) -> None:
        self._inner.write(payload)
        self._log("out", bytes(payload))

    def read(self, timeout_s: float) -> bytes | None:
        data = self._inner.read(timeout_s)
        if data is not None:
            self._log("in", data)
        return data
