"""H1 part 2: a ~30 kHz 0..4.16 V square (555, ~63 % high) on CH1, DC, 2 V/div, offset 0x80.

Each capture is replayed through ReplayTransport -> FrameAssembler -> deinterleave -> units ->
analysis. Captures hold 1..3 acquisitions; trigger byte 165 (~2.1 V), rising unless "fall".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from edu09.analysis import measure
from edu09.model import ChannelSettings, Coupling, VoltsPerDiv
from edu09.protocol import (
    FrameAssembler,
    decode_config,
    deinterleave,
    is_complete_status,
    is_waiting_report,
    tables,
)
from edu09.transport import ReplayTransport
from edu09.transport.replay import load_records
from edu09.units import Calibration, counts_to_volts

H1 = Path(__file__).resolve().parents[1] / "fixtures" / "h1"
CH = ChannelSettings(VoltsPerDiv.parse(2.0), Coupling.DC, 0.0)
SIGNAL_HZ = 30_000.0
SIGNAL_VPP = 4.1

CAPTURES = {
    "square_freerun_200us_2v.jsonl": (200e-6, None),
    "square_trig_200us_rise.jsonl": (200e-6, "rise"),
    "square_trig_200us_fall.jsonl": (200e-6, "fall"),
    "square_trig_50us_rise.jsonl": (50e-6, "rise"),
    "square_trig_20us_rise.jsonl": (20e-6, "rise"),
    "square_trig_10us_rise.jsonl": (10e-6, "rise"),
}


@dataclass
class Acquisition:
    raw: npt.NDArray[np.uint8]  # time-ordered (de-interleaved)
    mode: int
    arm_to_complete_s: float
    leading_status: int
    pre_status: list[bytes] = field(default_factory=list)  # reports after 0x0B before 64 C8
    pre_status_t: list[float] = field(default_factory=list)


def _acquisitions(name: str) -> tuple[bytes, list[Acquisition]]:
    """Drive the capture through ReplayTransport like a host would; return config + frames."""
    recs = load_records(H1 / name)
    t = ReplayTransport(H1 / name, strict=True)
    t.open()
    cfg = recs[0].data
    tb = next(e for e in tables.TIMEBASES.values() if e.hw_code == cfg[tables.CFG_IDX_TB])
    out: list[Acquisition] = []
    arm_t = done_t = 0.0
    pre: list[bytes] = []
    pre_t: list[float] = []
    asm: FrameAssembler | None = None
    for rec in recs:
        if rec.dir == "out":
            t.write(rec.data)
            op = rec.data[0]
            if op == tables.OP_ARM:
                arm_t, pre, pre_t = rec.t, [], []
            elif op == tables.OP_READOUT:
                asm = FrameAssembler(expected_mode=tb.mode)
            continue
        r = t.read(0.1)
        assert r == rec.data
        if asm is None:
            if is_complete_status(r, tb.mode):
                done_t = rec.t
            else:
                pre.append(r)
                pre_t.append(rec.t - arm_t)
            continue
        if asm.feed(r):
            raw = np.frombuffer(deinterleave(tb.mode, asm.frame), dtype=np.uint8)
            out.append(Acquisition(raw, tb.mode, done_t - arm_t, asm.skipped_status, pre, pre_t))
            asm = None
    assert t.exhausted
    return cfg, out


def _median5(v: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Remove the isolated out-of-place samples ETS frames contain (a few per frame)."""
    padded = np.pad(v, 2, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)


def _crossings(raw: npt.NDArray[np.uint8], level: int, falling: bool) -> npt.NDArray[np.intp]:
    """Indices of the first sample past ``level`` on each rising (or falling) edge."""
    a = raw.astype(int)
    if falling:
        return np.flatnonzero((a[:-1] > level) & (a[1:] <= level)) + 1
    return np.flatnonzero((a[:-1] < level) & (a[1:] >= level)) + 1


@pytest.mark.parametrize("name", sorted(CAPTURES))
def test_square_frequency_and_amplitude(name: str) -> None:
    timebase, _ = CAPTURES[name]
    tb = tables.TIMEBASES[timebase]
    cfg, acqs = _acquisitions(name)
    assert acqs
    for acq in acqs:
        assert acq.mode == tb.mode  # status mode byte == Table B mode (HW-verified)
        volts = counts_to_volts(acq.raw, CH, Calibration())
        if tb.is_ets:  # realtime has only ~5 samples/period: never filter it
            volts = _median5(volts)
        m = measure(volts, tb.dt, ["frequency", "vpp"])
        assert m["frequency"].value == pytest.approx(SIGNAL_HZ, rel=0.02), name
        assert m["vpp"].value == pytest.approx(SIGNAL_VPP, rel=0.10), name
        # Q3: higher voltage -> higher raw count. 0 V ~ raw 132, 4.16 V ~ raw 197.
        lo, hi = np.percentile(acq.raw, [5, 95])
        assert 130 <= lo <= 134 and 195 <= hi <= 200


@pytest.mark.parametrize("name", sorted(n for n, (_, edge) in CAPTURES.items() if edge is not None))
def test_trigger_edge_at_per_mode_index(name: str) -> None:
    timebase, edge = CAPTURES[name]
    tb = tables.TIMEBASES[timebase]
    cfg, acqs = _acquisitions(name)
    hw = decode_config(cfg)
    falling = edge == "fall"
    assert bool(hw.flags & tables.FLAG_TRIG_ENABLE)
    assert bool(hw.flags & tables.FLAG_EDGE_FALLING) == falling
    assert hw.trig == 165
    ti = tables.TRIGGER_INDEX_BY_MODE[tb.mode]
    for acq in acqs:
        near = [i for i in _crossings(acq.raw, hw.trig, falling) if abs(i - ti) <= 2]
        assert near, f"{name}: no {edge} edge within 2 samples of index {ti}"
        if not falling:
            assert not [i for i in _crossings(acq.raw, hw.trig, True) if abs(i - ti) <= 1]
        # triggered captures complete quickly: 7-9 ms realtime, 17-44 ms ETS (observed)
        assert 0.005 <= acq.arm_to_complete_s <= (0.05 if tb.is_ets else 0.02)


def test_falling_capture_is_falling_at_the_index() -> None:
    _, acqs = _acquisitions("square_trig_200us_fall.jsonl")
    ti = tables.TRIGGER_INDEX_BY_MODE[tables.MODE_REALTIME]
    for acq in acqs:
        raw = acq.raw.astype(int)
        assert raw[ti - 1] > 165 >= raw[ti]


def test_ets_waiting_reports_are_fast_and_trailing_report_shows_up_after_next_arm() -> None:
    for name in ("square_trig_50us_rise.jsonl", "square_trig_20us_rise.jsonl"):
        _, acqs = _acquisitions(name)
        first = acqs[0]
        assert first.pre_status and all(is_waiting_report(r) for r in first.pre_status)
        gaps = np.diff([0.0, *first.pre_status_t])
        assert gaps.max() < 0.005  # ETS: ~2 ms, not the 33 ms realtime cadence
        assert np.mean(gaps) == pytest.approx(tables.STATUS_INTERVAL_ETS_S, abs=0.0015)
        for later in acqs[1:]:
            # the previous read-out's trailing report arrives right after the next 0x0B
            stray = [r for r in later.pre_status if not is_waiting_report(r)]
            assert len(stray) == tables.READOUT_TRAILING_REPORTS
            assert not is_waiting_report(later.pre_status[0])


def test_leading_status_count_varies() -> None:
    counts = {a.leading_status for n in CAPTURES for a in _acquisitions(n)[1]}
    assert counts == {1, 2, 3}
