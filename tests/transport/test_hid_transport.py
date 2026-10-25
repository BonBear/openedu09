from __future__ import annotations

from typing import Any

import pytest

from edu09.errors import (
    DeviceAccessError,
    DeviceDisconnected,
    DeviceNotFound,
    ProtocolError,
    UnsafeOperation,
)
from edu09.transport import HidTransport, find_device, list_devices
from edu09.transport.base import Transport

ENTRY = {
    "path": b"/dev/hidraw3",
    "vendor_id": 0x10CF,
    "product_id": 0xED90,
    "serial_number": "ABC",
    "product_string": "EDU09 Oscilloscope Experiment Board",
    "manufacturer_string": "Velleman Instruments",
    "interface_number": 0,
}


class FakeDevice:
    def __init__(self, owner: FakeHid) -> None:
        self.o = owner

    def open_path(self, path: bytes) -> None:
        self.o.opened.append(path)
        if self.o.open_error:
            raise OSError("open failed")

    def set_nonblocking(self, v: int) -> None:
        self.o.nonblocking = v

    def write(self, data: bytes) -> int:
        if self.o.io_error:
            raise OSError("gone")
        self.o.writes.append(bytes(data))
        return self.o.write_ret if self.o.write_ret is not None else len(data)

    def read(self, n: int, ms: int) -> Any:
        self.o.read_args.append((n, ms))
        if self.o.io_error:
            raise OSError("gone")
        return self.o.reads.pop(0) if self.o.reads else []

    def close(self) -> None:
        self.o.closed += 1


class FakeHid:
    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.entries = [ENTRY] if entries is None else entries
        self.opened: list[bytes] = []
        self.writes: list[bytes] = []
        self.reads: list[Any] = []
        self.read_args: list[tuple[int, int]] = []
        self.closed = 0
        self.nonblocking: int | None = None
        self.open_error = False
        self.io_error = False
        self.write_ret: int | None = None

    def enumerate(self, vid: int, pid: int) -> list[dict[str, Any]]:
        assert (vid, pid) == (0x10CF, 0xED90)
        return self.entries

    def device(self) -> FakeDevice:
        return FakeDevice(self)


def test_list_and_find() -> None:
    h = FakeHid()
    devs = list_devices(h)
    assert len(devs) == 1 and devs[0].path == "/dev/hidraw3" and devs[0].serial == "ABC"
    assert find_device(serial="ABC", hid_module=h).path == "/dev/hidraw3"
    with pytest.raises(DeviceNotFound):
        find_device(serial="nope", hid_module=h)
    assert list_devices(FakeHid([])) == []


def test_open_no_device() -> None:
    with pytest.raises(DeviceNotFound):
        HidTransport(hid_module=FakeHid([])).open()


def test_open_failure_is_access_error() -> None:
    h = FakeHid()
    h.open_error = True
    with pytest.raises(DeviceAccessError):
        HidTransport(hid_module=h).open()


def test_write_prefixes_report_id() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h)
    assert isinstance(t, Transport)
    t.open()
    assert h.opened == [b"/dev/hidraw3"] and h.nonblocking == 0
    assert t.info.path == "/dev/hidraw3" and t.info.serial == "ABC"
    payload = bytes([0x0F]) + bytes(63)
    t.write(payload)
    assert h.writes == [b"\x00" + payload] and len(h.writes[0]) == 65


def test_write_wrong_size() -> None:
    t = HidTransport(hid_module=FakeHid())
    t.open()
    with pytest.raises(ValueError):
        t.write(b"\x0f")


def test_read_paths() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h)
    t.open()
    assert t.read(0.2) is None  # empty list = timeout
    assert h.read_args[-1][1] == 200
    h.reads.append(list(range(64)))
    assert t.read(0.0) == bytes(range(64))
    assert h.read_args[-1][1] >= 1  # never block forever
    h.reads.append([0, *range(64)])  # backend keeping the report-ID byte
    assert t.read(0.1) == bytes(range(64))
    h.reads.append([1, 2, 3])
    with pytest.raises(ProtocolError):
        t.read(0.1)


def test_oserror_maps_to_disconnected() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h)
    t.open()
    h.io_error = True
    with pytest.raises(DeviceDisconnected):
        t.write(bytes([0x0F]) + bytes(63))
    with pytest.raises(DeviceDisconnected):
        t.read(0.1)


def test_write_error_return() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h)
    t.open()
    h.write_ret = -1
    with pytest.raises(DeviceDisconnected):
        t.write(bytes([0x0F]) + bytes(63))


def test_close_idempotent_and_not_open() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h)
    with pytest.raises(DeviceDisconnected):
        t.write(bytes([0x0F]) + bytes(63))
    t.open()
    t.close()
    t.close()
    assert h.closed == 1
    with pytest.raises(DeviceDisconnected):
        t.read(0.1)


def test_opcode_gate() -> None:
    from edu09.protocol import tables

    h = FakeHid()
    t = HidTransport(hid_module=h)
    t.open()
    for op in (0x0C, 0x14, 0x00, 0x77):
        with pytest.raises(UnsafeOperation):
            t.write(bytes([op]) + bytes(63))
    assert h.writes == []
    t.write(bytes([tables.OP_CONFIG, 0x80, 0x07]) + bytes(61))  # config passes
    assert len(h.writes) == 1


def test_opcode_gate_release_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from edu09.protocol import tables

    h = FakeHid()
    t = HidTransport(hid_module=h)
    t.open()
    monkeypatch.setattr(tables, "SEND_RELEASE_ON_CLOSE", True)
    t.write(bytes([tables.OP_RELEASE]) + bytes(63))
    assert len(h.writes) == 1


def test_allow_unknown_opcodes() -> None:
    h = FakeHid()
    t = HidTransport(hid_module=h, allow_unknown_opcodes=True)
    t.open()
    t.write(bytes([0x0C]) + bytes(63))
    assert len(h.writes) == 1


def test_load_hid_injection_and_linux_preference() -> None:
    import sys

    from edu09.transport._hidlib import load_hid

    sentinel = object()
    assert load_hid(sentinel) is sentinel
    if sys.platform.startswith("linux"):
        pytest.importorskip("hidraw")
        assert load_hid().__name__ == "hidraw"
