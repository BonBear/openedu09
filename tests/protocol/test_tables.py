"""Consistency checks for edu09.protocol.tables (data invariants, not hardware truth)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import edu09.protocol as protocol_pkg
from edu09.protocol import tables


def test_framing() -> None:
    assert tables.REPORT_SIZE == 64
    assert tables.FRAME_SIZE == 1024
    assert tables.REPORTS_PER_FRAME == 16


def test_safety_sets() -> None:
    assert {0x08, 0x09, 0x0A, 0x0B, 0x0D, 0x0E, 0x0F} == tables.DRIVER_OPCODES
    assert 0x0C not in tables.DRIVER_OPCODES
    assert 0x14 not in tables.DRIVER_OPCODES
    assert tables.OPCODE_CLASS[0x0F] == "read"
    acquire = {op for op, c in tables.OPCODE_CLASS.items() if c == "acquire"}
    assert acquire == {0x08, 0x09, 0x0A, 0x0B, 0x0D, 0x0E}
    assert tables.OPCODE_CLASS[0x0C] == "unknown"
    assert tables.OPCODE_CLASS[0x14] == "unknown"
    assert tables.SEND_RELEASE_ON_CLOSE is False


def test_gain_tables() -> None:
    expected = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
    assert sorted(tables.GAIN_CODES_CH1) == expected
    assert sorted(tables.GAIN_CODES_CH2) == expected
    assert tables.GAIN_CODES_CH1[0.5] == 0x01
    assert tables.FULL_SCALE_MV[0.1] == 800
    assert tables.FULL_SCALE_MV[5.0] == 40000
    # Gain codes must not collide with the CH1 AC coupling bit.
    assert all(not code & tables.CH1_AC_BIT for code in tables.GAIN_CODES_CH1.values())
    assert len(set(tables.GAIN_CODES_CH1.values())) == 6


def test_timebase_table() -> None:
    keys = sorted(tables.TIMEBASES)
    assert len(keys) == 15
    assert keys[0] == pytest.approx(10e-6)
    assert keys[-1] == pytest.approx(500e-3)
    # Keys must match model-style literals exactly (different spellings of the same float).
    assert tables.TIMEBASES[0.002].hw_code == 0x08
    assert tables.TIMEBASES[0.0001].sw_zoom == 2
    for key, e in tables.TIMEBASES.items():
        assert key == e.s_per_div
        assert e.mode in tables.VALID_MODES
        assert 0 <= e.hw_code <= 0xFF
        assert e.dt == pytest.approx(1 / e.sample_rate_hz)
        if not e.is_ets:
            # Realtime: 31.25 samples/div, scaled by the software zoom.
            assert e.sample_rate_hz * e.s_per_div * e.sw_zoom == pytest.approx(
                tables.SAMPLES_PER_DIV
            )
    assert tables.TIMEBASES[50e-6].mode == tables.MODE_ETS8
    assert tables.TIMEBASES[20e-6].mode == tables.MODE_ETS16
    assert tables.TIMEBASES[10e-6].mode == tables.MODE_ETS16_ZOOM
    assert tables.DEFAULT_TIMEBASE in tables.TIMEBASES


def test_neutral_ch2_and_flags() -> None:
    assert tables.CH2_NEUTRAL_CFG == 0x29
    assert tables.CH2_NEUTRAL_OFS == 0x80
    bits = (
        tables.FLAG_TRIG_SRC_CH2,
        tables.FLAG_TRIG_ENABLE,
        tables.FLAG_EDGE_FALLING,
        tables.FLAG_DIGITAL,
    )
    combined = 0
    for b in bits:
        assert combined & b == 0
        combined |= b
    assert combined == tables.FLAGS_VALID_MASK


def test_status_constants() -> None:
    assert tables.STATUS_MARKER == 0x64
    assert tables.STATUS_COMPLETE == 0xC8
    assert tables.STATUS_POLL in ("passive", "resend_arm")
    assert tables.TRIGGER_INDEX == 255  # HW-verified realtime (Q4)
    assert tables.TRIGGER_INDEX_BY_MODE == {1: 255, 2: 248, 3: 253, 4: 253}
    assert set(tables.TRIGGER_INDEX_BY_MODE) == tables.VALID_MODES


def test_protocol_imports_only_stdlib_and_allowed() -> None:
    """Architecture rule: protocol imports only the stdlib, edu09.errors and edu09.protocol."""
    pkg_dir = Path(protocol_pkg.__file__).parent
    for path in pkg_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top == "edu09":
                    assert name == "edu09.errors" or name.startswith("edu09.protocol"), (
                        path.name,
                        name,
                    )
                else:
                    assert top in {"__future__", "dataclasses", "typing"}, (path.name, name)


def test_trigger_byte_default_matches_edu09_exe() -> None:
    assert tables.TRIGGER_BYTE_DEFAULT == 127


def test_timebase_samples_and_divisions_per_frame() -> None:
    for tb in (2e-3, 200e-6, 50e-6, 20e-6):
        e = tables.TIMEBASES[tb]
        assert e.samples_per_div == pytest.approx(31.25 * e.sw_zoom)
        assert e.divisions_per_frame == pytest.approx(32.768)
    for tb in (100e-6, 10e-6):
        e = tables.TIMEBASES[tb]
        assert e.sw_zoom == 2
        assert e.divisions_per_frame == pytest.approx(65.536)
