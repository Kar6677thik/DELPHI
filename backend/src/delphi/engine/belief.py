"""Belief updates and information measures (docs/06 §3, §13, §14)."""

from __future__ import annotations

import numpy as np

from delphi.engine.types import FloatArray

ZERO_PROBABILITY = 1e-15


def as_belief(probs: FloatArray | list[float] | tuple[float, ...]) -> FloatArray:
    """Validate and normalise a probability vector."""
    p = np.asarray(probs, dtype=np.float64)
    if p.ndim != 1 or p.size == 0 or np.any(p < 0) or not np.all(np.isfinite(p)):
        raise ValueError("belief must be a non-empty, non-negative, finite vector")
    total = float(p.sum())
    if total <= 0:
        raise ValueError("belief must have positive mass")
    result: FloatArray = p / total
    return result


def update(belief: FloatArray, likelihood_column: FloatArray) -> FloatArray:
    """Bayes update in log space: log p' = log p + log L(o|·), then softmax (docs/06 §3)."""
    with np.errstate(divide="ignore"):
        log_post = np.log(belief) + np.log(likelihood_column)
    peak = np.max(log_post)
    if not np.isfinite(peak):
        raise ValueError("observation has zero probability under every hypothesis")
    weights = np.exp(log_post - peak)
    result: FloatArray = weights / weights.sum()
    return result


def predictive(belief: FloatArray, likelihood: FloatArray) -> FloatArray:
    """P(o | p, a) = Σ_h p(h) L[o, h] for every outcome o."""
    result: FloatArray = likelihood @ belief
    return result


def entropy_bits(belief: FloatArray) -> float:
    nz = belief[belief > 0]
    return float(-(nz * np.log2(nz)).sum())


def kl_bits(after: FloatArray, before: FloatArray) -> float:
    """KL(after ‖ before) in bits — the information yield of one observation (docs/06 §14)."""
    mask = after > 0
    return float((after[mask] * np.log2(after[mask] / before[mask])).sum())


def settle_pct(belief: FloatArray, likelihood: FloatArray) -> float:
    """Expected % of remaining entropy removed by observing a source (docs/06 §13)."""
    h0 = entropy_bits(belief)
    if h0 <= 0:
        return 0.0
    joint = likelihood * belief  # (|O|, |H|), unnormalised posteriors
    probs = joint.sum(axis=1)
    expected = 0.0
    for o in range(joint.shape[0]):
        if probs[o] > ZERO_PROBABILITY:
            expected += probs[o] * entropy_bits(joint[o] / probs[o])
    return 100.0 * (h0 - expected) / h0
