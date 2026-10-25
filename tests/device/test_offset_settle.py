"""Offset DAC settling (HW-verified 2026-10-24) and the typical hardware calibration.

* The simulator can model the front end settling exponentially after an offset byte change
  (``SimulatedDevice.offset_settle_tau_s``; HW tau ~5 ms, first frame off by up to ~8 counts).
* The worker waits ``worker.OFFSET_SETTLE_S`` (20 ms) after a ``0x0E`` that changed the offset or
  gain byte before ``0x0B``; unchanged configs add no delay.
* Calibration retries a too-noisy *first* reading once after an extra settle.
* Hardware without a calibration file gets ``Calibration.typical()`` (typical offsets, no gain
  correction) and is reported as uncalibrated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edu09 import units
from edu09.device import Scope, measure_calibration
from edu09.device import calibration as calmod
from edu09.device import worker as worker_mod
from edu09.errors import CalibrationError
from edu09.model import ChannelSettings, Settings, Timebase, TriggerMode, VoltsPerDiv
from edu09.protocol import tables
from edu09.transport.base import TransportInfo
from edu09.transport.sim import SimTransport

TAU_S = 0.005
SETTLE_LOG = "offset settle: waited"


def _tolerance(step_counts: float) -> float:
    """Allowed head/tail difference after the 20 ms wait: exp(-4) ~1.8 % of the offset step
    (3 % with margin) plus one count of quantization."""
    return 0.03 * step_counts + 1.0


def _open(signal: str = "dc:0v", tau: float = TAU_S, seed: int = 0) -> Scope:
    t = SimTransport.virtual(signal, seed=seed)
    t.device.offset_settle_tau_s = tau
    return Scope.open(t, reconnect_interval_s=0.01)


def _head_tail_error(scope: Scope) -> float:
    """Mean of the first 100 samples minus the mean of the last 100 (0 when settled)."""
    raw = scope.acquire(trigger_mode="none").raw.astype(np.float64)
    return float(raw[:100].mean() - raw[-100:].mean())


# --------------------------------------------------------------------------------------------
# simulator model
# --------------------------------------------------------------------------------------------
def test_sim_settle_model_is_off_by_default() -> None:
    t = SimTransport.virtual("dc:0v")
    assert t.device.offset_settle_tau_s == 0.0
    with Scope.open(t) as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
        sc.acquire()
        sc.configure(position_div=-1.0)
        assert _head_tail_error(sc) == 0.0


def test_sim_settle_model_rejects_bad_tau() -> None:
    from edu09.transport.sim import SimulatedDevice

    with pytest.raises(ValueError, match="offset_settle_tau_s"):
        SimulatedDevice(offset_settle_tau_s=-1.0)


def test_without_the_wait_the_first_frame_is_off_at_its_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_mod, "OFFSET_SETTLE_S", 0.0)
    with _open() as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
        sc.acquire()
        # position -1 div: ground 32 counts lower; the unsettled start is still well above it
        sc.configure(position_div=-1.0)
        assert _head_tail_error(sc) > 5.0
        assert abs(_head_tail_error(sc)) < 0.5  # the next frame is settled (as on the HW)


@pytest.mark.parametrize(("vdiv", "pos"), [(1.0, -1.0), (1.0, 2.0), (0.1, -3.0), (5.0, 0.5)])
def test_worker_wait_removes_the_settling_artefact(
    vdiv: float, pos: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = abs(pos) * tables.COUNTS_PER_DIV
    errors = {}
    for settle in (0.0, tables.OFFSET_DAC_SETTLE_S):
        monkeypatch.setattr(worker_mod, "OFFSET_SETTLE_S", settle)
        with _open() as sc:
            sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
            sc.acquire()
            sc.configure(volts_per_div=vdiv, position_div=pos)
            errors[settle] = abs(_head_tail_error(sc))
            wf = sc.acquire(trigger_mode="none")
            assert abs(float(wf.volts.mean())) <= vdiv / 32  # 0 V reads 0 once settled
    assert errors[0.0] > 0.3 * step  # artefact without the wait (only the ~3 ms drain)
    assert errors[tables.OFFSET_DAC_SETTLE_S] <= _tolerance(step)


def test_settle_wait_only_when_the_offset_or_gain_byte_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _open() as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
        sc.acquire()
        with caplog.at_level(logging.DEBUG, logger=worker_mod.__name__):
            caplog.clear()
            for _ in range(3):
                sc.acquire()
            sc.configure(timebase="500us")  # offset byte unchanged
            sc.acquire()
            assert SETTLE_LOG not in caplog.text
            sc.configure(position_div=1.0)
            sc.acquire()
            assert caplog.text.count(SETTLE_LOG) == 1
            caplog.clear()
            sc.configure(volts_per_div=2.0, position_div=1.0)  # gain changed (ofs may not)
            sc.acquire()
            assert caplog.text.count(SETTLE_LOG) == 1


def test_continuous_run_adds_no_settle_delay(caplog: pytest.LogCaptureFixture) -> None:
    with _open() as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
        sc.acquire()
        with caplog.at_level(logging.DEBUG, logger=worker_mod.__name__):
            caplog.clear()
            sc.run()
            for _ in range(5):
                sc.acquire()
            sc.stop()
        assert SETTLE_LOG not in caplog.text


def test_auto_fallback_without_config_change_adds_no_settle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _open("dc:0v") as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="auto", trigger_level_v=2.0)
        sc.acquire()
        with caplog.at_level(logging.DEBUG, logger=worker_mod.__name__):
            caplog.clear()
            wf = sc.acquire()
        assert not wf.triggered  # AUTO fell back to free run
        assert "AUTO: no trigger" in caplog.text
        assert SETTLE_LOG not in caplog.text


def test_settle_wait_runs_on_the_worker_clock() -> None:
    with _open() as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")
        sc.acquire()
        t = sc._transport
        assert isinstance(t, SimTransport)
        clock = t.device.clock
        t0 = clock()
        sc.acquire()
        plain = clock() - t0
        sc.configure(position_div=1.0)
        t0 = clock()
        sc.acquire()
        settled = clock() - t0
        assert settled - plain == pytest.approx(worker_mod.OFFSET_SETTLE_S, abs=0.006)


def test_live_samples_settle_after_offset_change() -> None:
    with _open() as sc:
        sc.configure(volts_per_div=1.0, timebase="200us", trigger_mode="none")

        def grab(io: worker_mod.DeviceIO) -> bytes:
            s = io.settings.replace(position_div=-2.0)
            return io.live_samples(tables.FRAME_SIZE, settings=s)

        raw = np.frombuffer(sc.run_exclusive(grab), dtype=np.uint8).astype(np.float64)
        assert abs(raw[:100].mean() - raw[-100:].mean()) <= _tolerance(2 * tables.COUNTS_PER_DIV)


def test_live_samples_rejects_negative_settle() -> None:
    with _open() as sc, pytest.raises(ValueError, match="settle_s"):
        sc.run_exclusive(lambda io: io.live_samples(16, settle_s=-1.0))


def test_calibration_with_settling_front_end_matches_ideal() -> None:
    physical = units.Calibration(gnd_fit=units.GndFit(2.0, -133.6), range_corr={1.0: 3, 0.1: -7})
    results = []
    for tau in (0.0, TAU_S):
        t = SimTransport.virtual("dc:0v:noise=40mv", seed=1)
        t.device.calibration = physical
        t.device.offset_settle_tau_s = tau
        with Scope.open(t, reconnect_interval_s=0.01) as sc:
            results.append(measure_calibration(sc))
    assert results[0].base_ofs == results[1].base_ofs == 131
    assert results[0].range_corr == results[1].range_corr == {1.0: 3, 0.1: -7}
    assert results[1].gnd_fit.k == pytest.approx(2.0, abs=0.02)


# --------------------------------------------------------------------------------------------
# calibration: retry a noisy first reading once
# --------------------------------------------------------------------------------------------
@dataclass
class NoisyStartIO:
    """Front end ``raw = 255 - (k * dac + b)`` with the first ``noisy`` readings noisy."""

    noisy: int = 1
    noisy_at: set[int] = field(default_factory=set)
    k: float = 1.03
    b: float = -9.8
    calibration: units.Calibration = field(default_factory=units.Calibration)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(3)

    def live_samples(
        self,
        n: int,
        *,
        timebase: Any = None,
        settings: Settings | None = None,
        calibration: units.Calibration | None = None,
        settle_s: float = 0.0,
    ) -> bytes:
        assert settings is not None and calibration is not None
        assert not Timebase.parse(timebase).is_ets
        cfg = units.settings_to_hwconfig(
            settings.replace(trigger_mode=TriggerMode.NONE), calibration
        )
        idx = len(self.calls)
        self.calls.append({"ofs": cfg.ofs1, "settle_s": settle_s})
        mean = 255.0 - (self.k * cfg.ofs1 + self.b)
        sigma = 6.0 if (idx < self.noisy or idx in self.noisy_at) else 0.7
        x = np.rint(mean + self.rng.normal(0.0, sigma, n))
        return np.clip(x, 0, 255).astype(np.uint8).tobytes()


def test_noisy_first_reading_is_retried_once_after_extra_settle() -> None:
    io = NoisyStartIO(noisy=1)
    cal = calmod._measure(io)  # type: ignore[arg-type]
    assert io.calls[0]["settle_s"] == 0.0
    assert io.calls[1]["settle_s"] == calmod.RETRY_SETTLE_S
    assert io.calls[1]["ofs"] == io.calls[0]["ofs"]  # the same reading again
    assert all(c["settle_s"] == 0.0 for c in io.calls[2:])
    assert cal.gnd_fit.k == pytest.approx(1.03, abs=0.03)
    assert tables.CAL_BASE_OFS_MIN < cal.base_ofs < tables.CAL_BASE_OFS_MAX


def test_noisy_first_reading_twice_fails() -> None:
    io = NoisyStartIO(noisy=2)
    with pytest.raises(CalibrationError, match="signal seems to be connected"):
        calmod._measure(io)  # type: ignore[arg-type]
    assert len(io.calls) == 2


def test_only_the_first_reading_is_retried() -> None:
    io = NoisyStartIO(noisy=0, noisy_at={1})  # the slope probe (2nd reading) is noisy
    with pytest.raises(CalibrationError, match="signal seems to be connected"):
        calmod._measure(io)  # type: ignore[arg-type]
    assert len(io.calls) == 2
    assert all(c["settle_s"] == 0.0 for c in io.calls)


# --------------------------------------------------------------------------------------------
# typical hardware calibration
# --------------------------------------------------------------------------------------------
def test_typical_calibration_values() -> None:
    cal = units.Calibration.typical()
    assert cal.source == "typical"
    assert cal.base_ofs == 0x86
    assert cal.range_corr == {1.0: -1, 0.1: -1}
    assert cal.gnd_fit == units.GndFit(1.03, -9.8)
    assert units.Calibration.from_dict(cal.to_dict()) == cal
    assert cal.range_gain == {} and cal.range_gain_neg == {} and cal.neg_nonlinearity == {}
    # ground near mid-scale; one division of position ~31 DAC steps (not 16 as with slope 2):
    # 32 counts per division / 1.03 counts per DAC step; with a gain of 0.981 (one unit's 2 V/div)
    # it is 32 / 0.981 = 32.6 counts per true division -> 32 steps
    ch = ChannelSettings(VoltsPerDiv.V2)
    assert units.gnd_counts(ch, cal) == pytest.approx(126.8, abs=0.1)
    up = ChannelSettings(VoltsPerDiv.V2, position_div=1.0)
    assert cal.base_ofs - units.offset_dac(up, cal) == 31
    with_gain = replace(cal, range_gain=dict(tables.TYPICAL_RANGE_GAIN))
    assert with_gain.base_ofs - units.offset_dac(up, with_gain) == 32
    # the uncalibrated model is unchanged (sim and fixtures depend on it)
    assert units.Calibration().gnd_fit == units.GndFit(2.0, -128.0)
    assert units.Calibration().base_ofs == tables.OFFSET_DAC_DEFAULT


class _HidLike:
    def __init__(self) -> None:
        self.inner = SimTransport.virtual(seed=0)
        self.info = TransportInfo(kind="hid", path="/dev/fake-edu09")

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes) -> None:
        self.inner.write(payload)

    def read(self, timeout_s: float) -> bytes | None:
        return self.inner.read(timeout_s)


def test_hardware_without_calibration_file_uses_typical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from edu09 import storage
    from edu09.device.scope import UNCALIBRATED_WARNING

    def uncalibrated(sc: Scope) -> bool:
        st = sc.status
        d = st.to_dict()
        assert (
            d["calibrated"] is st.calibrated and d["calibration_warning"] == st.calibration_warning
        )
        return not st.calibrated and st.calibration_warning == UNCALIBRATED_WARNING

    monkeypatch.setattr(storage, "default_calibration_path", lambda: tmp_path / "none.json")
    caplog.set_level(logging.WARNING, logger="edu09.device.scope")
    with Scope.open(_HidLike(), lock_dir=tmp_path) as sc:  # type: ignore[arg-type]
        assert sc.calibration == units.Calibration.typical()
        assert sc.calibration.range_gain == {}  # gain-neutral: no other unit's gains
        assert sc.status.calibration_source == "typical"
        assert uncalibrated(sc)
    assert [r.getMessage() for r in caplog.records].count(
        f"no usable {tmp_path / 'none.json'}: {UNCALIBRATED_WARNING}"
    ) == 1
    with Scope.open(  # type: ignore[arg-type]
        _HidLike(), lock_dir=tmp_path, calibration=tmp_path / "missing.json"
    ) as sc:
        assert sc.status.calibration_source == "typical"
        assert uncalibrated(sc)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(storage, "default_calibration_path", lambda: bad)
    with Scope.open(_HidLike(), lock_dir=tmp_path) as sc:  # type: ignore[arg-type]
        assert sc.status.calibration_source == "typical"
        assert uncalibrated(sc)
        # installing a measured calibration (the wizard's save) clears it
        sc.set_calibration(replace(units.Calibration.typical(), source="measured"), save=False)
        assert sc.status.calibrated and sc.status.calibration_warning is None


def test_hardware_with_calibration_file_uses_it(tmp_path: Path) -> None:
    from edu09 import storage

    path = tmp_path / "cal.json"
    measured = units.Calibration(base_ofs=0x85, gnd_fit=units.GndFit(1.0, -8.0), source="measured")
    storage.save_calibration(measured, path)
    with Scope.open(_HidLike(), lock_dir=tmp_path, calibration=path) as sc:  # type: ignore[arg-type]
        assert sc.calibration == measured
        assert sc.status.calibrated and sc.status.calibration_warning is None


def test_sim_keeps_the_default_model() -> None:
    with Scope.open(SimTransport.virtual()) as sc:
        assert sc.status.calibration_source == "default"
        assert sc.status.calibrated  # the simulator's front end follows the model
        assert sc.settings == Settings()
