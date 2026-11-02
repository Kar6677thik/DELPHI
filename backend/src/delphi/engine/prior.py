"""Prior construction with open mass, and the fast-path rule (docs/06 §11, docs/08 §3)."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from delphi.engine.types import CompiledProfile, FloatArray
from delphi.kb.model import UNEXPLAINED

ANOMALY_KNEE = 0.90
MAX_OPEN_SHARE_SUM = 0.9


def open_mass(anomaly_percentile: float, base: float, slope: float) -> float:
    """u = base + slope · max(0, (q − 0.90) / 0.10)."""
    q = min(max(float(anomaly_percentile), 0.0), 1.0)
    a_open = max(0.0, (q - ANOMALY_KNEE) / (1.0 - ANOMALY_KNEE))
    return base + slope * a_open


def build_prior(
    profile: CompiledProfile,
    p_model: Mapping[str, float],
    anomaly_percentile: float,
) -> FloatArray:
    """Prior over ``profile.hypotheses`` (docs/06 §11).

    Model hypotheses = keys of ``p_model`` (the active artifact's classes) ∩ H; every other
    typology in H is KB-only and receives ``u · w_h``; UNEXPLAINED receives the rest of ``u``.
    """
    hyps = profile.hypotheses
    if UNEXPLAINED not in hyps:
        raise ValueError("prior construction needs UNEXPLAINED in the hypothesis set")
    model_h = [h for h in hyps if h in p_model]
    if not model_h:
        raise ValueError("p_model shares no hypothesis with the profile")
    kb_only = [h for h in hyps if h not in p_model and h != UNEXPLAINED]
    shares = {h: profile.open_mass_shares.get(h, 0.0) for h in kb_only}
    if sum(shares.values()) > MAX_OPEN_SHARE_SUM + 1e-12:
        raise ValueError("open_mass_share of KB-only typologies exceeds 0.9")

    model_mass = np.array([max(float(p_model[h]), 0.0) for h in model_h])
    if model_mass.sum() <= 0:
        raise ValueError("p_model has no mass on the profile's hypotheses")
    model_mass /= model_mass.sum()

    u = open_mass(anomaly_percentile, profile.open_mass_base, profile.open_mass_slope)
    prior = np.zeros(len(hyps))
    for h, m in zip(model_h, model_mass, strict=True):
        prior[hyps.index(h)] = (1.0 - u) * m
    for h, w in shares.items():
        prior[hyps.index(h)] = u * w
    prior[hyps.index(UNEXPLAINED)] = u * (1.0 - sum(shares.values()))
    return prior


def is_fast_path(
    p_genuine: float,
    amount_paise: float,
    anomaly_percentile: float,
    *,
    min_p_genuine: float,
    max_amount_inr: float,
    max_anomaly_percentile: float,
) -> bool:
    """docs/08 §3: release without probes when overwhelmingly genuine and small."""
    return (
        p_genuine >= min_p_genuine
        and amount_paise <= max_amount_inr * 100.0
        and anomaly_percentile < max_anomaly_percentile
    )
