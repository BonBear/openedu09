"""Hardware: discovery, version, open/close, inter-process owner lock. No input signal needed."""

from __future__ import annotations

import os

import pytest

from conftest import open_scope_or_skip
from edu09.device.lock import OwnerLock
from edu09.device.scope import Scope
from edu09.errors import DeviceBusy
from edu09.transport import list_devices

pytestmark = pytest.mark.hardware


def test_discovery_lists_the_device() -> None:
    devs = list_devices()
    if not devs:
        pytest.skip("no EDU09 attached (VID 0x10CF PID 0xED90)")
    assert devs[0].vid == 0x10CF
    assert devs[0].pid == 0xED90


def test_version_is_1_01() -> None:
    # Own Scope, not the module-scoped ``scope`` fixture: while that one is open, this process
    # owns the device and the open/lock tests below could not open it again.
    s = open_scope_or_skip()
    try:
        assert s.read_version() == "1.01"
        assert s.firmware == "1.01"
        assert s.connected
    finally:
        s.close()


def test_open_close_reopen() -> None:
    s = open_scope_or_skip()
    try:
        assert s.connected
        assert not s.closed
    finally:
        s.close()
    assert s.closed
    s2 = open_scope_or_skip()  # the lock and the HID handle were released
    s2.close()


def test_owner_lock_excludes_second_owner() -> None:
    s = open_scope_or_skip()
    try:
        with pytest.raises(DeviceBusy) as ei:
            OwnerLock("other").acquire()
        owner = ei.value.owner
        assert owner is not None
        assert owner.pid == os.getpid()
        with pytest.raises(DeviceBusy):
            Scope.open()
    finally:
        s.close()
    lock = OwnerLock("other")
    lock.acquire()  # free again after close
    lock.release()
