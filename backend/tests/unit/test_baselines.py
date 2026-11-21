"""The baselines the Guardian monitor shows (docs/22 §2, docs/08 §6, FR-GDN-12)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from delphi.sim.generator_config import ScorePolicyConfig, load_generator_config
from delphi.sim.labels import PaymentDecision
from delphi.sim.legacy_policy import B1_DECISION, Baseline, decide_b1, decide_b2

B2 = ScorePolicyConfig(release_below=0.20, hold_at_or_above=0.60)


@pytest.mark.parametrize(
    ("p_genuine", "expected"),
    [
        (0.95, Baseline.RELEASE),  # r = 0.05
        (0.81, Baseline.RELEASE),  # r = 0.19
        (0.80, Baseline.OTP_STEP_UP),  # r = 0.20: the band starts here
        (0.41, Baseline.OTP_STEP_UP),  # r = 0.59
        (0.40, Baseline.HOLD_FOR_REVIEW),  # r = 0.60: hold from here
        (0.0, Baseline.HOLD_FOR_REVIEW),
    ],
)
def test_b2_bands(p_genuine: float, expected: Baseline) -> None:
    assert decide_b2(B2, p_genuine=p_genuine) is expected


def test_b2_thresholds_come_from_config_and_must_be_ordered() -> None:
    assert load_generator_config().score_policy == B2  # docs/22 §2's 0.20 and 0.60
    with pytest.raises(ValidationError):
        ScorePolicyConfig(release_below=0.7, hold_at_or_above=0.6)


def test_b1_hold_is_recorded_as_hold_for_review() -> None:
    """docs/22 §2: "Wherever this table says HOLD, the decision recorded is HOLD_FOR_REVIEW"."""
    assert B1_DECISION == {
        PaymentDecision.RELEASED: Baseline.RELEASE,
        PaymentDecision.HELD: Baseline.HOLD_FOR_REVIEW,
        PaymentDecision.DECLINED: Baseline.DECLINE,
    }
    legacy = load_generator_config().legacy_policy
    held = decide_b1(
        legacy,
        amount_paise=50_000_00,
        channel="UPI_PUSH",
        payee_listed=False,
        payee_is_new=True,
        device_is_new=False,
    )
    assert B1_DECISION[held] is Baseline.HOLD_FOR_REVIEW
