"""Explanations for investigators: "what would change my mind" (docs/06 §12)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from delphi.engine.risk import decide
from delphi.engine.types import CompiledProfile, FloatArray

MIN_FLIP_PROBABILITY = 0.05


@dataclass(frozen=True, slots=True)
class FlipCandidate:
    source_key: str
    outcome: str
    probability: float
    new_decision: str
    risk_change_paise: float


def what_would_change_my_mind(
    profile: CompiledProfile,
    belief: FloatArray,
    losses: FloatArray,
    source_keys: Iterable[str],
) -> FlipCandidate | None:
    """The most likely single (source, outcome) that would flip d*; None if no such outcome."""
    current_idx, current_risk = decide(belief, losses)
    flips: list[FlipCandidate] = []
    for key in source_keys:
        src = profile.sources[key]
        joint = src.likelihood * belief
        probs = joint.sum(axis=1)
        for o, outcome in enumerate(src.outcomes):
            if probs[o] < MIN_FLIP_PROBABILITY:
                continue
            post = joint[o] / probs[o]
            new_idx, new_risk = decide(post, losses)
            if new_idx != current_idx:
                flips.append(
                    FlipCandidate(
                        key,
                        outcome,
                        float(probs[o]),
                        profile.decisions[new_idx],
                        float(abs(new_risk - current_risk)),
                    )
                )
    if not flips:
        return None
    return min(flips, key=lambda f: (-f.probability, -f.risk_change_paise, f.source_key))


def top_hypotheses(
    profile: CompiledProfile, belief: FloatArray, n: int = 2
) -> list[tuple[str, float]]:
    order = np.argsort(-belief, kind="stable")[:n]
    return [(profile.hypotheses[i], float(belief[i])) for i in order]
