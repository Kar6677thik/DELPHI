"""Error contracts and small helpers of the engine (fail fast, docs/24 §1)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from delphi.engine import (
    CompiledProfile,
    as_belief,
    build_prior,
    compile_profile,
    likelihood_matrix,
    loss_matrix,
    plan_next,
    predictive,
    session_rng,
    top_hypotheses,
    update,
    with_sources,
    without_exploration,
)
from delphi.engine.types import Phase
from delphi.kb.model import KnowledgeBase


@pytest.mark.parametrize("bad", [[], [[0.5, 0.5]], [-0.1, 1.1], [np.nan, 1.0], [0.0, 0.0]])
def test_as_belief_rejects_invalid(bad: list[float]) -> None:
    with pytest.raises(ValueError, match="belief"):
        as_belief(bad)


def test_predictive_sums_to_one(guardian: CompiledProfile) -> None:
    p = as_belief([0.5, 0.1, 0.2, 0.1, 0.05, 0.05])
    for src in guardian.sources.values():
        assert predictive(p, src.likelihood).sum() == pytest.approx(1.0)


def test_likelihood_matrix_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="entries"):
        likelihood_matrix(["A", "B"], ["GENUINE"], {"GENUINE": [0.2, 0.3, 0.5]})


def test_compile_rejects_inapplicable_or_missing_genuine(kb: KnowledgeBase) -> None:
    with pytest.raises(ValueError, match="not applicable"):
        compile_profile(kb, "CASE_ACCOUNT_REVIEW", hypotheses=["GENUINE", "ATO"])
    with pytest.raises(ValueError, match="GENUINE"):
        compile_profile(kb, "GUARDIAN_PAYMENT", hypotheses=["ATO"])


def test_evidence_cost_is_investigator_minutes(navigator_hold: CompiledProfile) -> None:
    # 8 minutes x ₹15/min = ₹120 = 12,000 paise (docs/06 §7)
    assert navigator_hold.sources["CUSTOMER_OUTREACH_CALL"].cost_paise == pytest.approx(12_000)


def test_compiled_profile_rejects_mis_shaped_likelihood(guardian: CompiledProfile) -> None:
    src = guardian.sources["OTP_STEP_UP"]
    bad = replace(src, likelihood=np.ones((2, 3)) / 2)
    with pytest.raises(ValueError, match="shape"):
        with_sources(guardian, {"OTP_STEP_UP": bad})


def test_loss_matrix_rejects_negative_amount(guardian: CompiledProfile) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        loss_matrix(guardian, -1, False)


def test_prior_contract_errors(kb: KnowledgeBase, guardian: CompiledProfile) -> None:
    no_unexplained = compile_profile(kb, "GUARDIAN_PAYMENT", include_unexplained=False)
    with pytest.raises(ValueError, match="UNEXPLAINED"):
        build_prior(no_unexplained, {"GENUINE": 1.0}, 0.5)
    with pytest.raises(ValueError, match="no mass"):
        build_prior(guardian, dict.fromkeys(guardian.hypotheses[:-1], 0.0), 0.5)
    crowded = replace(guardian, open_mass_shares=dict.fromkeys(guardian.open_mass_shares, 0.5))
    with pytest.raises(ValueError, match=r"exceeds 0\.9"):
        build_prior(crowded, {"GENUINE": 1.0}, 0.5)  # 4 KB-only typologies x 0.5 > 0.9


def test_top_hypotheses(guardian: CompiledProfile) -> None:
    p = as_belief([0.1, 0.2, 0.6, 0.05, 0.03, 0.02])
    assert top_hypotheses(guardian, p) == [
        ("APP_DIGITAL_ARREST", pytest.approx(0.6)),
        ("ATO", pytest.approx(0.2)),
    ]


def test_session_rng_is_reproducible() -> None:
    a = session_rng("pay-1", 7).random(3)
    b = session_rng("pay-1", 7).random(3)
    np.testing.assert_array_equal(a, b)


def test_without_exploration_disables_epsilon(guardian: CompiledProfile) -> None:
    quiet = without_exploration(guardian)
    assert quiet.exploration_epsilon == 0.0
    p = as_belief([0.5, 0.1, 0.2, 0.1, 0.05, 0.05])
    plan = plan_next(
        quiet,
        p,
        amount_paise=48_000_000,
        vulnerable=False,
        phase=Phase.GUARDIAN_ACTIVE,
        rng=np.random.default_rng(0),
    )
    assert plan.propensity is None


def test_update_keeps_zero_hypotheses_at_zero() -> None:
    post = update(as_belief([0.0, 0.5, 0.5]), np.array([0.3, 0.2, 0.5]))
    assert post[0] == 0.0
    assert post.sum() == pytest.approx(1.0)
