"""Transport protocol: the byte pipe between the worker and a device (real, simulated, replayed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from edu09.protocol.tables import REPORT_SIZE, USB_PID, USB_VID

__all__ = ["REPORT_SIZE", "USB_PID", "USB_VID", "Transport", "TransportInfo"]


@dataclass(frozen=True)
class TransportInfo:
    """Static description of a transport. ``kind`` is "hid", "sim" or "replay"."""

    kind: str
    path: str = ""
    serial: str | None = None


@runtime_checkable
class Transport(Protocol):
    """One 64-byte-report pipe. Not thread-safe; only the worker thread may call it."""

    info: TransportInfo

    def open(self) -> None:
        """Open the device. Raises DeviceNotFound / DeviceBusy."""
        ...

    def close(self) -> None:
        """Close the device. Idempotent, never raises."""
        ...

    def write(self, payload: bytes) -> None:
        """Send exactly 64 bytes (no report-ID byte). Raises DeviceDisconnected."""
        ...

    def read(self, timeout_s: float) -> bytes | None:
        """Return one 64-byte report, or None on timeout. Raises DeviceDisconnected."""
        ...
