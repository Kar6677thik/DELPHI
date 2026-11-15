"""MULE in the Guardian prior (docs/06 §11, #148), at engine level: the payment model has no
MULE class, so MULE comes from the payer's mule score, or from the open mass without one."""

from __future__ import annotations

import pytest

from delphi.engine.compile import CompiledProfile
from delphi.engine.prior import build_prior, open_mass
from delphi.sensing.prior import with_payer_mule

P_PAYMENT = {"GENUINE": 0.97, "ATO": 0.01, "APP_DIGITAL_ARREST": 0.01, "APP_INVESTMENT": 0.01}
Q = 0.5  # below the 90th percentile: u = open_mass_base


def _p0(guardian: CompiledProfile, p_model: dict[str, float]) -> dict[str, float]:
    prior = build_prior(guardian, p_model, Q)
    assert prior.sum() == pytest.approx(1.0)
    return dict(zip(guardian.hypotheses, prior.tolist(), strict=True))


def _u(guardian: CompiledProfile) -> float:
    return open_mass(Q, guardian.open_mass_base, guardian.open_mass_slope)


def test_mule_takes_the_payers_mule_score(guardian: CompiledProfile) -> None:
    p0 = _p0(guardian, with_payer_mule(P_PAYMENT, 0.3))
    u = _u(guardian)
    assert p0["MULE"] == pytest.approx((1 - u) * 0.3)
    assert p0["GENUINE"] == pytest.approx((1 - u) * 0.7 * 0.97)
    assert p0["UNEXPLAINED"] == pytest.approx(u)  # no KB-only typology left in the open mass


def test_without_a_mule_scorer_mule_gets_its_open_mass_share(guardian: CompiledProfile) -> None:
    p0 = _p0(guardian, with_payer_mule(P_PAYMENT, None))
    u = _u(guardian)
    share = guardian.open_mass_shares["MULE"]
    assert share > 0
    assert p0["MULE"] == pytest.approx(u * share)  # never vanishes, unlike a near-0 class
    assert p0["UNEXPLAINED"] == pytest.approx(u * (1 - share))
    assert p0["GENUINE"] == pytest.approx((1 - u) * 0.97)
