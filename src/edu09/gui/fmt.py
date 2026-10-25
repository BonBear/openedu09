"""Pure number formatting helpers for the GUI (no Qt imports; unit-testable)."""

from __future__ import annotations

import math

__all__ = ["format_si", "timebase_text", "vdiv_text"]

_PREFIXES: tuple[tuple[float, str], ...] = (
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, "µ"),
    (1e-9, "n"),
)


def format_si(value: float | None, unit: str = "", digits: int = 4) -> str:
    """``0.00123, "s"`` -> ``"1.23 ms"``; ``None``/NaN -> an em dash. ``%`` is never prefixed."""
    if value is None or not math.isfinite(value):
        return "—"
    if unit == "%":
        return f"{value:.{max(digits - 1, 1)}g} %"
    if value == 0:
        return f"0 {unit}".rstrip()
    mag = abs(value)
    # pick the prefix the value has *after* rounding to ``digits`` (999.97 -> "1 k")
    edge = 1.0 - 5.0 * 10.0 ** -(digits + 1)
    scale, prefix = next(((sc, pf) for sc, pf in _PREFIXES if mag >= sc * edge), _PREFIXES[-1])
    text = f"{value / scale:.{digits}g}"
    return f"{text} {prefix}{unit}".rstrip()


def vdiv_text(volts_per_div: float) -> str:
    return f"{format_si(volts_per_div, 'V')}/div"


def timebase_text(seconds_per_div: float) -> str:
    return f"{format_si(seconds_per_div, 's')}/div"
