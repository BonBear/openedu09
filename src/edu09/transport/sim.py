"""Simulated EDU09: a device-side model of the protocol plus a ``Transport`` around it.

* :class:`SignalSpec` describes the analog input (sine/square/triangle/sawtooth/dc/noise) and
  parses the ``--simulate SPEC`` strings (grammar in :meth:`SignalSpec.parse`).
* :class:`SimulatedDevice` implements the *device side* of protocol.md: it consumes 64-byte OUT
  payloads (:meth:`SimulatedDevice.handle`) and produces 64-byte IN reports
  (:meth:`SimulatedDevice.next_report`). Every byte layout comes from ``protocol.tables`` /
  ``protocol.commands``; volts -> counts uses ``units`` with the device's own (physical)
  :class:`~edu09.units.Calibration`.
* :class:`SimTransport` wraps a device in the :class:`~edu09.transport.base.Transport` protocol,
  applies the same opcode safety gate as ``HidTransport`` and supports unplug injection.

Nothing here starts a thread. All timing is driven by ``read``/``write`` calls and an injectable
clock: pass a :class:`VirtualClock` (or use :meth:`SimTransport.virtual`) for deterministic,
instant tests; the default is ``time.monotonic`` / ``time.sleep`` (real-time behaviour, for the
GUI and ``edu09-mcp --simulate``).

Hardware-verified behaviour (H1, 2026-10-24, firmware 1.01; protocol.md "Hardware-verified"):

* Idle: nothing unsolicited. ``0x0F`` -> ``"1.01"`` + CR + garbage. ``0x0E`` -> no reply.
* Q1 answered, ``"passive"`` (default): after ``0x0B`` the device streams by itself. While waiting
  for a trigger it sends ``tables.WAITING_REPORT`` (64 zero bytes) every ``status_interval_s``
  (``tables.STATUS_INTERVAL_S``, ~33 ms, first one ~33 ms after the arm; ETS modes use
  ``status_interval_ets_s`` = ``tables.STATUS_INTERVAL_ETS_S``, ~2 ms); when the capture is
  complete it sends one ``64 C8 mm`` + 61 zero bytes immediately. Timing follows the injected clock,
  not the number of reads. ``"resend_arm"`` (legacy option): each ``0x0B`` queues exactly one status
  report (waiting or complete); nothing is streamed.
* After ``0x0A`` the device queues ``leading_status_reports`` (default
  ``tables.READOUT_LEADING_STATUS`` = 2) exact ``64 C8 mm`` reports, the 16 data reports and
  ``trailing_reports`` (default ``tables.READOUT_TRAILING_REPORTS`` = 1) extra data-like reports
  that are not part of the frame (the simulator fills them with current input samples).
* Q4 trigger position (HW-verified per mode): the trigger crossing (first sample past the level)
  is placed at ``tables.TRIGGER_INDEX_BY_MODE[mode]`` in the *time-ordered* frame, unless a fixed
  ``trigger_index`` is passed.
* ``0x08`` while armed queues one final report (H2: 0.8..8.2 ms later on the HW): the waiting
  report, or ``64 C8 mm`` if the capture had completed (not yet read out); idle: no reply.
* ``0x0D`` (H2): never live samples. While armed it switches to a fast status stream, one report per
  ``tables.FAST_STATUS_INTERVAL_S`` (1 ms; the first immediately): the waiting report until the
  capture completes, then ``64 C8 mm`` repeatedly, until ``0x08`` (one final report, see above) or
  ``0x09``. A second ``0x0D`` changes nothing; ``0x0D`` while idle gets no reply.

Inferred choices (not distinguishable in the H1 captures):

* Waiting reports are streamed only while the trigger is enabled (a 2 ms/div free run on the
  hardware completed after 67 ms without a 33 ms waiting report), or while fewer than
  ``wait_polls`` have been sent (fault knob, default 0).
* Q2 status bytes: waiting = all zeros; byte 1 values other than 0xC8 are never produced.
  :data:`STATUS_WAITING_FLAG` is the (zero) byte 1 of the waiting report.
* Q5 edge: ``FLAG_EDGE_FALLING`` refers to the input *voltage* edge (so it stays correct if
  ``adc_inverted`` is flipped). Trigger source CH2 and the digital bit are ignored.
* Q6 ``0x14``/``0x0C``/unknown opcodes and ``0x09`` produce no reply. ``0x08`` aborts like
  ``0x09`` (plus the reply above).
* Fast status stream (0x0D): ``0x0A`` once complete still reads out the frame (and ends the stream);
  a re-arm (``0x0E`` while armed) ends it too.
* Offset DAC settling (HW-verified 2026-10-24, opt-in): with ``offset_settle_tau_s > 0`` a change
  of the offset byte does not take effect at once; the effective byte approaches the new value
  exponentially (``tau`` ~5 ms matches the hardware, whose first frame after an offset change is
  off by up to ~8 counts at its start). Off by default (0): the default model is ideal.
* Q7 ``0x0A`` before the capture is complete is ignored (no reply). ``0x0E`` while armed applies
  the new configuration and restarts the capture. ``0x0B`` while already armed does not restart
  the capture (in ``resend_arm`` mode it only answers with a status report).
* ``0x09``/``0x08`` do not flush reports that are already queued (like a real HID IN pipe; the
  host has to drain them).
* Unknown gain/timebase codes in a config keep the previous setting; a malformed ``0x0E`` payload
  is ignored.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, get_args

import numpy as np
import numpy.typing as npt

from edu09 import units
from edu09.errors import DeviceDisconnected, DeviceNotFound, UnsafeOperation
from edu09.model import ChannelSettings, Coupling, VoltsPerDiv
from edu09.protocol import ets, tables
from edu09.protocol.commands import HwConfig, decode_config
from edu09.transport.base import REPORT_SIZE, TransportInfo

__all__ = [
    "DEFAULT_SIGNAL",
    "KINDS",
    "PHYSICAL_UNITS",
    "STATUS_WAITING_FLAG",
    "SignalKind",
    "SignalSpec",
    "SimTransport",
    "SimulatedDevice",
    "UNIT1",
    "VirtualClock",
]

FloatArray = npt.NDArray[np.float64]

SignalKind = Literal["sine", "square", "triangle", "sawtooth", "dc", "noise"]
KINDS: Final[tuple[str, ...]] = get_args(SignalKind)
_KIND_ALIASES: Final[dict[str, str]] = {
    "sin": "sine",
    "sq": "square",
    "tri": "triangle",
    "saw": "sawtooth",
    "ramp": "sawtooth",
    "gauss": "noise",
}
_PERIODIC: Final = frozenset({"sine", "square", "triangle", "sawtooth"})
# Vrms -> peak amplitude factor per waveform shape (noise: amplitude *is* the rms).
_RMS_TO_PEAK: Final[dict[str, float]] = {
    "sine": math.sqrt(2.0),
    "square": 1.0,
    "triangle": math.sqrt(3.0),
    "sawtooth": math.sqrt(3.0),
    "dc": 1.0,
    "noise": 1.0,
}

STATUS_WAITING_FLAG: Final = 0x00
"""Byte 1 of the waiting report (HW: the whole report is zeros, ``tables.WAITING_REPORT``)."""

MAX_STATUS_BACKLOG: Final = 32
"""Streamed waiting reports kept for a host that does not read (like a bounded HID IN buffer)."""

_EPS: Final = 1e-9  # clock tolerance when a report is due exactly "now"

DEFAULT_SIGNAL: Final = "sine:1khz:2vpp"

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
_TOKEN_RE: Final = re.compile(rf"^(?:(?P<key>[a-z_]+)=)?(?P<num>{_NUM})\s*(?P<unit>[a-z%]*)$")

_FREQ_UNITS: Final = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "k": 1e3}
_VOLT_UNITS: Final = {"v": 1.0, "mv": 1e-3}
_KEY_ALIASES: Final = {
    "f": "freq",
    "freq": "freq",
    "frequency": "freq",
    "vpp": "vpp",
    "amp": "amp",
    "amplitude": "amp",
    "vp": "amp",
    "rms": "rms",
    "vrms": "rms",
    "offset": "offset",
    "ofs": "offset",
    "dc": "offset",
    "duty": "duty",
    "phase": "phase",
    "noise": "noise",
}


# --------------------------------------------------------------------------------------------------
# Physical front-end models (the device's own calibration: how volts become ADC counts)
# --------------------------------------------------------------------------------------------------
UNIT1: Final = units.Calibration(
    base_ofs=tables.TYPICAL_BASE_OFS,
    range_corr=dict(tables.TYPICAL_RANGE_CORR),
    gnd_fit=units.GndFit(tables.TYPICAL_GND_K, tables.TYPICAL_GND_B),
    source="measured",
    range_gain={0.1: 0.9870, 0.2: 0.9750, 0.5: 0.9741, 1.0: 0.9960, 2.0: 0.9810, 5.0: 0.9789},
    range_gain_neg={0.1: 0.9846, 0.2: 0.9668, 0.5: 0.9697, 1.0: 0.9892, 2.0: 0.9786, 5.0: 0.9726},
    neg_nonlinearity={
        0.2: ((4.0, 0.0), (5.069, 0.069), (6.26, 0.26)),
        0.5: ((4.0, 0.0), (5.083, 0.083), (6.315, 0.315)),
    },
)
"""Physical model of the measured unit 1 (docs/data/edu09_gain_sweep_unit1.json, protocol.md
11.3): typical offsets, positive / negative gains and the negative expansion at 0.2 and 0.5 V/div
(0.2 V/div beyond 5 div and the 5 V/div negative gain are extrapolated / assumed)."""

PHYSICAL_UNITS: Final[dict[str, units.Calibration]] = {
    "ideal": units.Calibration(),
    "unit1": UNIT1,
}
"""Named physical front ends for ``SimulatedDevice(calibration=...)`` (``--sim-unit``)."""


def _fmt(x: float) -> str:
    return f"{x:.12g}"


# --------------------------------------------------------------------------------------------------
# Signal description
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalSpec:
    """Analog input signal.

    ``amplitude_v`` is the peak amplitude (half of Vpp) for periodic kinds and the rms
    (standard deviation) for ``"noise"``; ``"dc"`` ignores it and outputs ``offset_v``.
    ``duty`` applies to ``"square"`` only. ``noise_v`` is the rms of additional Gaussian noise
    (any kind).
    """

    kind: SignalKind = "sine"
    frequency_hz: float = 1000.0
    amplitude_v: float = 1.0
    offset_v: float = 0.0
    duty: float = 0.5
    phase_deg: float = 0.0
    noise_v: float = 0.0

    def __post_init__(self) -> None:
        kind = _KIND_ALIASES.get(str(self.kind).lower(), str(self.kind).lower())
        if kind not in KINDS:
            raise ValueError(f"unknown signal kind {self.kind!r}; allowed: {', '.join(KINDS)}")
        object.__setattr__(self, "kind", kind)
        for name in ("frequency_hz", "amplitude_v", "offset_v", "duty", "phase_deg", "noise_v"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
            object.__setattr__(self, name, float(value))
        if self.frequency_hz <= 0:
            raise ValueError(f"frequency_hz must be > 0, got {self.frequency_hz}")
        if self.amplitude_v < 0 or self.noise_v < 0:
            raise ValueError("amplitude_v and noise_v must be >= 0")
        if not 0.0 < self.duty < 1.0:
            raise ValueError(f"duty must be in (0, 1), got {self.duty}")

    @property
    def is_periodic(self) -> bool:
        return self.kind in _PERIODIC

    @property
    def vpp(self) -> float:
        """Nominal peak-to-peak voltage of the noiseless signal (0 for dc and noise)."""
        return 2.0 * self.amplitude_v if self.is_periodic else 0.0

    def dc_component(self) -> float:
        """Mean value of the signal (what AC coupling removes)."""
        if self.kind == "square":
            return self.offset_v + self.amplitude_v * (2.0 * self.duty - 1.0)
        return self.offset_v

    def evaluate(self, t: npt.ArrayLike, rng: np.random.Generator | None = None) -> FloatArray:
        """Signal voltage at times ``t`` (seconds). ``rng`` is needed for noise (else no noise)."""
        tt = np.asarray(t, dtype=np.float64)
        a = self.amplitude_v
        if self.is_periodic:
            p = np.mod(self.frequency_hz * tt + self.phase_deg / 360.0, 1.0)
            if self.kind == "sine":
                v = a * np.sin(2.0 * np.pi * p)
            elif self.kind == "square":
                v = np.where(p < self.duty, a, -a)
            elif self.kind == "triangle":
                v = a * (1.0 - 4.0 * np.abs(np.mod(p + 0.25, 1.0) - 0.5))
            else:  # sawtooth
                v = a * (2.0 * np.mod(p + 0.5, 1.0) - 1.0)
        else:
            v = np.zeros_like(tt)
        v = v + self.offset_v
        if rng is not None:
            sigma = self.noise_v + (a if self.kind == "noise" else 0.0)
            if sigma > 0:
                v = v + rng.normal(0.0, sigma, size=tt.shape)
        return np.asarray(v, dtype=np.float64)

    # ---------------------------------------------------------------------------------------------
    @classmethod
    def parse(cls, spec: str | SignalSpec) -> SignalSpec:
        """Parse a ``--simulate`` string (case-insensitive, whitespace ignored).

        Grammar::

            SPEC   := KIND (":" TOKEN)*
            KIND   := sine | square | triangle | sawtooth | dc | noise
                      (aliases: sin, sq, tri, saw, ramp, gauss)
            TOKEN  := NUMBER UNIT | KEY "=" NUMBER [UNIT]
            UNIT   := hz | khz | mhz (= MHz)        -> frequency
                    | vpp | mvpp                    -> peak-to-peak (amplitude = vpp / 2)
                    | vp | vpk | mvp | mvpk         -> peak amplitude
                    | vrms | mvrms                  -> rms (amplitude = rms * crest factor;
                                                       for noise: the noise rms)
                    | v | mv                        -> DC offset (the level for "dc")
                    | %                             -> duty cycle (square)
                    | deg                           -> phase
            KEY    := f|freq|frequency, vpp, amp|amplitude|vp (peak), rms|vrms,
                      offset|ofs|dc, duty (fraction, or with %), phase (deg), noise (rms volts)

        Keyed values without a unit use base units (Hz, V, degrees; duty as a fraction).
        Examples: ``sine:1khz:2vpp``, ``square:50hz:5vpp:duty=25%:offset=2.5v``,
        ``dc:1.5v``, ``noise:100mvrms``, ``triangle:20khz:1vp:noise=10mv``.
        Raises ``ValueError`` with the offending token.
        """
        if isinstance(spec, SignalSpec):
            return spec
        if not isinstance(spec, str):
            raise ValueError(f"signal spec must be a string, got {type(spec).__name__}")
        parts = [p.strip() for p in spec.replace(" ", "").lower().split(":")]
        if not parts or not parts[0]:
            raise ValueError(f"empty signal spec {spec!r}; expected e.g. {DEFAULT_SIGNAL!r}")
        kind = _KIND_ALIASES.get(parts[0], parts[0])
        if kind not in KINDS:
            raise ValueError(f"unknown signal kind {parts[0]!r}; allowed: {', '.join(KINDS)}")
        kw: dict[str, float] = {}
        for tok in parts[1:]:
            if not tok:
                continue
            field_name, value = cls._parse_token(tok, kind)
            kw[field_name] = value
        try:
            return cls(kind=kind, **kw)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValueError(f"invalid signal spec {spec!r}: {exc}") from None

    @staticmethod
    def _parse_token(tok: str, kind: str) -> tuple[str, float]:
        m = _TOKEN_RE.match(tok)
        if not m:
            raise ValueError(f"cannot parse signal token {tok!r}")
        key, num, unit = m.group("key"), float(m.group("num")), m.group("unit")
        crest = _RMS_TO_PEAK[kind]
        if key is not None:
            k = _KEY_ALIASES.get(key)
            if k is None:
                raise ValueError(f"unknown signal key {key!r} in {tok!r}")
            if k == "freq":
                scale = 1.0 if unit == "" else _FREQ_UNITS.get(unit)
                if scale is None:
                    raise ValueError(f"bad frequency unit in {tok!r}")
                return "frequency_hz", num * scale
            if k == "duty":
                if unit == "%":
                    return "duty", num / 100.0
                if unit:
                    raise ValueError(f"bad duty unit in {tok!r}")
                return "duty", num
            if k == "phase":
                if unit not in ("", "deg"):
                    raise ValueError(f"bad phase unit in {tok!r}")
                return "phase_deg", num
            vscale = 1.0 if unit == "" else _VOLT_UNITS.get(unit)
            if vscale is None:
                raise ValueError(f"bad voltage unit in {tok!r}")
            v = num * vscale
            if k == "vpp":
                return "amplitude_v", v / 2.0
            if k == "amp":
                return "amplitude_v", v
            if k == "rms":
                return "amplitude_v", v * crest
            if k == "offset":
                return "offset_v", v
            return "noise_v", v  # k == "noise"
        if unit in _FREQ_UNITS and unit != "k":
            return "frequency_hz", num * _FREQ_UNITS[unit]
        if unit == "%":
            return "duty", num / 100.0
        if unit == "deg":
            return "phase_deg", num
        for suffix, field_name, factor in (
            ("vpp", "amplitude_v", 0.5),
            ("vpk", "amplitude_v", 1.0),
            ("vp", "amplitude_v", 1.0),
            ("vrms", "amplitude_v", crest),
            ("v", "offset_v", 1.0),
        ):
            if unit.endswith(suffix):
                prefix = unit[: -len(suffix)]
                if prefix in ("", "m"):
                    return field_name, num * factor * (1e-3 if prefix else 1.0)
        raise ValueError(f"unknown unit {unit!r} in signal token {tok!r}")

    def to_spec(self) -> str:
        """Canonical spec string; ``SignalSpec.parse(s.to_spec()) == s``."""
        parts: list[str] = [self.kind]
        if self.is_periodic:
            parts += [f"{_fmt(self.frequency_hz)}hz", f"{_fmt(2.0 * self.amplitude_v)}vpp"]
        elif self.kind == "noise":
            parts.append(f"{_fmt(self.amplitude_v)}vrms")
        if self.offset_v != 0.0 or self.kind == "dc":
            parts.append(f"offset={_fmt(self.offset_v)}v")
        if self.kind == "square" and self.duty != 0.5:
            parts.append(f"duty={_fmt(self.duty)}")
        if self.phase_deg != 0.0:
            parts.append(f"phase={_fmt(self.phase_deg)}deg")
        if self.noise_v != 0.0:
            parts.append(f"noise={_fmt(self.noise_v)}v")
        return ":".join(parts)

    def __str__(self) -> str:
        return self.to_spec()


# --------------------------------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------------------------------
class VirtualClock:
    """Manual monotonic clock for deterministic runs: ``clock()`` reads, ``sleep`` advances."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.now += seconds

    advance = sleep


# --------------------------------------------------------------------------------------------------
# Device model
# --------------------------------------------------------------------------------------------------
DeviceState = Literal["idle", "armed", "complete"]

_VDIV_BY_GAIN: Final[dict[int, float]] = {code: v for v, code in tables.GAIN_CODES_CH1.items()}
_TB_BY_CODE: Final[dict[int, tables.TimebaseEntry]] = {}
for _entry in tables.TIMEBASES.values():
    _TB_BY_CODE.setdefault(_entry.hw_code, _entry)  # 0x0B: 200 us (same HW setting as 100 us)


@dataclass
class _Capture:
    frame: bytes | None  # time-ordered raw samples; None = no trigger found (never completes)
    ready_at: float
    mode: int


class SimulatedDevice:
    """Device side of the EDU09 protocol (see the module docstring for unverified choices).

    Feed host payloads to :meth:`handle`; pull IN reports with :meth:`next_report` (``None`` when
    the device has nothing to send) and :meth:`seconds_until_report` (when the next streamed
    report is due). Fault knobs (plain attributes, change any time): ``never_trigger`` (captures
    never complete while the trigger is enabled), ``wait_polls`` (minimum number of waiting status
    reports per capture, streamed every ``status_interval_s`` even in free run; raise it for a
    slow status), and ``drop_frame_report`` (index 0..15 of a data report dropped from the next
    read-out, one-shot). ``offset_settle_tau_s`` (default 0 = instant) models the offset DAC
    settling exponentially after an offset byte change (HW: ~5 ms time constant).
    ``trim_gain`` (default 1.0) multiplies the input volts (a stand-in for the board trimmer).
    ``adc_noise_counts`` (default 0) adds Gaussian noise of that rms (in ADC counts) before
    quantisation (HW: ~0.7 counts at an open input); it dithers DC levels so averaged means
    resolve fractions of a count, as on the real device.
    ``signal`` may be replaced at any time (live input change).
    """

    def __init__(
        self,
        signal: SignalSpec | str = DEFAULT_SIGNAL,
        *,
        seed: int | None = 0,
        clock: Callable[[], float] = time.monotonic,
        calibration: units.Calibration | None = None,
        status_poll: tables.StatusPollStrategy = tables.STATUS_POLL,
        firmware: str = "1.01",
        trigger_index: int | None = None,
        wait_polls: int = 0,
        leading_status_reports: int = tables.READOUT_LEADING_STATUS,
        status_interval_s: float = tables.STATUS_INTERVAL_S,
        status_interval_ets_s: float = tables.STATUS_INTERVAL_ETS_S,
        trailing_reports: int = tables.READOUT_TRAILING_REPORTS,
        trigger_hysteresis_counts: float = 1.0,
        max_trigger_search: int = 1 << 16,
        report_interval_s: float = 0.001,
        offset_settle_tau_s: float = 0.0,
        adc_noise_counts: float = 0.0,
    ) -> None:
        if offset_settle_tau_s < 0 or not math.isfinite(offset_settle_tau_s):
            raise ValueError("offset_settle_tau_s must be finite and >= 0")
        if not math.isfinite(adc_noise_counts) or adc_noise_counts < 0:
            raise ValueError("adc_noise_counts must be finite and >= 0")
        if status_poll not in get_args(tables.StatusPollStrategy):
            raise ValueError(f"unknown status_poll strategy {status_poll!r}")
        if status_interval_s <= 0 or status_interval_ets_s <= 0:
            raise ValueError("status intervals must be > 0")
        if trigger_index is not None and not 0 <= trigger_index < tables.FRAME_SIZE:
            raise ValueError(f"trigger_index must be in 0..{tables.FRAME_SIZE - 1}")
        self.signal = SignalSpec.parse(signal)
        self.clock = clock
        self.calibration = calibration if calibration is not None else units.Calibration()
        self.status_poll: tables.StatusPollStrategy = status_poll
        self.firmware = firmware
        self.trigger_index = trigger_index
        self.wait_polls = wait_polls
        self.leading_status_reports = leading_status_reports
        self.status_interval_s = status_interval_s
        self.status_interval_ets_s = status_interval_ets_s
        self.trailing_reports = trailing_reports
        self._interval = status_interval_s
        self.trigger_hysteresis_counts = trigger_hysteresis_counts
        self.max_trigger_search = max_trigger_search
        self.report_interval_s = report_interval_s
        self.offset_settle_tau_s = offset_settle_tau_s
        self.never_trigger = False
        self.drop_frame_report: int | None = None
        self.trim_gain: float = 1.0
        self.adc_noise_counts: float = float(adc_noise_counts)
        self._seed = seed
        self._rng = np.random.default_rng(seed)
        self._epoch = clock()
        self.received: list[int] = []
        """Opcodes received, in order (for tests)."""
        self.ignored: list[str] = []
        """Human-readable notes about payloads the device ignored (for tests and debugging)."""
        self.frames_sent = 0
        self._reset_state()

    # -- state ------------------------------------------------------------------------------------
    def _reset_state(self) -> None:
        self.config = HwConfig()
        self._vdiv = tables.DEFAULT_VOLTS_PER_DIV
        self._tb = tables.TIMEBASES[tables.DEFAULT_TIMEBASE]
        self.state: DeviceState = "idle"
        self._capture: _Capture | None = None
        self._polls = 0
        self._next_tick = math.inf
        self._announced = False
        self._fast = False  # 0x0D fast status stream active
        self._next_fast = math.inf
        self._queue: deque[bytes] = deque()
        self._ofs_from = float(self.config.ofs1)  # effective offset byte when it last changed
        self._ofs_changed_at = -math.inf

    def power_cycle(self) -> None:
        """Back to power-on state (default config, idle, no queued reports). Keeps the signal."""
        self._reset_state()

    @property
    def pending_reports(self) -> int:
        """Number of reports queued for the host."""
        return len(self._queue)

    @property
    def timebase(self) -> tables.TimebaseEntry:
        """Timebase currently selected by the configuration."""
        return self._tb

    @property
    def volts_per_div(self) -> float:
        return self._vdiv

    @property
    def mode(self) -> int:
        return self._tb.mode

    def now(self) -> float:
        """Signal time in seconds since the device was created."""
        return self.clock() - self._epoch

    # -- host -> device ---------------------------------------------------------------------------
    def handle(self, payload: bytes) -> None:
        """Process one 64-byte OUT payload (no report-ID byte)."""
        data = bytes(payload)
        if len(data) != REPORT_SIZE:
            raise ValueError(f"payload must be exactly {REPORT_SIZE} bytes, got {len(data)}")
        op = data[0]
        self.received.append(op)
        if op == tables.OP_VERSION:
            ver = self.firmware.encode("ascii")[: tables.VERSION_MAX_LENGTH - 1]
            ver += bytes((tables.VERSION_TERMINATOR,))
            garbage = self._rng.integers(0, 256, REPORT_SIZE - len(ver), dtype=np.uint8)
            self._queue.append(ver + garbage.tobytes())
        elif op == tables.OP_CONFIG:
            self._apply_config(data)
        elif op in (tables.OP_RESET, tables.OP_STOP):
            if op == tables.OP_STOP and self.state != "idle":
                # HW (H1/H2): one final report when armed; 64 C8 mm if the capture had completed
                self._queue.append(self._status_report(count_poll=False))
            self.state = "idle"
            self._capture = None
            self._fast = False
        elif op == tables.OP_ARM:
            if self.state == "idle":
                self._arm()
            if self.status_poll == "resend_arm":
                self._queue.append(self._status_report())
        elif op == tables.OP_READOUT:
            self._readout()
        elif op == tables.OP_LIVE:
            if self.state == "idle":
                self.ignored.append("0x0D while idle ignored (no reply)")
            elif not self._fast:  # HW (H2): a second 0x0D changes nothing
                self._fast = True
                self._next_fast = self.clock()
        else:
            self.ignored.append(f"opcode 0x{op:02X} ignored (no reply)")

    def _apply_config(self, data: bytes) -> None:
        try:
            cfg = decode_config(data)
        except ValueError as exc:
            self.ignored.append(f"malformed config ignored: {exc}")
            return
        if cfg.ofs1 != self.config.ofs1 and self.offset_settle_tau_s > 0:
            now = self.clock()
            self._ofs_from = float(self._effective_ofs(np.array([now]))[0])
            self._ofs_changed_at = now
        self.config = cfg
        gain = cfg.gain1 & ~tables.CH1_AC_BIT
        if gain in _VDIV_BY_GAIN:
            self._vdiv = _VDIV_BY_GAIN[gain]
        else:
            self.ignored.append(f"unknown CH1 gain code 0x{gain:02X}; keeping {self._vdiv} V/div")
        if cfg.tb in _TB_BY_CODE:
            self._tb = _TB_BY_CODE[cfg.tb]
        else:
            self.ignored.append(f"unknown timebase code 0x{cfg.tb:02X}; keeping previous")
        if self.state != "idle":  # Q7 choice: config while armed restarts the capture
            self._arm()

    # -- device -> host ---------------------------------------------------------------------------
    def next_report(self) -> bytes | None:
        """The next IN report, or ``None`` if the device has nothing to send right now.

        Queued replies come first; in ``"passive"`` mode an armed device then streams waiting
        reports on the ``status_interval_s`` grid and one ``64 C8 mm`` when the capture completes.
        """
        if self._queue:
            return self._queue.popleft()
        if self._fast and self.state != "idle":
            return self._next_fast_report()
        if self.status_poll != "passive" or self.state == "idle":
            return None
        if self.state == "armed" and self._waiting_due(self.clock()):
            self._polls += 1
            self._next_tick += self._interval
            return tables.WAITING_REPORT
        if self._is_complete() and not self._announced:
            self._announced = True
            return self._status_report()
        return None

    def seconds_until_report(self) -> float | None:
        """Seconds until :meth:`next_report` has a report (0 = now); ``None``: nothing expected."""
        if self._queue:
            return 0.0
        if self._fast and self.state != "idle":
            now = self.clock()
            self._limit_fast_backlog(now)
            return max(0.0, self._next_fast - now)
        if self.status_poll != "passive" or self.state == "idle":
            return None
        now = self.clock()
        if self.state == "complete":
            return None if self._announced else 0.0
        times: list[float] = []
        if self._streams_waiting():
            self._limit_backlog(now)
            times.append(self._next_tick)
        cap = self._capture
        if cap is not None and cap.frame is not None and self._polls >= self.wait_polls:
            times.append(cap.ready_at)
        if not times:
            return None
        return max(0.0, min(times) - now)

    def _limit_fast_backlog(self, now: float) -> None:
        step = tables.FAST_STATUS_INTERVAL_S
        oldest = now - (MAX_STATUS_BACKLOG - 1) * step
        if self._next_fast < oldest:
            self._next_fast += math.floor((oldest - self._next_fast) / step) * step

    def _next_fast_report(self) -> bytes | None:
        """0x0D fast status stream (H2): one status report per ``FAST_STATUS_INTERVAL_S``."""
        now = self.clock()
        self._limit_fast_backlog(now)
        if self._next_fast > now + _EPS:
            return None
        self._next_fast += tables.FAST_STATUS_INTERVAL_S
        if self._is_complete():
            self._announced = True
        return self._status_report()

    def _streams_waiting(self) -> bool:
        trig_on = bool(self.config.flags & tables.FLAG_TRIG_ENABLE)
        return trig_on or self._polls < self.wait_polls

    def _limit_backlog(self, now: float) -> None:
        oldest = now - (MAX_STATUS_BACKLOG - 1) * self._interval
        if self._next_tick < oldest:
            skipped = math.floor((oldest - self._next_tick) / self._interval)
            self._next_tick += skipped * self._interval

    def _waiting_due(self, now: float) -> bool:
        """A waiting report is due: on the tick grid, and the capture was not complete by then."""
        if not self._streams_waiting():
            return False
        self._limit_backlog(now)
        if self._next_tick > now + _EPS:
            return False
        cap = self._capture
        ready = cap.ready_at if cap is not None and cap.frame is not None else math.inf
        return self._next_tick < ready or self._polls < self.wait_polls

    # -- acquisition ------------------------------------------------------------------------------
    def _channel(self) -> tuple[ChannelSettings, units.Calibration]:
        """Front end as configured: (DC channel at position 0, calibration with base = DAC)."""
        cal = self.calibration
        vdiv = VoltsPerDiv.parse(self._vdiv)
        dac = self.config.ofs1 - cal.range_correction(vdiv)
        dac = min(max(dac, tables.OFFSET_DAC_MIN), tables.OFFSET_DAC_MAX)
        dev_cal = dataclasses.replace(cal, base_ofs=dac, range_corr={})
        return ChannelSettings(vdiv, Coupling.DC, 0.0), dev_cal

    def _effective_ofs(self, at: FloatArray) -> FloatArray:
        """Offset byte the front end actually applies at clock times ``at`` (settling model)."""
        target = float(self.config.ofs1)
        tau = self.offset_settle_tau_s
        if tau <= 0 or self._ofs_from == target:
            return np.full(np.shape(at), target)
        age = np.maximum(np.asarray(at, dtype=np.float64) - self._ofs_changed_at, 0.0)
        return target + (self._ofs_from - target) * np.exp(-age / tau)

    def _settle_counts(self, t: FloatArray) -> FloatArray | float:
        """Raw-count error of the unsettled offset at signal times ``t`` (0 when settled).

        ``gnd_raw = 255 - (k * dac + b)``: an offset byte ``d`` above its target shifts the
        samples by ``-k * d`` counts. ETS frames use their effective sample times (an
        approximation; the HW observation was made on realtime timebases)."""
        if self.offset_settle_tau_s <= 0 or self._ofs_from == self.config.ofs1:
            return 0.0
        eff = self._effective_ofs(np.asarray(t, dtype=np.float64) + self._epoch)
        return -self.calibration.gnd_fit.k * (eff - float(self.config.ofs1))

    def _adc_counts(self, v: FloatArray, t: FloatArray) -> FloatArray:
        """Quantized ADC counts (rounded, saturating, float64) of volts ``v`` at times ``t``."""
        ch, cal = self._channel()
        c = units.volts_to_counts(v, ch, cal) + self._settle_counts(t)
        if self.adc_noise_counts > 0:
            c = c + self._rng.normal(0.0, float(self.adc_noise_counts), np.shape(c))
        return np.clip(np.rint(c), tables.ADC_MIN, tables.ADC_MAX)

    def _to_raw(self, v: FloatArray, t: FloatArray) -> npt.NDArray[np.uint8]:
        return self._adc_counts(v, t).astype(np.uint8)

    def _volts(self, t: FloatArray) -> FloatArray:
        v = self.signal.evaluate(t, self._rng)
        if self.config.gain1 & tables.CH1_AC_BIT:
            v = v - self.signal.dc_component()
        if self.trim_gain != 1.0:
            v = v * float(self.trim_gain)
        return v

    def _input_samples(self, n: int) -> bytes:
        """``n`` raw samples of the current input at the real ADC rate (trailing report filler)."""
        dt = self._tb.dt * tables.ETS_SUBRECORDS[self._tb.mode]  # real ADC rate
        t = self.now() + np.arange(n, dtype=np.float64) * dt
        return self._to_raw(self._volts(t), t).tobytes()

    def _trigger_level_counts(self) -> float:
        b = self.config.trig
        return float(tables.ADC_MAX - b if tables.TRIGGER_BYTE_INVERTED else b)

    def _search_length(self, dt: float) -> int:
        n = tables.FRAME_SIZE
        if self.signal.is_periodic:
            per_period = 1.0 / (self.signal.frequency_hz * dt)
            n = max(n, math.ceil(2.0 * per_period) + 2)
        else:
            n = 4 * n
        return min(n, self.max_trigger_search) + tables.FRAME_SIZE

    def trigger_index_for(self, mode: int) -> int:
        """Where the trigger crossing lands in the time-ordered frame for acquisition ``mode``."""
        if self.trigger_index is not None:
            return self.trigger_index
        return tables.TRIGGER_INDEX_BY_MODE[mode]

    def _find_trigger(self, counts: FloatArray, falling: bool, ti: int) -> int | None:
        """First Schmitt-trigger crossing at index >= ``ti`` that leaves room for a frame."""
        level = self._trigger_level_counts()
        s, lvl = (-counts, -level) if falling else (counts, level)
        n = s.size
        ev = np.zeros(n, dtype=np.int8)
        ev[s <= lvl - self.trigger_hysteresis_counts] = -1
        ev[s >= lvl] = 1
        idx = np.where(ev != 0, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        state = ev[idx]
        fires = np.flatnonzero((state[1:] == 1) & (state[:-1] == -1)) + 1
        ok = fires[(fires >= ti) & (fires - ti + tables.FRAME_SIZE <= n)]
        return int(ok[0]) if ok.size else None

    def _arm(self) -> None:
        self.state = "armed"
        self._polls = 0
        self._announced = False
        self._fast = False
        tb = self._tb
        mode = tb.mode
        self._interval = self.status_interval_ets_s if tb.is_ets else self.status_interval_s
        self._next_tick = self.clock() + self._interval
        ti = self.trigger_index_for(mode)
        nsub = tables.ETS_SUBRECORDS[mode]
        dt = tb.dt
        ch, cal = self._channel()
        trig_on = bool(self.config.flags & tables.FLAG_TRIG_ENABLE)
        n = self._search_length(dt) if trig_on else tables.FRAME_SIZE
        t = self.now() + np.arange(n, dtype=np.float64) * dt
        v = self._volts(t)
        start: int | None = 0
        if trig_on:
            if self.never_trigger:
                start = None
            else:
                falling = bool(self.config.flags & tables.FLAG_EDGE_FALLING)
                falling ^= cal.adc_inverted  # edge is defined on the input voltage
                counts = units.volts_to_counts(v, ch, cal) + self._settle_counts(t)
                hit = self._find_trigger(counts, falling, ti)
                start = None if hit is None else hit - ti
        arm_time = self.clock()
        if start is None:
            self._capture = _Capture(None, math.inf, mode)
            return
        seg = slice(start, start + tables.FRAME_SIZE)
        frame = self._to_raw(v[seg], t[seg]).tobytes()
        duration = (start + tables.FRAME_SIZE) * dt * nsub  # ETS needs nsub trigger events
        self._capture = _Capture(frame, arm_time + duration, mode)

    def _is_complete(self) -> bool:
        cap = self._capture
        if self.state == "complete":
            return True
        if self.state != "armed" or cap is None or cap.frame is None:
            return False
        if self.clock() + _EPS < cap.ready_at or self._polls < self.wait_polls:
            return False
        self.state = "complete"
        return True

    def _status_report(self, count_poll: bool = True) -> bytes:
        """``64 C8 mm`` + zeros if the capture is complete, else the all-zero waiting report."""
        mode = self._capture.mode if self._capture is not None else self._tb.mode
        if self._is_complete():
            return self._complete_report(mode)
        if count_poll:
            self._polls += 1
        return tables.WAITING_REPORT

    @staticmethod
    def _complete_report(mode: int) -> bytes:
        head = bytes((tables.STATUS_MARKER, tables.STATUS_COMPLETE, mode))
        return head + bytes(REPORT_SIZE - len(head))  # HW-verified: zero padded

    def _readout(self) -> None:
        cap = self._capture
        # 0x0A after the capture time has passed counts as complete even without a status poll
        armed = cap is not None and self.state == "armed" and cap.frame is not None
        if armed and cap is not None and self.clock() >= cap.ready_at:
            self.state = "complete"
        if self.state != "complete" or cap is None or cap.frame is None:
            self.ignored.append("0x0A before capture complete ignored (no reply)")
            return
        wire = ets.interleave(cap.mode, cap.frame)
        for _ in range(self.leading_status_reports):
            self._queue.append(self._complete_report(cap.mode))
        drop, self.drop_frame_report = self.drop_frame_report, None
        for i in range(tables.REPORTS_PER_FRAME):
            if i != drop:
                self._queue.append(wire[i * REPORT_SIZE : (i + 1) * REPORT_SIZE])
        for _ in range(self.trailing_reports):  # HW: one extra report that is not frame data
            self._queue.append(self._input_samples(REPORT_SIZE))
        self.frames_sent += 1
        self.state = "idle"
        self._fast = False
        self._capture = None


# --------------------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------------------
class SimTransport:
    """``Transport`` backed by a :class:`SimulatedDevice`. Starts no threads.

    ``read(timeout_s)`` returns the device's next report after sleeping
    ``device.report_interval_s`` (one USB interrupt interval). If nothing is available it sleeps
    until the device's next streamed report is due (if that is within ``timeout_s``) and returns
    it, else sleeps ``timeout_s`` and returns ``None``. ``sleep`` defaults to the device clock's
    ``sleep`` (for :class:`VirtualClock`), else ``time.sleep``.

    The opcode gate mirrors ``HidTransport``: opcodes outside ``tables.DRIVER_OPCODES`` (0x14 only
    if ``tables.SEND_RELEASE_ON_CLOSE``) raise ``UnsafeOperation`` unless
    ``allow_unknown_opcodes``; if let through, the device ignores them (no reply).

    Fault injection: :meth:`unplug` / :meth:`replug` / :meth:`schedule_unplug`, plus the device
    knobs (``never_trigger``, ``wait_polls``, ``drop_frame_report``). When ``device`` is given,
    ``signal``/``seed``/``clock``/``status_poll`` are ignored (they configure the default device).
    """

    def __init__(
        self,
        signal: SignalSpec | str = DEFAULT_SIGNAL,
        *,
        device: SimulatedDevice | None = None,
        seed: int | None = 0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        status_poll: tables.StatusPollStrategy = tables.STATUS_POLL,
        allow_unknown_opcodes: bool = False,
    ) -> None:
        if device is None:
            device = SimulatedDevice(
                signal,
                seed=seed,
                clock=clock if clock is not None else time.monotonic,
                status_poll=status_poll,
            )
        self.device = device
        if sleep is None:
            clock_sleep = getattr(device.clock, "sleep", None)
            sleep = clock_sleep if callable(clock_sleep) else time.sleep
        self._sleep: Callable[[float], None] = sleep
        self._allow_unknown = allow_unknown_opcodes
        self._open = False
        self._plugged = True
        self._unplug_after_reads: int | None = None
        self.info = TransportInfo(kind="sim", path=f"sim:{device.signal.to_spec()}")

    @classmethod
    def virtual(
        cls,
        signal: SignalSpec | str = DEFAULT_SIGNAL,
        *,
        seed: int | None = 0,
        status_poll: tables.StatusPollStrategy = tables.STATUS_POLL,
        allow_unknown_opcodes: bool = False,
        start: float = 0.0,
    ) -> SimTransport:
        """Deterministic transport on a fresh :class:`VirtualClock` (never really sleeps)."""
        clock = VirtualClock(start)
        return cls(
            signal,
            seed=seed,
            clock=clock,
            sleep=clock.sleep,
            status_poll=status_poll,
            allow_unknown_opcodes=allow_unknown_opcodes,
        )

    # -- Transport protocol -----------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def plugged(self) -> bool:
        return self._plugged

    def open(self) -> None:
        if not self._plugged:
            raise DeviceNotFound("simulated EDU09 is unplugged")
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, payload: bytes) -> None:
        if len(payload) != REPORT_SIZE:
            raise ValueError(f"payload must be exactly {REPORT_SIZE} bytes, got {len(payload)}")
        self._check_opcode(payload[0])
        self._require()
        self.device.handle(bytes(payload))

    def read(self, timeout_s: float) -> bytes | None:
        self._require()
        if self._unplug_after_reads is not None:
            if self._unplug_after_reads <= 0:
                self.unplug()
                raise DeviceDisconnected("simulated EDU09 unplugged (scheduled)")
            self._unplug_after_reads -= 1
        report = self.device.next_report()
        if report is None:
            waited = 0.0
            due = self.device.seconds_until_report()
            if due is not None and due <= timeout_s:
                if due > 0:
                    self._sleep(due)
                waited = due
                report = self.device.next_report()
            if report is None:
                if timeout_s - waited > 0:
                    self._sleep(timeout_s - waited)
                return None
        self._sleep(self.device.report_interval_s)
        return report

    # -- fault injection --------------------------------------------------------------------------
    def unplug(self) -> None:
        """Pull the cable: I/O raises ``DeviceDisconnected``, ``open`` raises ``DeviceNotFound``."""
        self._plugged = False
        self._open = False
        self._unplug_after_reads = None

    def replug(self, power_cycle: bool = True) -> None:
        """Plug the device back in (power-on state unless ``power_cycle=False``). Needs ``open``."""
        self._plugged = True
        if power_cycle:
            self.device.power_cycle()

    def schedule_unplug(self, after_reads: int) -> None:
        """Unplug when the read after ``after_reads`` further reads is attempted."""
        if after_reads < 0:
            raise ValueError("after_reads must be >= 0")
        self._unplug_after_reads = after_reads

    # -- helpers ----------------------------------------------------------------------------------
    def _check_opcode(self, op: int) -> None:
        if self._allow_unknown or op in tables.DRIVER_OPCODES:
            return
        if op == tables.OP_RELEASE and tables.SEND_RELEASE_ON_CLOSE:
            return
        raise UnsafeOperation(f"opcode 0x{op:02X} is not allowed by the transport safety gate")

    def _require(self) -> None:
        if not self._plugged:
            raise DeviceDisconnected("simulated EDU09 is unplugged")
        if not self._open:
            raise DeviceDisconnected("transport is not open")
