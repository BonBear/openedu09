"""Helpers for the hardware tests (``tests/hw``): opt-in environment flags, signal spec parsing,
frame sanity checks. Nothing here touches the device."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pytest

from edu09.model import Timebase, VoltsPerDiv
from edu09.units import sample_interval

ENV_SIGNAL = "EDU09_HW_SIGNAL"
ENV_INPUT_OPEN = "EDU09_HW_INPUT_OPEN"
ENV_CALIBRATE = "EDU09_HW_CALIBRATE"

_SIGNAL_RE = re.compile(
    r"^(?P<kind>[a-z]+):(?P<freq>[0-9.]+)\s*(?P<unit>[kKmM]?)hz:"
    r"(?P<lo>[-+]?[0-9.]+)\.\.(?P<hi>[-+]?[0-9.]+)v?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SignalSpec:
    """A known periodic signal on CH1 (``EDU09_HW_SIGNAL="square:30khz:0..4.16v"``)."""

    kind: str
    frequency_hz: float
    low_v: float
    high_v: float

    @property
    def mid_v(self) -> float:
        return (self.low_v + self.high_v) / 2

    @property
    def vpp(self) -> float:
        return self.high_v - self.low_v


def parse_signal(text: str) -> SignalSpec:
    """``KIND:FREQ:LO..HI`` e.g. ``square:30khz:0..4.16v`` (FREQ in hz/khz/mhz, volts)."""
    m = _SIGNAL_RE.match(text.strip())
    if not m:
        raise ValueError(
            f"{ENV_SIGNAL}={text!r}: expected KIND:FREQ:LO..HI, e.g. square:30khz:0..4.16v"
        )
    scale = {"": 1.0, "k": 1e3, "m": 1e6}[m["unit"].lower()]
    spec = SignalSpec(m["kind"].lower(), float(m["freq"]) * scale, float(m["lo"]), float(m["hi"]))
    if spec.frequency_hz <= 0 or spec.high_v <= spec.low_v:
        raise ValueError(f"{ENV_SIGNAL}={text!r}: need FREQ > 0 and LO < HI")
    return spec


def signal_from_env() -> SignalSpec | None:
    text = os.environ.get(ENV_SIGNAL, "").strip()
    return parse_signal(text) if text else None


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def fit_signal(spec: SignalSpec) -> tuple[VoltsPerDiv, float]:
    """Smallest V/div that shows the signal within 6 divisions, and the position_div that puts
    the signal's centre on the screen centre."""
    for vd in VoltsPerDiv:
        pos = -spec.mid_v / float(vd)
        if spec.vpp <= 6 * float(vd) and abs(pos) <= 4.0:
            return vd, pos
    raise ValueError(f"signal {spec} does not fit any V/div range")


def timebases_for(spec: SignalSpec | None) -> list[Timebase]:
    """Timebases that show >= 3 periods of the signal with >= 4 samples per period (all of
    them when no signal is configured; the tests skip themselves then)."""
    if spec is None:
        return list(Timebase)
    out = []
    for tb in Timebase:
        per = 1.0 / (spec.frequency_hz * sample_interval(tb))
        if 4 <= per <= 1024 / 3:
            out.append(tb)
    return out


def longest_zero_run(raw: npt.NDArray[np.uint8]) -> int:
    """Length of the longest run of raw 0 samples (a lost report shows up as 64 zeros)."""
    z = np.concatenate(([0], (raw == 0).astype(np.int8), [0]))
    d = np.diff(z)
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return int((ends - starts).max()) if starts.size else 0


def assert_clean(raw: npt.NDArray[np.uint8], what: str = "frame") -> None:
    assert raw.size == 1024, f"{what}: {raw.size} samples"
    run = longest_zero_run(raw)
    assert run < 16, f"{what}: run of {run} zero samples (lost report?)"


def crossing_near(
    volts: npt.NDArray[np.float64], level: float, rising: bool, near: int
) -> int | None:
    """Index of the first sample past ``level`` on a rising/falling edge, closest to ``near``."""
    a, b = volts[:-1], volts[1:]
    hit = (a < level) & (b >= level) if rising else (a >= level) & (b < level)
    idx = np.flatnonzero(hit) + 1
    if idx.size == 0:
        return None
    return int(idx[np.argmin(np.abs(idx - near))])


def skip_unless(name: str, why: str) -> None:
    if not flag(name):
        pytest.skip(f"{why}: set {name}=1")
