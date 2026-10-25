"""Review: MCP calibration tools payload cap for a fit with many warnings (spec 4.3: every
response <= 8 KB). Simulator only; never touches port 8765 (in-process client)."""

from __future__ import annotations

from typing import Any

import numpy as np
from mcp_helpers import call, ok, text_of

from edu09.calwizard.fit import GainPoint
from edu09.device.scope import Scope
from edu09.mcp_server.cal_tools import MAX_RESPONSE_BYTES, calibration_tools_of
from edu09.mcp_server.server import ServerOptions, build_server, shutdown_server
from edu09.model import VoltsPerDiv


def _noisy_points() -> tuple[GainPoint, ...]:
    """A full-mode-sized point set (7 per range) with ~1 % scatter: every point gets a residual
    warning, the worst case for the preview size."""
    rng = np.random.default_rng(5)
    pts = []
    for v in (float(x) for x in VoltsPerDiv):
        for i, d in enumerate((5.0, 2.5, -3.0, -2.0, -5.0, -6.0, -6.5)):
            v_ref = d * v
            pts.append(
                GainPoint(
                    v,
                    -d / 2,
                    v_ref,
                    v_ref * (1 + float(rng.normal(0, 0.01))),
                    "model",
                    0.7,
                    f"S{i + 2:02d}",
                )
            )
    return tuple(pts)


def test_finish_payload_capped_with_many_warnings(mcp_scope: Scope) -> None:
    srv = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    try:
        ok(srv, "calibration_start", {"sources": []})
        ok(srv, "calibration_submit", {"action": "skip"})  # zero -> ready_to_fit
        sess: Any = calibration_tools_of(srv).session  # type: ignore[union-attr]
        sess.gain_points = _noisy_points  # instance override (the fit's input)
        for args in ({}, {"save": True, "confirm": True}):
            res = call(srv, "calibration_finish", args)
            assert not res.is_error, text_of(res)
            n = len(text_of(res).encode())
            assert n <= MAX_RESPONSE_BYTES, f"{args}: {n} bytes"
            d = ok(srv, "calibration_finish") if not args else None
            if d is not None:
                assert d["preview"]["warnings"]  # still reported (shortened)
                assert any("off after the fit" in w for w in d["preview"]["warnings"])
    finally:
        shutdown_server(srv)
