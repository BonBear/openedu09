"""Fixtures for the calibration wizard tests: a simulated UNIT1 scope with ADC noise.

Never opens the real device: every Scope here uses a ``SimTransport`` on a ``VirtualClock``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from calwizard_helpers import make_scope

from edu09.calwizard import model
from edu09.calwizard.simref import SimReference
from edu09.device.scope import Scope


@pytest.fixture(autouse=True)
def _no_saved_source_profile() -> Iterator[None]:
    """The isolated user config dir is shared by the whole run: start without a profile."""
    model.default_sources_path().unlink(missing_ok=True)
    yield
    model.default_sources_path().unlink(missing_ok=True)


@pytest.fixture
def unit1_scope() -> Iterator[Scope]:
    scope = make_scope("unit1")
    try:
        yield scope
    finally:
        scope.close()


@pytest.fixture
def sim_ref(unit1_scope: Scope) -> SimReference:
    ref = SimReference.for_scope(unit1_scope, noise_v=0.0)
    assert ref is not None
    return ref
