"""Fixtures for the hardware tests. Every test here is marked ``hardware`` (skipped without
``--hardware`` / ``EDU09_HW=1``). A missing device, missing permissions or a device owned by
another process (GUI/MCP) skips instead of failing."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hw_helpers import SignalSpec, signal_from_env

from edu09 import storage
from edu09.device.scope import Scope
from edu09.errors import DeviceAccessError, DeviceBusy, DeviceNotFound
from edu09.model import Settings

# Computed at import (collection), before the per-test platformdirs isolation: the real saved
# calibration is used read-only by the hardware tests.
REAL_CALIBRATION = storage.default_calibration_path()


def open_scope_or_skip(**kw: object) -> Scope:
    try:
        return Scope.open(calibration=REAL_CALIBRATION, open_timeout_s=10.0, **kw)  # type: ignore[arg-type]
    except DeviceBusy as exc:
        if exc.owner is not None and exc.owner.pid == os.getpid():
            pytest.fail(f"this test process already owns the device (a Scope left open?): {exc}")
        pytest.skip(f"device is owned by another process, close the GUI/MCP server first: {exc}")
    except DeviceNotFound as exc:
        pytest.skip(f"no EDU09 attached: {exc}")
    except DeviceAccessError as exc:
        pytest.skip(f"cannot access the device (udev rule / permissions?): {exc}")


@pytest.fixture(scope="module")
def scope() -> Iterator[Scope]:
    s = open_scope_or_skip()
    try:
        yield s
    finally:
        try:
            s.stop()
        finally:
            s.close()


@pytest.fixture(autouse=True)
def _default_settings(request: pytest.FixtureRequest) -> None:
    """Start each test from known settings (only for tests that use the shared scope)."""
    if "scope" in request.fixturenames:
        s: Scope = request.getfixturevalue("scope")
        s.stop()
        s.set_settings(Settings())


@pytest.fixture(scope="session")
def signal() -> SignalSpec:
    spec = signal_from_env()
    if spec is None:
        pytest.skip('no signal configured: set EDU09_HW_SIGNAL="square:30khz:0..4.16v"')
    return spec
