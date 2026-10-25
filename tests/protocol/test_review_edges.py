"""Reviewer edge-case tests: Table B vs protocol.md, ETS round-trip, FrameAssembler R1 limits."""

from __future__ import annotations

import random

import pytest

from edu09.protocol import tables
from edu09.protocol.ets import deinterleave, interleave
from edu09.protocol.reports import FrameAssembler

# protocol.md 3.2 Table B: (s/div, hw code, exported sample rate, status mode). The DSOLink export
# truncates to int; tables keeps the exact 31.25 samples/div value.
TABLE_B = [
    (500e-3, 0x01, 62, 1),
    (200e-3, 0x02, 156, 1),
    (100e-3, 0x03, 312, 1),
    (50e-3, 0x04, 625, 1),
    (20e-3, 0x05, 1_562, 1),
    (10e-3, 0x06, 3_125, 1),
    (5e-3, 0x07, 6_250, 1),
    (2e-3, 0x08, 15_625, 1),
    (1e-3, 0x09, 31_250, 1),
    (500e-6, 0x0A, 62_500, 1),
    (200e-6, 0x0B, 156_250, 1),
    (100e-6, 0x0B, 156_250, 1),
    (50e-6, 0x0C, 625_000, 2),
    (20e-6, 0x0D, 1_562_500, 3),
    (10e-6, 0x10, 1_562_500, 4),
]


@pytest.mark.parametrize(("s_per_div", "hw", "exported", "mode"), TABLE_B)
def test_timebase_table_matches_protocol_md(
    s_per_div: float, hw: int, exported: int, mode: int
) -> None:
    e = tables.TIMEBASES[s_per_div]
    assert e.hw_code == hw
    assert e.mode == mode
    assert int(e.sample_rate_hz) == exported  # DSOLink exports int(rate)
    assert e.sample_rate_hz - exported < 1.0


def test_zoomed_timebases_share_the_sample_rate_of_their_hw_setting() -> None:
    # 0.1 ms/div is the 0.2 ms/div hardware setting with 2x display zoom: same samples, same dt.
    assert tables.TIMEBASES[100e-6].dt == tables.TIMEBASES[200e-6].dt
    # 10 us/div: exported rate equals 20 us/div (unverified, Q10), although HW code/mode differ.
    assert tables.TIMEBASES[10e-6].dt == tables.TIMEBASES[20e-6].dt


@pytest.mark.parametrize("mode", sorted(tables.VALID_MODES))
def test_ets_random_roundtrip_both_directions(mode: int) -> None:
    rnd = random.Random(mode)
    data = bytes(rnd.randrange(256) for _ in range(tables.FRAME_SIZE))
    assert deinterleave(mode, interleave(mode, data)) == data
    assert interleave(mode, deinterleave(mode, data)) == data


def test_ets_mode2_first_sub_record_is_every_8th_output() -> None:
    wire = bytes(range(128)) * 8  # sub-record k holds i = 0..127
    out = deinterleave(tables.MODE_ETS8, wire)
    assert out[0:8] == bytes([0] * 8)  # out[8*0 + k] = in[0 + 128*k] = 0
    assert out[8:16] == bytes([1] * 8)


def _status(mode: int = 1) -> bytes:
    return bytes((0x64, 0xC8, mode)) + bytes(61)


def test_frame_assembler_max_leading_status_boundary() -> None:
    asm = FrameAssembler(expected_mode=1, max_leading_status=2)
    assert asm.feed(_status()) is False
    assert asm.feed(_status()) is False
    for i in range(15):
        assert asm.feed(bytes([i + 1]) * 64) is False
    assert asm.feed(bytes(64)) is True
    assert asm.skipped_status == 2


def test_frame_assembler_unknown_mode_status_is_data_without_expected_mode() -> None:
    asm = FrameAssembler()  # any *valid* mode is skipped; mode 9 is not valid -> data
    asm.feed(_status(9))
    assert asm.reports_received == 1


def test_frame_assembler_residual_r1_ambiguity_is_documented() -> None:
    # A first data report that literally starts 64 C8 <mode> is indistinguishable from a status
    # report and is skipped (residual R1 risk, much narrower than EDU09.exe's 0x64-only test).
    asm = FrameAssembler(expected_mode=1)
    assert asm.feed(_status(1)) is False
    assert asm.reports_received == 0
