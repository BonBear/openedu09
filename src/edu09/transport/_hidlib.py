"""Pick the hidapi backend module (PyPI ``hidapi`` ships both ``hid`` and ``hidraw``)."""

from __future__ import annotations

import sys
from typing import Any


def load_hid(hid_module: Any | None = None) -> Any:
    """Return ``hid_module`` if given; else hidraw on Linux (fallback hid), hid elsewhere."""
    if hid_module is not None:
        return hid_module
    if sys.platform.startswith("linux"):
        try:
            import hidraw

            return hidraw
        except ImportError:
            pass
    import hid

    return hid
