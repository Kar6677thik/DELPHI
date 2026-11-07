"""Small draw helpers shared by the generator modules (#57: public, so modules don't import
another module's private names)."""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta

import numpy as np

_CDFS: dict[tuple[tuple[str, float], ...], tuple[tuple[str, ...], list[float]]] = {}


def choice(rng: np.random.Generator, weights: dict[str, float]) -> str:
    """One draw from a categorical distribution (one uniform, inverse CDF)."""
    key = tuple(weights.items())
    if key not in _CDFS:
        totals = np.cumsum([w for _, w in key])
        _CDFS[key] = (tuple(k for k, _ in key), [float(t) for t in totals / totals[-1]])
    keys, cdf = _CDFS[key]
    return keys[min(bisect.bisect_right(cdf, rng.random()), len(keys) - 1)]


def between(rng: np.random.Generator, start: datetime, end: datetime) -> datetime:
    """A uniformly random instant in [start, end), to the second."""
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=int(rng.integers(0, max(seconds, 1))))
