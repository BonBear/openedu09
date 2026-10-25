"""Tests for edu09.model: enum parsing, settings validation, replace(), dicts, Waveform."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest

from edu09.model import (
    SETTINGS_KEYS,
    ChannelSettings,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    TriggerSettings,
    VoltsPerDiv,
    Waveform,
)

# -- enums ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.5, VoltsPerDiv.V0_5),
        (0.503, VoltsPerDiv.V0_5),  # within 1 %
        (2, VoltsPerDiv.V2),
        ("0.5", VoltsPerDiv.V0_5),
        ("500mV", VoltsPerDiv.V0_5),
        ("100 mV", VoltsPerDiv.V0_1),
        ("2 V", VoltsPerDiv.V2),
        ("5V/div", VoltsPerDiv.V5),
        ("V0_2", VoltsPerDiv.V0_2),
        ("v1", VoltsPerDiv.V1),
        (VoltsPerDiv.V1, VoltsPerDiv.V1),
    ],
)
def test_volts_per_div_parse(value: object, expected: VoltsPerDiv) -> None:
    assert VoltsPerDiv.parse(value) is expected


@pytest.mark.parametrize("value", [0.3, 0.52, 10, -1, 0, "abc", "2 A", True, None, float("nan")])
def test_volts_per_div_parse_rejects(value: object) -> None:
    with pytest.raises(ValueError, match=r"allowed: 0\.1, 0\.2, 0\.5, 1, 2, 5 V/div"):
        VoltsPerDiv.parse(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2ms", Timebase.MS2),
        ("2 ms", Timebase.MS2),
        (0.002, Timebase.MS2),
        ("0.002", Timebase.MS2),
        ("500us", Timebase.US500),
        ("500µs", Timebase.US500),
        ("500μs", Timebase.US500),
        ("0.5ms", Timebase.US500),
        ("0.5s", Timebase.MS500),
        ("10us/div", Timebase.US10),
        ("MS2", Timebase.MS2),
        ("us50", Timebase.US50),
        (1.005e-3, Timebase.MS1),
    ],
)
def test_timebase_parse(value: object, expected: Timebase) -> None:
    assert Timebase.parse(value) is expected


@pytest.mark.parametrize("value", ["3ms", 1.0, "5us", "2 hours", 0, False, [1]])
def test_timebase_parse_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="allowed: 500ms, 200ms"):
        Timebase.parse(value)


def test_timebase_labels_roundtrip_and_ets() -> None:
    assert len(Timebase) == 15
    for tb in Timebase:
        assert Timebase.parse(tb.label) is tb
        assert Timebase.parse(tb.seconds) is tb
    assert [tb for tb in Timebase if tb.is_ets] == [Timebase.US50, Timebase.US20, Timebase.US10]
    assert Timebase.MS2.label == "2ms"
    assert Timebase.US50.label == "50us"


def test_volts_per_div_props() -> None:
    assert VoltsPerDiv.V0_1.full_scale_v == pytest.approx(0.8)
    assert VoltsPerDiv.V0_5.label == "500mV"
    assert VoltsPerDiv.V2.label == "2V"


def test_str_enum_parse() -> None:
    assert Coupling.parse("AC") is Coupling.AC
    assert Coupling.parse("ground") is Coupling.GND
    assert TriggerEdge.parse("up") is TriggerEdge.RISING
    assert TriggerEdge.parse("Falling") is TriggerEdge.FALLING
    assert TriggerMode.parse("off") is TriggerMode.NONE
    assert TriggerMode.parse("NORMAL") is TriggerMode.NORMAL
    with pytest.raises(ValueError, match="allowed: 'ac', 'dc', 'gnd'"):
        Coupling.parse("xx")
    with pytest.raises(ValueError, match="trigger_mode"):
        TriggerMode.parse(3)


# -- settings ---------------------------------------------------------------------------------


def test_defaults() -> None:
    s = Settings()
    assert s.ch1 == ChannelSettings(VoltsPerDiv.V2, Coupling.DC, 0.0)
    assert s.timebase is Timebase.MS2
    assert s.trigger == TriggerSettings(TriggerMode.AUTO, TriggerEdge.RISING, 0.0, 0.1)
    assert hash(s) == hash(Settings())


def test_channel_coerces_and_validates() -> None:
    ch = ChannelSettings(volts_per_div=0.5, coupling="ac", position_div=1)  # type: ignore[arg-type]
    assert ch.volts_per_div is VoltsPerDiv.V0_5
    assert ch.coupling is Coupling.AC
    assert isinstance(ch.position_div, float)
    for bad in (4.01, -5, float("inf"), "x"):
        with pytest.raises(ValueError, match="position_div"):
            ChannelSettings(position_div=bad)  # type: ignore[arg-type]


def test_trigger_validates() -> None:
    with pytest.raises(ValueError, match="auto_timeout_s"):
        TriggerSettings(auto_timeout_s=0)
    with pytest.raises(ValueError, match="level_v"):
        TriggerSettings(level_v=float("nan"))


def test_level_range_depends_on_vdiv_and_position() -> None:
    ch = ChannelSettings(volts_per_div=VoltsPerDiv.V1, position_div=1.0)
    assert ch.level_range_v() == pytest.approx((-5.0, 3.0))
    Settings(ch1=ch, trigger=TriggerSettings(level_v=3.0))
    with pytest.raises(ValueError, match="out of range"):
        Settings(ch1=ch, trigger=TriggerSettings(level_v=3.5))


def test_ets_requires_trigger_in_constructor() -> None:
    with pytest.raises(ValueError, match="equivalent-time"):
        Settings(timebase=Timebase.US20, trigger=TriggerSettings(mode=TriggerMode.NONE))
    Settings(timebase=Timebase.US20, trigger=TriggerSettings(mode=TriggerMode.NORMAL))


def test_replace_flat_keys() -> None:
    s = Settings().replace(
        volts_per_div="1V",
        coupling="ac",
        position_div=-1.5,
        timebase="500us",
        trigger_mode="normal",
        trigger_edge="falling",
        trigger_level_v=0.25,
        trigger_auto_timeout_s=0.5,
    )
    assert s.ch1 == ChannelSettings(VoltsPerDiv.V1, Coupling.AC, -1.5)
    assert s.timebase is Timebase.US500
    assert s.trigger == TriggerSettings(TriggerMode.NORMAL, TriggerEdge.FALLING, 0.25, 0.5)


def test_replace_none_means_unchanged_and_unknown_raises() -> None:
    s = Settings()
    assert s.replace(volts_per_div=None, timebase=None) == s
    with pytest.raises(ValueError, match="unknown setting"):
        s.replace(level_v=1.0)


def test_replace_is_atomic() -> None:
    s = Settings()
    with pytest.raises(ValueError):
        s.replace(volts_per_div=1.0, timebase="7ms")
    assert s == Settings()


def test_replace_ets_coerces_mode_with_warning() -> None:
    s = Settings().replace(trigger_mode="none")
    s2, warnings = s.replace_with_warnings(timebase="50us")
    assert s2.trigger.mode is TriggerMode.AUTO
    assert len(warnings) == 1 and "trigger" in warnings[0]
    s3, w3 = s2.replace_with_warnings(trigger_mode="none")
    assert s3.trigger.mode is TriggerMode.AUTO and w3


def test_replace_clamps_implicit_level_but_rejects_explicit() -> None:
    s = Settings().replace(trigger_level_v=6.0)  # 2 V/div -> +/-8 V window
    s2, warnings = s.replace_with_warnings(volts_per_div=0.5)  # window +/-2 V
    assert s2.trigger.level_v == pytest.approx(2.0)
    assert warnings and "clamped" in warnings[0]
    with pytest.raises(ValueError, match="trigger_level_v"):
        s.replace(volts_per_div=0.5, trigger_level_v=3.0)


def test_dict_roundtrip_is_json_friendly() -> None:
    s = Settings().replace(
        volts_per_div=0.2, coupling="gnd", timebase="20us", trigger_edge="falling"
    )
    d = json.loads(json.dumps(s.to_dict()))
    assert Settings.from_dict(d) == s
    assert d["timebase"] == pytest.approx(20e-6)
    assert d["ch1"]["coupling"] == "gnd"
    flat = s.to_flat_dict()
    assert set(flat) == set(SETTINGS_KEYS)
    assert Settings().replace(**flat) == s


def test_from_dict_defaults_and_errors() -> None:
    assert Settings.from_dict({}) == Settings()
    assert Settings.from_dict({"timebase": "1ms"}).timebase is Timebase.MS1
    with pytest.raises(ValueError, match="unknown settings key"):
        Settings.from_dict({"ch2": {}})
    with pytest.raises(ValueError, match="unknown ch1 key"):
        Settings.from_dict({"ch1": {"probe": 10}})
    with pytest.raises(ValueError, match="volts_per_div"):
        Settings.from_dict({"ch1": {"volts_per_div": 0.3}})


# -- waveform ---------------------------------------------------------------------------------


def _wf(raw: np.ndarray, **kw: object) -> Waveform:
    args: dict[str, object] = {
        "seq": 7,
        "timestamp": datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        "raw": raw,
        "volts": raw.astype(np.float64) / 10,
        "dt": 1e-4,
        "trigger_index": 258,
        "settings": Settings(),
        "calibration_id": "default:abc",
    }
    args.update(kw)
    return Waveform(**args)  # type: ignore[arg-type]


def test_waveform_basic() -> None:
    raw = np.full(1024, 100, dtype=np.uint8)
    wf = _wf(raw)
    assert wf.waveform_id == "w7"
    assert wf.n == 1024
    assert wf.sample_rate_hz == pytest.approx(1e4)
    assert wf.duration_s == pytest.approx(0.1024)
    assert not wf.clipped
    t = wf.t()
    assert t[258] == 0.0
    assert t[0] == pytest.approx(-258e-4) == wf.t0
    assert t[1] - t[0] == pytest.approx(1e-4)


def test_waveform_arrays_are_readonly_copies() -> None:
    raw = np.full(1024, 100, dtype=np.uint8)
    wf = _wf(raw)
    raw[0] = 1
    assert wf.raw[0] == 100
    assert not wf.raw.flags.writeable and not wf.volts.flags.writeable
    with pytest.raises(ValueError):
        wf.raw[0] = 5
    assert wf.raw.dtype == np.uint8 and wf.volts.dtype == np.float64


@pytest.mark.parametrize("edge", [0, 255])
def test_waveform_clipped(edge: int) -> None:
    raw = np.full(1024, 100, dtype=np.uint8)
    raw[10] = edge
    assert _wf(raw).clipped


def test_waveform_validation_and_naive_timestamp() -> None:
    raw = np.zeros(16, dtype=np.uint8) + 5
    wf = _wf(raw, trigger_index=None, timestamp=datetime(2026, 1, 1))
    assert wf.timestamp.tzinfo is UTC
    assert wf.t()[0] == 0.0
    with pytest.raises(ValueError, match="lengths differ"):
        _wf(raw, volts=np.zeros(8))
    with pytest.raises(ValueError, match="dt"):
        _wf(raw, dt=0.0)
    with pytest.raises(ValueError, match="trigger_index"):
        _wf(raw, trigger_index=16)
    with pytest.raises(ValueError, match="1-D"):
        _wf(np.zeros((2, 8), dtype=np.uint8), volts=np.zeros((2, 8)))
