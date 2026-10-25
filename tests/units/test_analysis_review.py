"""Reviewer tests for analysis.decimate (minmax bucket structure) and edge inputs."""

from __future__ import annotations

import numpy as np
import pytest

from edu09.analysis import decimate, measure


@pytest.mark.parametrize("n", [1024, 1000, 1023, 37])
@pytest.mark.parametrize("max_points", [2, 3, 16, 200, 511])
def test_minmax_points_stay_in_their_bucket(n: int, max_points: int) -> None:
    rng = np.random.default_rng(n + max_points)
    x = rng.normal(size=n)
    d = decimate(x, max_points)
    assert d.values.size <= max_points
    if n <= max_points:
        assert d.factor == 1
        return
    buckets = d.indices // d.factor
    counts = np.bincount(buckets)
    assert np.all((counts >= 1) & (counts <= 2))  # every bucket emits 1 or 2 points
    for b in np.unique(buckets):
        seg = x[b * d.factor : (b + 1) * d.factor]
        got = x[d.indices[buckets == b]]
        assert seg.min() in got and seg.max() in got
    np.testing.assert_array_equal(d.values, x[d.indices])


def test_minmax_constant_signal_emits_one_point_per_bucket() -> None:
    # Non-uniform output length: consumers must use ``indices``, not a fixed step (see T10).
    d = decimate(np.zeros(1024), 64)
    assert d.values.size == 32
    assert np.all(np.diff(d.indices) == d.factor)


def test_measure_rejects_bad_dt() -> None:
    for dt in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            measure(np.zeros(4), dt)
