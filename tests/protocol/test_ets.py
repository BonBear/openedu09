"""Tests for ETS de-interleave / interleave."""

from __future__ import annotations

import random

import pytest

from edu09.errors import ProtocolError
from edu09.protocol.ets import deinterleave, interleave

FRAME = bytes(random.Random(1234).randrange(256) for _ in range(1024))


def test_realtime_is_identity() -> None:
    assert deinterleave(1, FRAME) == FRAME
    assert interleave(1, FRAME) == FRAME


def test_mode2_formula() -> None:
    out = deinterleave(2, FRAME)
    for i in range(128):
        for k in range(8):
            assert out[8 * i + k] == FRAME[i + 128 * k]


@pytest.mark.parametrize("mode", [3, 4])
def test_mode3_4_formula(mode: int) -> None:
    out = deinterleave(mode, FRAME)
    for i in range(64):
        for k in range(16):
            assert out[16 * i + k] == FRAME[i + 64 * k]


@pytest.mark.parametrize("mode", [1, 2, 3, 4])
def test_roundtrip(mode: int) -> None:
    assert deinterleave(mode, interleave(mode, FRAME)) == FRAME
    assert interleave(mode, deinterleave(mode, FRAME)) == FRAME


def test_ramp_mode2() -> None:
    # A time-ordered ramp interleaved for the wire: sub-record k holds samples k, k+8, k+16, ...
    ramp = bytes(i & 0xFF for i in range(1024))
    wire = interleave(2, ramp)
    assert wire[:4] == bytes([0, 8, 16, 24])
    assert wire[128:132] == bytes([1, 9, 17, 25])
    assert deinterleave(2, wire) == ramp


def test_accepts_buffer_types() -> None:
    assert deinterleave(2, bytearray(FRAME)) == deinterleave(2, memoryview(FRAME))


@pytest.mark.parametrize("mode", [0, 5, 255])
def test_unknown_mode(mode: int) -> None:
    with pytest.raises(ProtocolError):
        deinterleave(mode, FRAME)
    with pytest.raises(ProtocolError):
        interleave(mode, FRAME)


def test_bad_length() -> None:
    with pytest.raises(ValueError):
        deinterleave(3, FRAME[:1000])
