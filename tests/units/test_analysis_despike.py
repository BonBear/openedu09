"""analysis.despike: opt-in removal of isolated one-sample outliers (H1/H2 ETS frames)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from edu09 import analysis
from edu09.protocol import FrameAssembler, deinterleave, tables
from edu09.transport.replay import load_records

H1 = Path(__file__).resolve().parents[1] / "fixtures" / "h1"


def _frames(name: str, mode: int) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    asm: FrameAssembler | None = None
    for r in load_records(H1 / name):
        if r.dir == "out" and r.data[0] == tables.OP_READOUT:
            asm = FrameAssembler(expected_mode=mode)
        elif r.dir == "in" and asm is not None and asm.feed(r.data):
            raw = np.frombuffer(deinterleave(mode, asm.frame), dtype=np.uint8)
            frames.append(raw.astype(np.float64))
            asm = None
    return frames


def test_isolated_spikes_replaced_by_median_of_three() -> None:
    x = np.array([0, 0, 9, 0, 0, -7, 0, 1, 2, 3], dtype=float)
    y = analysis.despike(x)
    assert y.tolist() == [0, 0, 0, 0, 0, 0, 0, 1, 2, 3]
    assert x[2] == 9  # input untouched


def test_steps_edges_and_endpoints_preserved() -> None:
    x = np.array([5, 0, 0, 0, 4, 4, 4, 2, 2, 9], dtype=float)
    assert analysis.despike(x).tolist() == x.tolist()


def test_threshold_and_nan() -> None:
    x = np.array([0, 2, 0, 10, 0, np.nan, 7, 0], dtype=float)
    y = analysis.despike(x, threshold=3)
    assert y[1] == 2 and y[3] == 0  # small bump kept, big spike removed
    assert np.isnan(y[5]) and y[6] == 7  # NaN neighbourhood left alone
    assert analysis.despike([1.0, 2.0]).tolist() == [1.0, 2.0]
    with pytest.raises(ValueError):
        analysis.despike(x, threshold=-1)
    with pytest.raises(ValueError):
        analysis.despike(np.zeros((2, 3)))


@pytest.mark.parametrize(
    ("name", "mode", "rate"),
    [
        ("square_trig_10us_rise.jsonl", 4, 1_562_500.0),
        ("square_trig_20us_rise.jsonl", 3, 1_562_500.0),
        ("square_trig_50us_rise.jsonl", 2, 625_000.0),
    ],
)
def test_despike_fixes_ets_frequency_on_hw_frames(name: str, mode: int, rate: float) -> None:
    for x in _frames(name, mode):
        f = analysis.measure(analysis.despike(x, threshold=5), 1 / rate, ["frequency"])
        assert f["frequency"].value == pytest.approx(29_900, rel=0.005)


def test_despike_is_wrong_for_undersampled_realtime_frames() -> None:
    """Why it is not applied by default: at 200 us/div (~5 samples per period) it corrupts."""
    rate = 156_250.0
    for x in _frames("square_trig_200us_rise.jsonl", 1):
        raw = analysis.measure(x, 1 / rate, ["frequency"])["frequency"].value
        assert raw == pytest.approx(30_000, rel=0.005)
        bad = analysis.measure(analysis.despike(x, 5), 1 / rate, ["frequency"])["frequency"].value
        assert bad is not None and abs(bad - 30_000) > 1_000
