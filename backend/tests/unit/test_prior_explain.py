"""Prior construction, fast path, explanations, information measures, seeding."""

from __future__ import annotations

import numpy as np
import pytest

from delphi.engine import (
    CompiledProfile,
    as_belief,
    build_prior,
    entropy_bits,
    is_fast_path,
    kl_bits,
    loss_matrix,
    open_mass,
    session_seed,
    settle_pct,
    update,
    what_would_change_my_mind,
)
from delphi.engine.compile import compile_profile
from delphi.kb.model import KnowledgeBase

P_MODEL = {
    "GENUINE": 0.7,
    "ATO": 0.2,
    "APP_DIGITAL_ARREST": 0.05,
    "APP_INVESTMENT": 0.03,
    "MULE": 0.02,
}


class TestOpenMass:
    @pytest.mark.parametrize(("q", "u"), [(0.0, 0.02), (0.9, 0.02), (0.95, 0.11), (1.0, 0.20)])
    def test_open_mass(self, q: float, u: float) -> None:
        assert open_mass(q, 0.02, 0.18) == pytest.approx(u)

    def test_prior_all_model_classes(self, guardian: CompiledProfile) -> None:
        prior = build_prior(guardian, P_MODEL, 0.5)
        assert prior.sum() == pytest.approx(1.0)
        assert prior[-1] == pytest.approx(0.02)  # UNEXPLAINED gets all of u
        assert prior[0] == pytest.approx(0.98 * 0.7)

    def test_kb_only_typology_takes_a_share_of_open_mass(self, guardian: CompiledProfile) -> None:
        p_model = {k: v for k, v in P_MODEL.items() if k != "APP_INVESTMENT"}
        prior = build_prior(guardian, p_model, 1.0)  # u = 0.20; INV share 0.5
        h = guardian.hypotheses
        assert prior[h.index("APP_INVESTMENT")] == pytest.approx(0.10)
        assert prior[h.index("UNEXPLAINED")] == pytest.approx(0.10)
        assert prior.sum() == pytest.approx(1.0)

    def test_rejects_disjoint_model(self, guardian: CompiledProfile) -> None:
        with pytest.raises(ValueError, match="shares no hypothesis"):
            build_prior(guardian, {"SOMETHING_ELSE": 1.0}, 0.5)

    def test_account_review_prior_from_mule_score(self, kb: KnowledgeBase) -> None:
        prof = compile_profile(kb, "CASE_ACCOUNT_REVIEW")
        assert prof.hypotheses == ("GENUINE", "MULE", "UNEXPLAINED")
        prior = build_prior(prof, {"MULE": 0.8, "GENUINE": 0.2}, 0.0)
        np.testing.assert_allclose(prior, [0.98 * 0.2, 0.98 * 0.8, 0.02])


@pytest.mark.parametrize(
    ("p_g", "amount_inr", "q", "expected"),
    [
        (0.996, 9_000, 0.5, True),
        (0.994, 9_000, 0.5, False),
        (0.999, 10_001, 0.5, False),
        (0.999, 5_000, 0.95, False),
        (0.995, 10_000, 0.89, True),
    ],
)
def test_fast_path(p_g: float, amount_inr: float, q: float, expected: bool) -> None:
    assert (
        is_fast_path(
            p_g,
            amount_inr * 100,
            q,
            min_p_genuine=0.995,
            max_amount_inr=10_000,
            max_anomaly_percentile=0.90,
        )
        is expected
    )


class TestExplain:
    def test_wcmm_finds_the_most_likely_flip(self, navigator_hold: CompiledProfile) -> None:
        """Near the boundary (CONFIRM_FRAUD at 10% fraud mass), clean device-sharing evidence
        — 85% likely — would flip the recommendation to RELEASE."""
        belief = as_belief([0.90, 0.05, 0.03, 0.01, 0.005, 0.005])
        losses = loss_matrix(navigator_hold, 48_000_000, False)
        assert navigator_hold.decisions[int(np.argmin(losses @ belief))] == "CONFIRM_FRAUD"
        flip = what_would_change_my_mind(navigator_hold, belief, losses, navigator_hold.sources)
        assert flip is not None
        assert (flip.source_key, flip.outcome) == ("BENEFICIARY_DEVICE_SHARING", "NOT_SHARED")
        assert flip.new_decision == "RELEASE"
        assert flip.probability == pytest.approx(0.852, abs=1e-3)

    def test_wcmm_none_when_far_from_boundary(self, navigator_hold: CompiledProfile) -> None:
        """55% fraud mass on ₹4.8 lakh: no single item can justify a release."""
        belief = as_belief([0.45, 0.05, 0.40, 0.05, 0.03, 0.02])
        losses = loss_matrix(navigator_hold, 48_000_000, False)
        assert (
            what_would_change_my_mind(navigator_hold, belief, losses, navigator_hold.sources)
            is None
        )

    def test_wcmm_none_when_certain(self, navigator_hold: CompiledProfile) -> None:
        belief = as_belief([1e-9, 1e-9, 1.0, 1e-9, 1e-9, 1e-9])
        losses = loss_matrix(navigator_hold, 48_000_000, False)
        assert (
            what_would_change_my_mind(navigator_hold, belief, losses, navigator_hold.sources)
            is None
        )

    def test_information_measures(self, guardian: CompiledProfile) -> None:
        p = as_belief([0.5, 0.1, 0.2, 0.1, 0.05, 0.05])
        src = guardian.sources["CALL_STATUS"]
        post = update(p, src.likelihood[0])
        assert kl_bits(post, p) > 0
        assert kl_bits(p, p) == pytest.approx(0.0)
        assert 0 < settle_pct(p, src.likelihood) < 100
        assert entropy_bits(as_belief([1, 0, 0])) == 0.0
        assert entropy_bits(as_belief([1, 1])) == pytest.approx(1.0)

    def test_update_rejects_impossible_observation(self) -> None:
        with pytest.raises(ValueError, match="zero probability"):
            update(as_belief([1.0, 0.0]), np.array([0.0, 1.0]))


def test_session_seed_is_deterministic_and_seed_dependent() -> None:
    a = session_seed("018f2a4e-0000-7000-8000-000000000001", 20261031)
    assert a == session_seed("018f2a4e-0000-7000-8000-000000000001", 20261031)
    assert a != session_seed("018f2a4e-0000-7000-8000-000000000001", 7)
    assert 0 <= a < 2**63
