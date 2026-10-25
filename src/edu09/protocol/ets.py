"""ETS (equivalent-time sampling) sample reordering (protocol.md 4.3).

In ETS modes the device returns ``n`` sub-records of ``L = len / n`` samples back to back. The
time-ordered output is ``out[n*i + k] = in[i + L*k]``:

- mode 1 (realtime): n = 1, no reordering;
- mode 2 (50 us/div): n = 8, L = 128;
- modes 3/4 (20/10 us/div): n = 16, L = 64.

The mode is the acquisition mode byte reported in the status report, not the requested timebase.
"""

from __future__ import annotations

from edu09.errors import ProtocolError
from edu09.protocol import tables


def _subrecords(mode: int, length: int) -> int:
    try:
        n = tables.ETS_SUBRECORDS[mode]
    except KeyError:
        raise ProtocolError(
            f"unknown acquisition mode {mode}; expected one of {sorted(tables.ETS_SUBRECORDS)}"
        ) from None
    if length % n:
        raise ValueError(
            f"frame length {length} is not a multiple of {n} sub-records (mode {mode})"
        )
    return n


def deinterleave(mode: int, raw: bytes | bytearray | memoryview) -> bytes:
    """Reorder a frame as received from the device into time order.

    Raises ``ProtocolError`` for an unknown mode and ``ValueError`` if the length does not split
    evenly into the mode's sub-records.
    """
    data = bytes(raw)
    n = _subrecords(mode, len(data))
    if n == 1:
        return data
    sub_len = len(data) // n
    out = bytearray(len(data))
    for k in range(n):
        out[k::n] = data[k * sub_len : (k + 1) * sub_len]
    return bytes(out)


def interleave(mode: int, samples: bytes | bytearray | memoryview) -> bytes:
    """Inverse of ``deinterleave``: time order -> device wire order (used by the simulator)."""
    data = bytes(samples)
    n = _subrecords(mode, len(data))
    if n == 1:
        return data
    sub_len = len(data) // n
    out = bytearray(len(data))
    for k in range(n):
        out[k * sub_len : (k + 1) * sub_len] = data[k::n]
    return bytes(out)
