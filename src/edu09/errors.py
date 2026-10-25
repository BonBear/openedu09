"""Exception hierarchy. Every error raised by edu09 derives from Edu09Error."""

from __future__ import annotations

from typing import Any


class Edu09Error(Exception):
    """Base class for all edu09 errors."""


class DeviceNotFound(Edu09Error):
    """No EDU09 device was found (or the requested path/serial does not exist)."""


class DeviceBusy(Edu09Error):
    """The device is already owned by another process or Scope instance.

    ``owner`` is the owner description when known (``edu09.device.lock.OwnerInfo``), else None.
    """

    def __init__(self, message: str = "", owner: Any = None) -> None:
        super().__init__(message)
        self.owner = owner


class DeviceAccessError(Edu09Error):
    """The device exists but cannot be opened (permissions/udev rule, driver detach)."""


class DeviceDisconnected(Edu09Error):
    """The device was unplugged or the transport failed during I/O."""


class ProtocolError(Edu09Error):
    """Unexpected, malformed or out-of-sequence data from the device."""


class AcquisitionTimeout(Edu09Error):
    """An acquisition did not complete (for example no trigger) in time."""


class Cancelled(Edu09Error):
    """The operation was cancelled before completion."""


class JobAborted(Cancelled):
    """A long exclusive device operation (calibration, live capture) was aborted
    (``Scope.abort_exclusive``, an ``abort_event``, or ``Scope.close``)."""


class CalibrationError(Edu09Error):
    """Calibration data is invalid or the calibration procedure failed."""


class UnsafeOperation(Edu09Error):
    """The requested opcode or operation is not allowed by the safety policy."""
