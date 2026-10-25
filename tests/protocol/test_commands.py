"""Byte-exact tests for edu09.protocol.commands."""

from __future__ import annotations

import pytest

from edu09.errors import UnsafeOperation
from edu09.protocol import tables
from edu09.protocol.commands import (
    HwConfig,
    decode_config,
    encode_arm,
    encode_config,
    encode_live,
    encode_readout,
    encode_release,
    encode_reset,
    encode_simple,
    encode_stop,
    encode_version_query,
)


def _padded(prefix: str) -> bytes:
    b = bytes.fromhex(prefix)
    return b + bytes(64 - len(b))


@pytest.mark.parametrize(
    ("fn", "op"),
    [
        (encode_version_query, 0x0F),
        (encode_reset, 0x09),
        (encode_arm, 0x0B),
        (encode_readout, 0x0A),
        (encode_stop, 0x08),
        (encode_live, 0x0D),
    ],
)
def test_simple_encoders(fn: object, op: int) -> None:
    assert callable(fn)
    out = fn()
    assert isinstance(out, bytes)
    assert len(out) == tables.REPORT_SIZE
    assert out[0] == op
    assert out[1:] == bytes(63)
    assert encode_simple(op) == out


def test_version_query_matches_hw_verified_bytes() -> None:
    # HW-verified 2026-10-24: write 00 0F + zero padding -> reply "1.01".
    assert encode_version_query() == _padded("0f")


@pytest.mark.parametrize("op", [0x0C, 0x14, 0x00, 0x01, 0x10, 0xFF, 0x100, -1])
def test_encode_simple_rejects_unsafe_opcodes(op: int) -> None:
    with pytest.raises(UnsafeOperation):
        encode_simple(op)


def test_encode_simple_rejects_config_without_payload() -> None:
    with pytest.raises(UnsafeOperation):
        encode_simple(0x0E)


def test_release_gated_off() -> None:
    assert tables.SEND_RELEASE_ON_CLOSE is False
    with pytest.raises(UnsafeOperation):
        encode_release()


def test_release_when_gate_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tables, "SEND_RELEASE_ON_CLOSE", True)
    assert encode_release() == _padded("14")


def test_encode_config_protocol_md_10_3_fixture() -> None:
    # protocol.md 10.3: CH1 1 V/div DC, offset 0x80, trigger mid, 2 ms/div, trigger on, rising.
    cfg = HwConfig(gain1=0x0A, gain2=0x04, ofs1=0x80, ofs2=0x80, trig=0x80, tb=0x08, flags=0x02)
    assert encode_config(cfg) == _padded("0e 80 07 0a 04 80 80 80 08 02")


def test_encode_config_positional_field_order() -> None:
    cfg = HwConfig(0x0A, 0x04, 0x80, 0x80, 0x80, 0x08, 0x02)
    assert encode_config(cfg) == _padded("0e 80 07 0a 04 80 80 80 08 02")


def test_hwconfig_defaults_are_startup_state() -> None:
    cfg = HwConfig()
    assert cfg.gain1 == tables.GAIN_CODES_CH1[2.0] == 0x02
    assert cfg.gain2 == tables.CH2_NEUTRAL_CFG == 0x29
    assert (cfg.ofs1, cfg.ofs2, cfg.trig) == (0x80, 0x80, 0x7F)
    assert cfg.tb == 0x08
    assert cfg.flags == tables.FLAG_TRIG_ENABLE
    assert encode_config(cfg) == _padded("0e 80 07 02 29 80 80 7f 08 02")


def test_encode_config_with_flags_and_ac() -> None:
    cfg = HwConfig(
        gain1=tables.GAIN_CODES_CH1[0.5] | tables.CH1_AC_BIT,
        tb=tables.TIMEBASES[50e-6].hw_code,
        trig=0x10,
        ofs1=0x7F,
        flags=tables.FLAG_TRIG_ENABLE | tables.FLAG_EDGE_FALLING,
    )
    assert encode_config(cfg) == _padded("0e 80 07 05 29 7f 80 10 0c 06")


@pytest.mark.parametrize("field", ["gain1", "gain2", "ofs1", "ofs2", "trig", "tb", "flags"])
@pytest.mark.parametrize("value", [-1, 256])
def test_hwconfig_rejects_out_of_range(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        HwConfig(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1.0, "1", True, None])
def test_hwconfig_rejects_non_int(value: object) -> None:
    with pytest.raises(TypeError):
        HwConfig(trig=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("flags", [0x10, 0x20, 0x80, 0xF2])
def test_hwconfig_rejects_garbage_flag_bits(flags: int) -> None:
    with pytest.raises(ValueError, match="flags"):
        HwConfig(flags=flags)


def test_hwconfig_is_frozen() -> None:
    cfg = HwConfig()
    with pytest.raises(AttributeError):
        cfg.trig = 1  # type: ignore[misc]


def test_decode_config_roundtrip() -> None:
    cfg = HwConfig(gain1=0x0C, gain2=0x10, ofs1=1, ofs2=2, trig=3, tb=0x10, flags=0x0F)
    assert decode_config(encode_config(cfg)) == cfg


@pytest.mark.parametrize("payload", [_padded("0f"), _padded("0e 81 07"), bytes.fromhex("0e8007")])
def test_decode_config_rejects_non_config(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_config(payload)
