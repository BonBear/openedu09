"""Device -> host report parsers (protocol.md section 4). Pure; no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from edu09.errors import ProtocolError
from edu09.protocol import tables

Buffer = bytes | bytearray | memoryview


def _check_report(r: Buffer, what: str) -> bytes:
    data = bytes(r)
    if len(data) != tables.REPORT_SIZE:
        raise ProtocolError(f"{what}: expected a {tables.REPORT_SIZE}-byte report, got {len(data)}")
    return data


# --------------------------------------------------------------------------------------------------
# Version (0x0F) - HW-verified: b"1.01\r" followed by uninitialised garbage.
# --------------------------------------------------------------------------------------------------
def _printable(b: int) -> bool:
    return 0x20 <= b < 0x7F


def parse_version(r: Buffer) -> str:
    """Firmware version string from a 0x0F reply (HW: ``31 2E 30 31 0D`` + garbage -> ``"1.01"``).

    The version is the leading run of printable ASCII. If that run is terminated by CR or NUL
    (within ``tables.VERSION_MAX_LENGTH`` bytes) it is taken whole; otherwise (no terminator: the
    garbage may itself be printable) only the first ``tables.VERSION_LENGTH`` bytes are used.
    Trailing spaces are stripped.

    Raises ``ProtocolError`` if the report is shorter than 4 bytes or does not start with a
    printable version (Q8: format verified only for firmware "1.01").
    """
    data = bytes(r)
    if len(data) < tables.VERSION_LENGTH:
        raise ProtocolError(f"version reply too short: {len(data)} bytes")
    window = data[: tables.VERSION_MAX_LENGTH]
    end = next((i for i, b in enumerate(window) if not _printable(b)), len(window))
    terminated = end < len(window) and window[end] in (tables.VERSION_TERMINATOR, 0x00)
    head = window[:end] if terminated else window[: min(end, tables.VERSION_LENGTH)]
    head = head.rstrip(b" ")
    if not head:
        raise ProtocolError(f"version reply is not printable ASCII: {data[:5].hex(' ')}")
    return head.decode("ascii")


# --------------------------------------------------------------------------------------------------
# Status (while armed). HW-verified: 64 C8 mm + zeros = complete (mode mm); waiting = all zeros.
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Status:
    """A parsed status report. Only bytes 0..2 are interpreted; ``raw`` keeps all 64 (Q2)."""

    complete: bool
    mode: int
    raw: bytes = field(repr=False)

    @property
    def flag(self) -> int:
        """Byte 1 as received (0xC8 = complete; other values undocumented, Q2)."""
        return self.raw[tables.STATUS_IDX_FLAG]


def parse_status(r: Buffer) -> Status | None:
    """Parse a ``64 xx mm`` status report; ``None`` if byte 0 is not ``tables.STATUS_MARKER``.

    The HW "waiting" report is all zeros, so it gives ``None`` here; recognise it with
    :func:`is_waiting_report` (kept separate so an all-zero *data* report can never be mistaken
    for a ``Status``; R1).

    Raises ``ProtocolError`` for an empty report or a marker report shorter than 3 bytes.
    """
    data = bytes(r)
    if not data:
        raise ProtocolError("empty report")
    if data[tables.STATUS_IDX_MARKER] != tables.STATUS_MARKER:
        return None
    if len(data) <= tables.STATUS_IDX_MODE:
        raise ProtocolError(f"status report too short: {len(data)} bytes")
    return Status(
        complete=data[tables.STATUS_IDX_FLAG] == tables.STATUS_COMPLETE,
        mode=data[tables.STATUS_IDX_MODE],
        raw=data,
    )


def is_complete_status(r: Buffer, expected_mode: int | None = None) -> bool:
    """True for an exact ``64 C8 mm`` report.

    ``mm`` must equal ``expected_mode`` if given, else be any valid mode.
    """
    if len(r) <= tables.STATUS_IDX_MODE:
        return False
    st = parse_status(r)
    if st is None or not st.complete:
        return False
    if expected_mode is None:
        return st.mode in tables.VALID_MODES
    return st.mode == expected_mode


def is_waiting_report(r: Buffer) -> bool:
    """True for the HW-verified "still waiting" report: ``tables.WAITING_REPORT`` (64 zeros).

    Only meaningful while armed and before 0x0A: after a read-out an all-zero report is frame data
    (raw samples of 0), which ``FrameAssembler`` always keeps.
    """
    return bytes(r) == tables.WAITING_REPORT


# --------------------------------------------------------------------------------------------------
# Frame assembly after 0x0A (protocol.md 4.2, architecture.md R1)
# --------------------------------------------------------------------------------------------------
class FrameAssembler:
    """Collects the 16 data reports that follow a 0x0A read-out into one 1024-byte frame.

    Unlike EDU09.exe (which discards any report whose first byte is 0x64, corrupting frames whose
    first sample is 100), this counts reports. Before the first data report it skips only *exact*
    completion status reports ``64 C8 mm`` (``mm == expected_mode`` if given, else any valid mode);
    at most ``max_leading_status`` of them, then raises ``ProtocolError``. Once data has started
    every report is data.

    Usage::

        asm = FrameAssembler(expected_mode=1)
        while not asm.feed(transport.read(...)):  # read() returns None on timeout -> no progress
            ...
        frame = asm.frame
    """

    def __init__(self, expected_mode: int | None = None, max_leading_status: int = 64) -> None:
        self.expected_mode = expected_mode
        self.max_leading_status = max_leading_status
        self._buf = bytearray()
        self._reports = 0
        self._skipped = 0

    def reset(self) -> None:
        """Discard any partial frame."""
        self._buf.clear()
        self._reports = 0
        self._skipped = 0

    @property
    def done(self) -> bool:
        return self._reports == tables.REPORTS_PER_FRAME

    @property
    def reports_received(self) -> int:
        """Number of data reports collected so far (0..16)."""
        return self._reports

    @property
    def skipped_status(self) -> int:
        """Number of leading status reports skipped."""
        return self._skipped

    def feed(self, r: Buffer | None) -> bool:
        """Add one IN report. Returns True when the frame is complete.

        ``None`` (a read timeout) is no progress: returns False, state unchanged.

        Raises ``ProtocolError`` on a wrong report length, too many leading status reports, or a
        feed after completion.
        """
        if r is None:
            return False
        if self.done:
            raise ProtocolError("frame already complete; call reset() before feeding more reports")
        data = _check_report(r, "frame report")
        if self._reports == 0 and is_complete_status(data, self.expected_mode):
            self._skipped += 1
            if self._skipped > self.max_leading_status:
                raise ProtocolError(
                    f"more than {self.max_leading_status} status reports before frame data"
                )
            return False
        self._buf += data
        self._reports += 1
        return self.done

    @property
    def frame(self) -> bytes:
        """The assembled 1024-byte frame in wire order. Raises ``ProtocolError`` if incomplete."""
        if not self.done:
            raise ProtocolError(
                f"frame incomplete: {self._reports}/{tables.REPORTS_PER_FRAME} reports received"
            )
        return bytes(self._buf)
