"""HID transport on top of the PyPI ``hidapi`` package (import name ``hid``).

All backend differences live here: hidapi wants a leading report-ID byte (0x00, the device has no
report IDs) on writes and returns bare 64-byte reports on reads. OSError -> DeviceDisconnected.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
from typing import Any

from edu09.errors import DeviceAccessError, DeviceDisconnected, ProtocolError, UnsafeOperation
from edu09.protocol import tables
from edu09.transport._hidlib import load_hid
from edu09.transport.base import REPORT_SIZE, TransportInfo
from edu09.transport.discovery import find_device

UDEV_RULE_URL = (
    "https://github.com/BonBear/openedu09/blob/main/packaging/linux/99-velleman-edu09.rules"
)


class HidTransport:
    def __init__(
        self,
        path: str | None = None,
        serial: str | None = None,
        hid_module: Any | None = None,
        allow_unknown_opcodes: bool = False,
    ) -> None:
        self._allow_unknown = allow_unknown_opcodes
        self._path = path
        self._serial = serial
        self._hid = hid_module
        self._dev: Any | None = None
        self.info = TransportInfo(kind="hid", path=path or "", serial=serial)

    def _hid_mod(self) -> Any:
        if self._hid is None:
            self._hid = load_hid()
        return self._hid

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def open(self) -> None:
        if self._dev is not None:
            return
        hid = self._hid_mod()
        dev_info = find_device(self._path, self._serial, hid)  # raises DeviceNotFound
        dev = hid.device()
        try:
            dev.open_path(os.fsencode(dev_info.path))
        except OSError as e:
            hint = "driver detached or in use by another program"
            if (
                sys.platform.startswith("linux")
                and dev_info.path.startswith("/dev/")
                and not os.access(dev_info.path, os.R_OK | os.W_OK)
            ):
                hint = (
                    "no read/write permission; install the udev rule "
                    f"packaging/linux/99-velleman-edu09.rules ({UDEV_RULE_URL}; "
                    "packaging/linux/install-udev.sh installs it) and replug"
                )
            raise DeviceAccessError(f"cannot open {dev_info.path}: {e} ({hint})") from e
        with contextlib.suppress(OSError, AttributeError):
            dev.set_nonblocking(0)
        self._dev = dev
        self.info = TransportInfo(kind="hid", path=dev_info.path, serial=dev_info.serial)

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            with contextlib.suppress(Exception):  # close must never raise
                dev.close()

    def write(self, payload: bytes) -> None:
        if len(payload) != REPORT_SIZE:
            raise ValueError(f"payload must be exactly {REPORT_SIZE} bytes, got {len(payload)}")
        self._check_opcode(payload[0])
        dev = self._require()
        try:
            n = dev.write(b"\x00" + bytes(payload))
        except OSError as e:
            raise DeviceDisconnected(f"write failed: {e}") from e
        if n is None or n < 0:
            raise DeviceDisconnected("write failed (hidapi returned an error)")

    def read(self, timeout_s: float) -> bytes | None:
        dev = self._require()
        # timeout 0 would block forever in hidapi; always use at least 1 ms.
        ms = max(1, math.ceil(timeout_s * 1000))
        try:
            data = dev.read(REPORT_SIZE + 1, ms)
        except OSError as e:
            raise DeviceDisconnected(f"read failed: {e}") from e
        if data is None:
            raise DeviceDisconnected("read failed (hidapi returned an error)")
        if len(data) == 0:
            return None
        report = bytes(data)
        if len(report) == REPORT_SIZE + 1 and report[0] == 0:
            report = report[1:]  # some backends keep the report-ID byte
        if len(report) != REPORT_SIZE:
            raise ProtocolError(f"short/odd report: {len(report)} bytes")
        return report

    def _check_opcode(self, op: int) -> None:
        if self._allow_unknown or op in tables.DRIVER_OPCODES:
            return
        if op == tables.OP_RELEASE and tables.SEND_RELEASE_ON_CLOSE:
            return
        raise UnsafeOperation(f"opcode 0x{op:02X} is not allowed by the transport safety gate")

    def _require(self) -> Any:
        if self._dev is None:
            raise DeviceDisconnected("transport is not open")
        return self._dev
