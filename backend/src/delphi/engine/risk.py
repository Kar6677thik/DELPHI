"""Loss, Bayes risk, decisions and myopic value of information (docs/06 §4–5)."""

from __future__ import annotations

import numpy as np

from delphi.engine.types import CompiledProfile, FloatArray


def loss_matrix(profile: CompiledProfile, amount_paise: float, vulnerable: bool) -> FloatArray:
    """Loss(d, h) = (base·100 + rate·A) · m, in paise; shape (|D|, |H|) (docs/06 §4)."""
    if amount_paise < 0:
        raise ValueError("amount_paise must be non-negative")
    loss = profile.loss_base_paise + profile.loss_rate * float(amount_paise)
    if vulnerable:
        loss = np.where(profile.loss_vscaled, loss * profile.vulnerability_multiplier, loss)
    result: FloatArray = np.asarray(loss, dtype=np.float64)
    return result


def risk_by_decision(belief: FloatArray, losses: FloatArray) -> FloatArray:
    result: FloatArray = losses @ belief
    return result


def decide(belief: FloatArray, losses: FloatArray) -> tuple[int, float]:
    """Return (index of d*, R(p)). Ties go to the earlier, less intrusive decision."""
    risks = risk_by_decision(belief, losses)
    idx = int(np.argmin(risks))  # argmin returns the first minimum → declared order wins
    return idx, float(risks[idx])


def bayes_risk(belief: FloatArray, losses: FloatArray) -> float:
    return float(np.min(losses @ belief))


def expected_risk_after(belief: FloatArray, likelihood: FloatArray, losses: FloatArray) -> float:
    """Σ_o P(o) R(p^{a,o}) computed from unnormalised posteriors: Σ_o min_d Loss_d · (L[o]∘p)."""
    joint = likelihood * belief  # (|O|, |H|)
    return float(np.min(joint @ losses.T, axis=1).sum())


def voi_myopic(belief: FloatArray, likelihood: FloatArray, losses: FloatArray) -> float:
    """VoI_1(a | p) = R(p) − Σ_o P(o) R(p^{a,o}) ≥ 0 (docs/06 §5). Reported, not used to choose."""
    return bayes_risk(belief, losses) - expected_risk_after(belief, likelihood, losses)
