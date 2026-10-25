"""Per-frame ETS trigger-edge detection (worker) and the simulator's ``ets_trigger_offset``.

HW (2026-10-25): the ETS trigger position in the de-interleaved frame varies by a few samples
between sessions (+4 vs the H1 table), so the worker detects the edge per triggered ETS frame.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest

from edu09 import units
from edu09.device.scope import Scope
from edu09.device.worker import ETS_TRIGGER_SEARCH, detect_trigger_index
from edu09.model import Coupling, Settings, Timebase, TriggerEdge
from edu09.protocol import tables
from edu09.transport.sim import SimTransport, SimulatedDevice, VirtualClock

SQUARE = "square:30khz:4vpp:offset=2v:duty=63%"
ETS_TBS = [Timebase.US50, Timebase.US20, Timebase.US10]


def _open(signal: str) -> tuple[SimTransport, Scope]:
    sim = SimTransport.virtual(signal, seed=3)
    return sim, Scope.open(sim, reconnect_interval_s=0.01)


@pytest.fixture
def square() -> Iterator[tuple[SimTransport, Scope]]:
    sim, scope = _open(SQUARE)
    try:
        yield sim, scope
    finally:
        scope.close()


def _configure(scope: Scope, tb: Timebase, edge: str, level: float = 2.0) -> None:
    scope.configure(
        volts_per_div=2,
        position_div=0,
        coupling="dc",
        timebase=tb,
        trigger_mode="normal",
        trigger_edge=edge,
        trigger_level_v=level,
    )


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("tb", ETS_TBS, ids=lambda t: t.label)
@pytest.mark.parametrize("offset", range(-6, 7))
def test_worker_finds_shifted_ets_edge(
    square: tuple[SimTransport, Scope], tb: Timebase, edge: str, offset: int
) -> None:
    sim, scope = square
    sim.device.ets_trigger_offset = offset
    _configure(scope, tb, edge)
    table = units.trigger_index(tb)
    wf = scope.acquire(timeout_s=2.0)
    assert wf.triggered
    assert wf.trigger_index == table + offset
    i = wf.trigger_index
    if edge == "rising":
        assert wf.volts[i - 1] < 2.0 <= wf.volts[i]
    else:
        assert wf.volts[i - 1] > 2.0 >= wf.volts[i]


def test_no_crossing_keeps_table_index() -> None:
    # 5 kHz at 50 us/div: 125 samples per period. Trigger crossing forced to index 100, so the
    # next rising edge is ~225 and nothing rises within 248 +- 8.
    clock = VirtualClock()
    dev = SimulatedDevice(SQUARE.replace("30khz", "5khz"), seed=3, clock=clock, trigger_index=100)
    sim = SimTransport(device=dev, sleep=clock.sleep)
    with Scope.open(sim, reconnect_interval_s=0.01) as scope:
        _configure(scope, Timebase.US50, "rising")
        wf = scope.acquire(timeout_s=2.0)
    table = units.trigger_index(Timebase.US50)
    assert wf.raw[100] > wf.raw[99]  # the real edge is where the sim put it
    assert wf.trigger_index == table


def test_realtime_keeps_table_index(square: tuple[SimTransport, Scope]) -> None:
    sim, scope = square
    sim.device.ets_trigger_offset = 4  # ETS only: realtime frames are not shifted
    assert sim.device.trigger_index_for(tables.MODE_REALTIME) == tables.TRIGGER_INDEX
    sim.device.trigger_index = tables.TRIGGER_INDEX + 3  # a real shift is NOT detected
    _configure(scope, Timebase.US200, "rising")
    wf = scope.acquire(timeout_s=2.0)
    assert wf.trigger_index == units.trigger_index(Timebase.US200)


def test_sim_ets_offset_only_affects_ets_modes() -> None:
    dev = SimulatedDevice("dc:0v", ets_trigger_offset=-5)
    assert dev.trigger_index_for(tables.MODE_REALTIME) == tables.TRIGGER_INDEX_BY_MODE[1]
    for mode in (tables.MODE_ETS8, tables.MODE_ETS16, tables.MODE_ETS16_ZOOM):
        assert dev.trigger_index_for(mode) == tables.TRIGGER_INDEX_BY_MODE[mode] - 5
    with pytest.raises(ValueError, match="ets_trigger_offset"):
        SimulatedDevice("dc:0v", ets_trigger_offset=100)


# --------------------------------------------------------------------------------------------
# detect_trigger_index (pure)
# --------------------------------------------------------------------------------------------
def _settings(edge: TriggerEdge = TriggerEdge.RISING, level: float = 0.0) -> Settings:
    s = Settings().replace(
        volts_per_div=1, coupling="dc", timebase=Timebase.US20, trigger_mode="normal"
    )
    return s.replace(trigger_edge=edge.value, trigger_level_v=level)


def _level(s: Settings, cal: units.Calibration) -> int:
    b = units.trigger_byte(s.trigger, s.ch1, cal)
    return tables.ADC_MAX - b if tables.TRIGGER_BYTE_INVERTED else b


def _step(n: int, at: int, lo: int, hi: int) -> np.ndarray:
    raw = np.full(n, lo, dtype=np.uint8)
    raw[at:] = hi
    return raw


def test_detect_picks_crossing_closest_to_table() -> None:
    cal = units.Calibration()
    s = _settings()
    lvl = _level(s, cal)
    raw = np.full(1024, lvl - 20, dtype=np.uint8)
    for edge_at in (245, 258):  # two rising edges in the window: 258 is closer to 253
        raw[edge_at : edge_at + 3] = lvl + 20
    assert detect_trigger_index(raw, s, cal, 253) == 258
    assert detect_trigger_index(raw, s, cal, 248) == 245


def test_detect_exact_level_counts_as_past() -> None:
    cal = units.Calibration()
    s = _settings()
    lvl = _level(s, cal)
    raw = _step(1024, 250, lvl - 10, lvl)
    assert detect_trigger_index(raw, s, cal, 253) == 250


def test_detect_falling_and_inverted_adc() -> None:
    cal = units.Calibration()
    s = _settings(TriggerEdge.FALLING)
    lvl = _level(s, cal)
    falling = _step(1024, 256, lvl + 10, lvl - 10)
    rising = _step(1024, 256, lvl - 10, lvl + 10)
    assert detect_trigger_index(falling, s, cal, 253) == 256
    assert detect_trigger_index(rising, s, cal, 253) is None
    inv = replace(cal, adc_inverted=True)
    lvl_inv = _level(s, inv)
    # Inverted ADC: a falling input voltage edge is a rising raw edge.
    assert detect_trigger_index(_step(1024, 250, lvl_inv - 10, lvl_inv + 10), s, inv, 253) == 250


def test_detect_window_limit_and_gnd() -> None:
    cal = units.Calibration()
    s = _settings()
    lvl = _level(s, cal)
    table = 253
    inside = _step(1024, table + ETS_TRIGGER_SEARCH, lvl - 10, lvl + 10)
    outside = _step(1024, table + ETS_TRIGGER_SEARCH + 1, lvl - 10, lvl + 10)
    assert detect_trigger_index(inside, s, cal, table) == table + ETS_TRIGGER_SEARCH
    assert detect_trigger_index(outside, s, cal, table) is None
    gnd = s.replace(coupling=Coupling.GND.value)
    assert detect_trigger_index(inside, gnd, cal, table) is None
