"""Enumerate attached EDU09 devices."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from edu09.errors import DeviceNotFound
from edu09.transport._hidlib import load_hid
from edu09.transport.base import USB_PID, USB_VID


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    serial: str | None
    product: str | None
    manufacturer: str | None
    vid: int = USB_VID
    pid: int = USB_PID
    interface: int = -1


def _decode_path(p: bytes | str) -> str:
    return os.fsdecode(p) if isinstance(p, bytes) else p


def list_devices(hid_module: Any | None = None) -> list[DeviceInfo]:
    """All attached EDU09 devices (VID 0x10CF, PID 0xED90). Empty list if none or hid fails."""
    hid = load_hid(hid_module)
    try:
        entries = hid.enumerate(USB_VID, USB_PID)
    except OSError:
        return []
    out: list[DeviceInfo] = []
    for e in entries:
        if e.get("vendor_id", USB_VID) != USB_VID or e.get("product_id", USB_PID) != USB_PID:
            continue
        out.append(
            DeviceInfo(
                path=_decode_path(e["path"]),
                serial=e.get("serial_number") or None,
                product=e.get("product_string") or None,
                manufacturer=e.get("manufacturer_string") or None,
                vid=USB_VID,
                pid=USB_PID,
                interface=int(e.get("interface_number", -1)),
            )
        )
    return out


def find_device(
    path: str | None = None, serial: str | None = None, hid_module: Any | None = None
) -> DeviceInfo:
    """First device matching path and/or serial. Raises DeviceNotFound."""
    for d in list_devices(hid_module):
        if path is not None and d.path != path:
            continue
        if serial is not None and d.serial != serial:
            continue
        return d
    what = " ".join(
        x for x in (f"path={path}" if path else "", f"serial={serial}" if serial else "")
    )
    raise DeviceNotFound(f"no EDU09 device found{' (' + what + ')' if what else ''}")
