"""The case console's decision support (docs/09 §2, docs/06 §6, §12–13, #103)."""

from __future__ import annotations

import pytest

from delphi.engine.explain import what_would_change_my_mind
from delphi.engine.risk import loss_matrix
from delphi.kb.model import KnowledgeBase
from delphi.navigator.console import OUTREACH, belief_vector, case_profile, console

UNSURE = {
    "GENUINE": 0.40,
    "ATO": 0.05,
    "APP_DIGITAL_ARREST": 0.40,
    "APP_INVESTMENT": 0.05,
    "MULE": 0.05,
    "UNEXPLAINED": 0.05,
}
CERTAIN = {"GENUINE": 0.0001, "APP_DIGITAL_ARREST": 0.9999}
AMOUNT = 48_000_000


def test_next_best_evidence_is_ranked_by_net_value_with_minutes_and_settle(
    kb: KnowledgeBase,
) -> None:
    profile = case_profile(kb, "PAYMENT_HOLD")
    view = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset(),
    )
    eligible = [r for r in view.next_best_evidence if r.eligible]
    assert eligible, "an unsure ₹4,80,000 hold has evidence worth fetching"
    nets = [r.net_value_paise for r in eligible]
    assert nets == sorted(nets, reverse=True)  # ranking always uses net value (doc 06 §13)
    for row in eligible:
        assert row.expected_minutes == kb.sources[row.source_key].expected_minutes
        assert row.settle_pct is not None and 0 <= row.settle_pct <= 100
    assert all(not r.pinned_by_protocol for r in view.next_best_evidence)
    assert not view.evidence_sufficient
    assert view.recommended_decision in profile.decisions
    assert set(view.risk_by_decision) == set(profile.decisions)


def test_observed_evidence_is_not_offered_again(kb: KnowledgeBase) -> None:
    profile = case_profile(kb, "PAYMENT_HOLD")
    first = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset(),
    )
    top = next(r.source_key for r in first.next_best_evidence if r.eligible)
    again = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset({top}),
    )
    assert top not in {r.source_key for r in again.next_best_evidence}


def test_the_coercion_protocol_pins_the_outreach_call_until_done(kb: KnowledgeBase) -> None:
    profile = case_profile(kb, "PAYMENT_HOLD")
    view = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="COERCION",
        observed=frozenset(),
    )
    assert view.next_best_evidence[0].source_key == OUTREACH
    assert view.next_best_evidence[0].pinned_by_protocol
    done = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="COERCION",
        observed=frozenset({OUTREACH}),
    )
    assert OUTREACH not in {r.source_key for r in done.next_best_evidence}


def test_a_settled_case_reports_evidence_sufficient(kb: KnowledgeBase) -> None:
    profile = case_profile(kb, "PAYMENT_HOLD")
    view = console(
        kb,
        profile,
        posterior=CERTAIN,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset(),
    )
    assert view.evidence_sufficient and view.recommended_decision == "CONFIRM_FRAUD"
    assert view.posterior["APP_DIGITAL_ARREST"] == pytest.approx(0.9999)


def test_wcmm_is_the_engines_answer_over_the_unobserved_evidence(kb: KnowledgeBase) -> None:
    profile = case_profile(kb, "PAYMENT_HOLD")
    view = console(
        kb,
        profile,
        posterior=UNSURE,
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset(),
    )
    belief = belief_vector(profile, UNSURE)
    expected = what_would_change_my_mind(
        profile,
        belief,
        loss_matrix(profile, AMOUNT, False),
        [r.source_key for r in view.next_best_evidence if r.eligible],
    )
    assert view.what_would_change_my_mind == expected


def test_an_account_review_uses_its_own_profile(kb: KnowledgeBase) -> None:
    profile = case_profile(kb, "ACCOUNT_REVIEW")
    view = console(
        kb,
        profile,
        posterior={"GENUINE": 0.5, "MULE": 0.5},
        amount_paise=AMOUNT,
        vulnerable=False,
        protocol="STANDARD",
        observed=frozenset(),
    )
    assert set(view.risk_by_decision) == {"CLEAR", "CONFIRM_MULE"}


def test_a_posterior_with_no_mass_on_the_profile_is_refused(kb: KnowledgeBase) -> None:
    with pytest.raises(ValueError, match="no mass"):
        belief_vector(case_profile(kb, "PAYMENT_HOLD"), {"NOT_A_HYPOTHESIS": 1.0})
