"""Physical front-end models of the simulator (UNIT1 vs docs/data), ``trim_gain``, SimReference."""

from __future__ import annotations

import collections
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from edu09.calwizard.simref import SimReference
from edu09.device import Scope
from edu09.protocol import tables
from edu09.transport.replay import RecordingTransport, ReplayTransport
from edu09.transport.sim import PHYSICAL_UNITS, UNIT1, SignalSpec, SimTransport
from edu09.units import Calibration, volts_to_deflection

DATA = Path(__file__).resolve().parents[2] / "docs" / "data" / "edu09_gain_sweep_unit1.json"


def _sweep() -> dict[str, object]:
    out: dict[str, object] = json.loads(DATA.read_text(encoding="utf-8"))
    return out


def _neg_groups() -> list[tuple[float, float, float]]:
    """(vdiv, div_below_ground, mean counts/div over positions) of the negative sweep."""
    neg = _sweep()["negative_counts_per_div_by_deflection"]
    assert isinstance(neg, dict)
    rows: list[tuple[float, float, float]] = []
    for key, entries in neg.items():
        groups: dict[float, list[float]] = collections.defaultdict(list)
        for e in entries:
            groups[e["div_below_ground"]].append(e["counts_per_div"])
        rows += [(float(key), d, float(np.mean(c))) for d, c in sorted(groups.items())]
    return rows


def test_physical_units_registry() -> None:
    assert set(PHYSICAL_UNITS) == {"ideal", "unit1"}
    assert PHYSICAL_UNITS["ideal"] == Calibration()
    assert PHYSICAL_UNITS["unit1"] is UNIT1
    assert UNIT1.base_ofs == Calibration.typical().base_ofs
    assert UNIT1.gnd_fit == Calibration.typical().gnd_fit


def test_unit1_gains_match_the_typical_gain_table() -> None:
    """``tables.TYPICAL_RANGE_GAIN`` (rounded, documentation / ``--uncalibrated typical``) and the
    simulator's UNIT1 positive gains describe the same measured unit."""
    assert tables.TYPICAL_RANGE_GAIN == {
        0.1: 0.987,
        0.2: 0.975,
        0.5: 0.974,
        1.0: 0.996,
        2.0: 0.981,
        5.0: 0.979,
    }
    assert UNIT1.range_gain.keys() == tables.TYPICAL_RANGE_GAIN.keys()
    for vdiv, g in tables.TYPICAL_RANGE_GAIN.items():
        assert UNIT1.range_gain[vdiv] == pytest.approx(g, abs=1e-3), vdiv


def test_unit1_model_matches_sweep_positive() -> None:
    pos = _sweep()["positive_counts_per_div"]
    assert isinstance(pos, dict)
    for key, cpd in pos.items():
        vdiv = float(key)
        model = volts_to_deflection(4 * vdiv, vdiv, UNIT1) / 4
        assert model == pytest.approx(cpd, abs=0.01), key


@pytest.mark.parametrize(("vdiv", "div", "measured"), _neg_groups())
def test_unit1_model_matches_sweep_negative(vdiv: float, div: float, measured: float) -> None:
    secant = -volts_to_deflection(-div * vdiv, vdiv, UNIT1) / div
    # near ground the data is dominated by the ~1 count ground accuracy (1 div: 1 count/div)
    tol = 0.1 if div >= 2.4 else 0.15 if div >= 1.0 else 0.35
    assert secant == pytest.approx(measured, abs=tol)


# -- end to end through the simulated device -------------------------------------------------


@pytest.fixture
def unit1_scope() -> Iterator[tuple[Scope, SimTransport]]:
    t = SimTransport.virtual("dc:0v", seed=5)
    t.device.calibration = UNIT1
    # host: the same offsets, no gain correction (counts are what we look at)
    with Scope.open(t, calibration=UNIT1.without_gain_correction()) as sc:
        yield sc, t


def _mean_raw(sc: Scope, t: SimTransport, volts: float, vdiv: float) -> float:
    t.device.signal = SignalSpec(kind="dc", offset_v=volts, noise_v=vdiv / 32)  # 1 LSB dither
    return float(np.mean([sc.acquire().raw[:625].mean() for _ in range(3)]))


@pytest.mark.parametrize(
    ("vdiv", "div", "pos", "expect"),
    [
        (0.5, -6.0, 3.0, 34.73),  # docs/data: 34.71..34.75
        (0.5, -5.0, 3.0, 33.55),
        (0.5, -3.0, 1.5, 33.0),
        (0.2, -5.0, 3.0, 33.56),
        (1.0, -3.0, 1.5, 32 / 0.9892),
        (0.1, 4.0, -2.0, 32.42),
        (0.5, 4.0, -2.0, 32.85),
        (5.0, 4.0, -2.0, 32.69),
    ],
)
def test_sim_unit1_secants(
    unit1_scope: tuple[Scope, SimTransport], vdiv: float, div: float, pos: float, expect: float
) -> None:
    sc, t = unit1_scope
    sc.configure(volts_per_div=vdiv, position_div=pos, timebase="5ms", trigger_mode="none")
    ground = _mean_raw(sc, t, 0.0, vdiv)
    level = _mean_raw(sc, t, div * vdiv, vdiv)
    secant = (level - ground) / div
    assert secant == pytest.approx(expect, abs=0.1)


def test_trim_gain_scales_input(unit1_scope: tuple[Scope, SimTransport]) -> None:
    sc, t = unit1_scope
    sc.configure(volts_per_div=1.0, position_div=-2.0, timebase="5ms", trigger_mode="none")
    assert t.device.trim_gain == 1.0
    ground = _mean_raw(sc, t, 0.0, 1.0)
    plain = _mean_raw(sc, t, 4.0, 1.0) - ground
    t.device.trim_gain = 1.05
    trimmed = _mean_raw(sc, t, 4.0, 1.0) - ground
    assert trimmed / plain == pytest.approx(1.05, abs=0.003)
    assert _mean_raw(sc, t, 0.0, 1.0) == pytest.approx(ground, abs=0.1)  # 0 V stays 0 V


# -- SimReference ----------------------------------------------------------------------------


def test_sim_reference_connect_disconnect() -> None:
    t = SimTransport.virtual("sine:1khz:2vpp")
    ref = SimReference(t.device, noise_v=0.004, source_error_pct=2.0, meter_error_pct=-1.0)
    reading = ref.connect(-2.5)
    sig = t.device.signal
    assert sig.kind == "dc" and sig.offset_v == pytest.approx(-2.55) and sig.noise_v == 0.004
    assert ref.applied_v == pytest.approx(-2.55)
    assert reading == round(-2.55 * 0.99, 3)
    assert SimReference(t.device).connect(1.23456) == 1.235  # 3 decimals
    ref.disconnect()
    assert t.device.signal.offset_v == 0.0 and t.device.signal.noise_v == 0.004
    assert ref.applied_v is None


@pytest.mark.parametrize(
    "kw",
    [
        {"noise_v": -0.1},
        {"noise_v": float("nan")},
        {"meter_error_pct": 60.0},
        {"source_error_pct": float("inf")},
    ],
)
def test_sim_reference_validation(kw: dict[str, float]) -> None:
    t = SimTransport.virtual()
    with pytest.raises(ValueError):
        SimReference(t.device, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SimReference(t.device).connect(float("nan"))
    with pytest.raises(TypeError):
        SimReference(object())  # type: ignore[arg-type]


def test_sim_reference_for_scope(tmp_path: Path) -> None:
    t = SimTransport.virtual()
    ref = SimReference.for_scope(SimpleNamespace(transport=t), noise_v=0.0)
    assert ref is not None and ref.device is t.device and ref.noise_v == 0.0
    rec = RecordingTransport(t, tmp_path / "rec.jsonl")
    assert rec.inner is t
    wrapped = SimReference.for_scope(SimpleNamespace(transport=rec))
    assert wrapped is not None and wrapped.device is t.device
    assert SimReference.for_scope(SimpleNamespace(transport=ReplayTransport([]))) is None
    assert SimReference.for_scope(object()) is None
    with Scope.open(SimTransport.virtual()) as sc:
        real = SimReference.for_scope(sc)
        assert real is not None
