"""Simulated reference for the calibration wizard (spec 3.7).

:class:`SimReference` stands in for "the human + a bench supply + a multimeter" on a simulated
scope: :meth:`SimReference.connect` puts a DC level on the simulated input (``device.signal``) and
returns what the multimeter would show. It is only used for tests, demos and the CLI's
``--simulate`` mode; on hardware there is no ``SimReference`` (``for_scope`` returns ``None``).
"""

from __future__ import annotations

import math
from typing import Any

from edu09.transport.replay import RecordingTransport
from edu09.transport.sim import SignalSpec, SimTransport, SimulatedDevice

__all__ = ["SimReference"]

_MAX_ERROR_PCT = 50.0


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


class SimReference:
    """DC source + multimeter on a :class:`~edu09.transport.sim.SimulatedDevice`.

    ``noise_v``: rms noise of the source (volts) as seen by the scope. ``source_error_pct``: the
    source delivers ``volts * (1 + source_error_pct / 100)`` instead of the requested value (a
    knob that is off by a few percent; the wizard must still be right because it uses the meter
    reading). ``meter_error_pct``: the meter reads ``applied * (1 + meter_error_pct / 100)`` (a
    miscalibrated meter; the wizard then calibrates the scope to the meter). Meter readings are
    rounded to 3 decimals. ``seed`` is accepted for API stability (the readings are currently
    deterministic; the device's own generator makes the noise).
    """

    def __init__(
        self,
        device: SimulatedDevice,
        *,
        noise_v: float = 0.002,
        meter_error_pct: float = 0.0,
        source_error_pct: float = 0.0,
        seed: int | None = 0,
    ) -> None:
        if not isinstance(device, SimulatedDevice):
            raise TypeError(f"device must be a SimulatedDevice, got {type(device).__name__}")
        self.device = device
        self.noise_v = _finite("noise_v", noise_v)
        if self.noise_v < 0:
            raise ValueError(f"noise_v must be >= 0, got {noise_v!r}")
        self.meter_error_pct = _finite("meter_error_pct", meter_error_pct)
        self.source_error_pct = _finite("source_error_pct", source_error_pct)
        for name, pct in (("meter", self.meter_error_pct), ("source", self.source_error_pct)):
            if abs(pct) > _MAX_ERROR_PCT:
                raise ValueError(f"{name}_error_pct must be within +-{_MAX_ERROR_PCT:g}")
        self.seed = seed
        self.applied_v: float | None = None
        """Volts currently at the simulated input (``None`` = disconnected / open input)."""

    @classmethod
    def for_scope(cls, scope: Any, **kw: Any) -> SimReference | None:
        """Reference for a scope running on a ``SimTransport`` (also inside a
        ``RecordingTransport``), else ``None`` (hardware, replay)."""
        transport = getattr(scope, "transport", None)
        if isinstance(transport, RecordingTransport):
            transport = transport.inner
        if isinstance(transport, SimTransport):
            return cls(transport.device, **kw)
        return None

    def connect(self, volts: float) -> float:
        """Apply ``volts`` (signed; negative = leads reversed) and return the meter reading."""
        v = _finite("volts", volts)
        applied = v * (1.0 + self.source_error_pct / 100.0)
        self.device.signal = SignalSpec(kind="dc", offset_v=applied, noise_v=self.noise_v)
        self.applied_v = applied
        return round(applied * (1.0 + self.meter_error_pct / 100.0), 3)

    def disconnect(self) -> None:
        """Open input: 0 V plus the source-independent noise."""
        self.device.signal = SignalSpec(kind="dc", offset_v=0.0, noise_v=self.noise_v)
        self.applied_v = None
