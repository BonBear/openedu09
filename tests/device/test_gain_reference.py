"""Per-range gain on the simulated front end: round trip, ``gain_from_reference`` and its CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import storage, units
from edu09.device import Scope, measure_calibration
from edu09.device import calibration as calmod
from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, VoltsPerDiv
from edu09.transport.sim import SignalSpec, SimTransport

GAINS = {0.1: 0.987, 0.2: 0.975, 0.5: 0.974, 1.0: 0.996, 2.0: 0.981, 5.0: 0.979}
# physical front end: offsets like the default model, but reads high by 1/g like the real unit
PHYSICAL = units.Calibration(range_gain=GAINS)


def _open(
    signal: str,
    physical: units.Calibration = PHYSICAL,
    host: units.Calibration | None = None,
) -> Scope:
    t = SimTransport.virtual(signal, seed=3)
    t.device.calibration = physical
    return Scope.open(t, calibration=host, reconnect_interval_s=0.01)


def _mean_volts(scope: Scope, **settings: Any) -> float:
    scope.configure(timebase="5ms", trigger_mode="none", **settings)
    return float(np.mean([scope.acquire().volts[:625].mean() for _ in range(3)]))


@pytest.mark.parametrize(
    ("vdiv", "volts", "pos"),
    [(0.5, 1.5, -1.5), (0.1, 0.45, -2.0), (2.0, 9.0, -2.5), (5.0, -12.0, 1.0), (1.0, 2.5, 0.0)],
)
def test_host_with_same_gain_recovers_volts(vdiv: float, volts: float, pos: float) -> None:
    with _open(f"dc:{volts}v:noise=15mv", host=PHYSICAL) as sc:
        lsb = vdiv / 32
        got = _mean_volts(sc, volts_per_div=vdiv, position_div=pos)
        assert got == pytest.approx(volts, abs=0.5 * lsb)
        # without the gain the host reads high by 1/g, as the hardware did
        sc.set_calibration(units.Calibration())
        high = _mean_volts(sc, volts_per_div=vdiv, position_div=pos)
        assert high == pytest.approx(volts / GAINS[vdiv], abs=0.5 * lsb)


def test_trigger_level_with_gain_triggers_at_true_volts() -> None:
    # the trigger byte sits 32/g counts per division above ground: the triggering sample is at
    # the requested TRUE level (a gain-free byte would trigger ~52 mV = 3.4 LSB too low)
    with _open("sine:100hz:3vpp:offset=1v:noise=1mv", host=PHYSICAL) as sc:
        sc.configure(volts_per_div=0.5, position_div=-1.5, timebase="200us")
        sc.configure(trigger_mode="normal", trigger_level_v=2.0)
        for _ in range(3):
            wf = sc.acquire(timeout_s=2.0)
            assert wf.triggered
            assert wf.trigger_index is not None
            v = wf.volts
            ti = wf.trigger_index
            assert v[ti] == pytest.approx(2.0, abs=0.025)
            assert v[ti - 3] < 2.0 < v[ti + 3]


@pytest.mark.parametrize(("vdiv", "volts"), [(0.5, 1.5), (0.1, 0.42), (2.0, 9.0), (5.0, 20.0)])
def test_gain_from_reference_measures_physical_gain(vdiv: float, volts: float) -> None:
    host = units.Calibration(range_gain={vdiv: 1.1})  # existing gain must be ignored
    with _open(f"dc:{volts}v:noise=15mv", host=host) as sc:
        g = calmod.gain_from_reference(sc, volts, vdiv)
        assert g == pytest.approx(GAINS[vdiv], abs=0.003)
        assert sc.calibration == host  # unchanged
        assert sc.settings.ch1.position_div == 0.0  # user settings restored


def test_gain_from_reference_explicit_position_and_frames() -> None:
    with _open("dc:1.5v:noise=15mv") as sc:
        g = calmod.gain_from_reference(sc, 1.5, "500mV", position_div=-3.0, frames=2)
        assert g == pytest.approx(0.974, abs=0.003)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"v_ref": -1.0, "volts_per_div": 0.5}, "positive"),
        ({"v_ref": 0.0, "volts_per_div": 0.5}, "positive"),
        ({"v_ref": 0.2, "volts_per_div": 0.5}, "div"),
        ({"v_ref": 4.0, "volts_per_div": 0.5}, "div"),
        ({"v_ref": 1.5, "volts_per_div": 0.5, "position_div": 2.0}, "position"),
        ({"v_ref": 1.5, "volts_per_div": 0.5, "position_div": -4.0}, "position"),
        ({"v_ref": 1.5, "volts_per_div": 0.5, "frames": 0}, "frames"),
        ({"v_ref": 1.5, "volts_per_div": 0.3}, "volts_per_div"),
    ],
)
def test_gain_from_reference_argument_checks(kwargs: dict[str, Any], match: str) -> None:
    with _open("dc:1.5v") as sc, pytest.raises(ValueError, match=match):
        calmod.gain_from_reference(sc, **kwargs)


def test_gain_from_reference_rejects_wrong_reference() -> None:
    with _open("dc:0v:noise=15mv") as sc, pytest.raises(CalibrationError, match="GAIN"):
        calmod.gain_from_reference(sc, 1.5, 0.5)  # nothing connected
    with _open("dc:1.0v:noise=15mv") as sc, pytest.raises(CalibrationError, match="outside"):
        calmod.gain_from_reference(sc, 1.5, 0.5)  # wrong v_ref -> g ~1.46
    with _open("dc:10v") as sc, pytest.raises(CalibrationError, match="saturated"):
        calmod.gain_from_reference(sc, 1.5, 0.5)


def test_offset_calibration_keeps_gain_and_reserved_data() -> None:
    nl = {"version": 1, "todo": True}
    host = units.Calibration(range_gain={0.5: 0.974}, neg_nonlinearity=nl)
    with _open("dc:0v:noise=40mv", host=host) as sc:
        cal = measure_calibration(sc)
        assert cal.source == "measured"
        assert cal.range_gain == {0.5: 0.974}
        assert cal.neg_nonlinearity == nl


def test_cli_measures_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "calibration.json"
    storage.save_calibration(units.Calibration(range_gain={1.0: 0.996}, source="measured"), path)
    opened: list[Scope] = []

    class _FakeScope:
        @staticmethod
        def open(calibration: str | None = None) -> Scope:
            assert calibration == str(path)
            t = SimTransport.virtual("dc:1.5v:noise=15mv", seed=3)
            t.device.calibration = PHYSICAL
            sc = Scope.open(t, calibration=storage.load_calibration(path))
            sc._calibration_path = None  # like sim: saving needs the explicit path
            opened.append(sc)
            return sc

    monkeypatch.setattr(calmod, "Scope", _FakeScope)
    rc = calmod.main(["--vref", "1.5", "--vdiv", "500mV", "--calibration", str(path), "--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "range_gain[0.5] = 0.97" in out
    doc = json.loads(path.read_text(encoding="utf-8"))
    gains = doc["calibration"]["range_gain"]
    assert set(gains) == {"0.5", "1"}
    assert gains["1"] == 0.996
    assert gains["0.5"] == pytest.approx(0.974, abs=0.003)
    assert list(tmp_path.glob("calibration.*.bak.json"))  # old file backed up
    assert opened[0].closed


def test_cli_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _FakeScope:
        @staticmethod
        def open(calibration: str | None = None) -> Scope:
            t = SimTransport.virtual("dc:0v", seed=3)
            return Scope.open(t)

    monkeypatch.setattr(calmod, "Scope", _FakeScope)
    assert calmod.main(["--vref", "1.5", "--vdiv", "0.5"]) == 1
    assert "GAIN CALIBRATION FAILED" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        calmod.main(["--vref", "1.5", "--vdiv", "0.3"])


def test_sim_signal_spec_still_used() -> None:
    # guard: changing the sim signal mid-test still works with a gain calibration
    with _open("dc:0v", host=PHYSICAL) as sc:
        t = sc._transport
        assert isinstance(t, SimTransport)
        t.device.signal = SignalSpec.parse("dc:1v:noise=15mv")
        assert _mean_volts(sc, volts_per_div=VoltsPerDiv.V0_5) == pytest.approx(1.0, abs=0.01)
        ch = ChannelSettings(VoltsPerDiv.V0_5)
        assert units.trigger_byte(sc.settings.trigger, ch, PHYSICAL) >= 0
