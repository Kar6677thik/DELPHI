"""Planner guardrails, stop reasons, tie-breaks and candidate tables (docs/06 §6–10)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from delphi.engine import (
    Budgets,
    CompiledProfile,
    Ineligible,
    Phase,
    SessionFacts,
    StopReason,
    initial_budgets,
    make_source,
    plan_next,
    static_ineligibility,
    with_sources,
)

UNIFORMISH = np.array([0.5, 0.1, 0.2, 0.1, 0.05, 0.05])
CONSENT = SessionFacts(consents=frozenset({"call_status"}))


def _plan(profile: CompiledProfile, **kw: object) -> object:
    args: dict[str, object] = {
        "amount_paise": 48_000_000,
        "vulnerable": False,
        "phase": Phase.GUARDIAN_ACTIVE,
        "facts": CONSENT,
        "budgets": initial_budgets(profile),
    }
    args.update(kw)
    return plan_next(profile, UNIFORMISH, **args)  # type: ignore[arg-type]


def reasons(plan: object) -> dict[str, Ineligible | None]:
    return {c.source_key: c.ineligible_reason for c in plan.candidates}  # type: ignore[attr-defined]


class TestStaticGuardrails:
    def test_phase_split(self, guardian: CompiledProfile) -> None:
        a = _plan(guardian, phase=Phase.GUARDIAN_PASSIVE)
        assert reasons(a)["OTP_STEP_UP"] is Ineligible.ACTIVE_PROBE_IN_PASSIVE_PHASE
        b = _plan(guardian)
        assert reasons(b)["CALL_STATUS"] is Ineligible.PASSIVE_PROBE_IN_ACTIVE_PHASE

    def test_disabled_source(self, guardian: CompiledProfile) -> None:
        assert reasons(_plan(guardian))["COLLECT_INTENT_CHECK"] is Ineligible.DISABLED

    def test_channel_restriction(self, guardian: CompiledProfile) -> None:
        enabled = with_sources(
            guardian,
            {
                **guardian.sources,
                "COLLECT_INTENT_CHECK": replace(
                    guardian.sources["COLLECT_INTENT_CHECK"], enabled=True
                ),
            },
        )
        assert reasons(_plan(enabled))["COLLECT_INTENT_CHECK"] is Ineligible.WRONG_CHANNEL
        on_collect = _plan(enabled, facts=SessionFacts(channel="UPI_COLLECT_APPROVAL"))
        assert reasons(on_collect)["COLLECT_INTENT_CHECK"] is None

    def test_consent(self, guardian: CompiledProfile) -> None:
        src = guardian.sources["CALL_STATUS"]
        no_consent = static_ineligibility(
            guardian, src, SessionFacts(), frozenset(), Phase.GUARDIAN_PASSIVE
        )
        assert no_consent is Ineligible.NO_CONSENT

    def test_language_and_fairness(self, guardian: CompiledProfile) -> None:
        plan = _plan(guardian, facts=SessionFacts(servable_question_probes=frozenset()))
        assert reasons(plan)["PURPOSE_QUESTION"] is Ineligible.NO_LANGUAGE_VARIANT
        capped = _plan(guardian, facts=SessionFacts(fairness_cap_reached=True))
        assert capped.is_stop  # type: ignore[attr-defined]
        assert capped.stop_reason is StopReason.NO_ELIGIBLE_SOURCES  # type: ignore[attr-defined]
        assert reasons(capped)["OTP_STEP_UP"] is Ineligible.FAIRNESS_CAP


class TestDynamicGuardrails:
    def test_exclusive_group(self, guardian: CompiledProfile) -> None:
        budgets = initial_budgets(guardian, ("BIOMETRIC_STEP_UP",))
        plan = _plan(guardian, budgets=budgets, observed=frozenset({"BIOMETRIC_STEP_UP"}))
        assert reasons(plan)["OTP_STEP_UP"] is Ineligible.EXCLUSIVE_GROUP

    def test_medium_budget(self, guardian: CompiledProfile) -> None:
        ran = ("SPELL_BREAKER_DIGITAL_ARREST", "SPELL_BREAKER_INVESTMENT")
        plan = _plan(guardian, budgets=initial_budgets(guardian, ran), observed=frozenset(ran))
        assert reasons(plan)["COOLING_OFF"] is Ineligible.MEDIUM_BUDGET

    def test_active_budget_exhausted(self, guardian: CompiledProfile) -> None:
        ran = ("PURPOSE_QUESTION", "BIOMETRIC_STEP_UP", "SPELL_BREAKER_INVESTMENT")
        plan = _plan(guardian, budgets=initial_budgets(guardian, ran), observed=frozenset(ran))
        assert plan.is_stop  # type: ignore[attr-defined]
        assert plan.stop_reason is StopReason.BUDGET_EXHAUSTED  # type: ignore[attr-defined]
        assert plan.lookahead_depth == 0  # type: ignore[attr-defined]

    def test_budget_after(self, guardian: CompiledProfile) -> None:
        b = Budgets(3, 2).after(guardian.sources["SPELL_BREAKER_DIGITAL_ARREST"])
        assert b == Budgets(2, 1, frozenset())
        b = b.after(guardian.sources["OTP_STEP_UP"])
        assert b == Budgets(1, 1, frozenset({"STEP_UP_AUTH"}))


class TestSelection:
    def test_stop_when_decision_cannot_change(self, guardian: CompiledProfile) -> None:
        certain = np.array([0.0001, 0.0001, 0.9995, 0.0001, 0.0001, 0.0001])
        plan = plan_next(
            guardian,
            certain,
            amount_paise=48_000_000,
            vulnerable=False,
            phase=Phase.GUARDIAN_ACTIVE,
            facts=CONSENT,
            budgets=initial_budgets(guardian),
        )
        assert plan.is_stop
        assert plan.stop_reason is StopReason.NO_POSITIVE_VALUE
        assert plan.recommended_decision == "PROTECTIVE_HOLD"

    def test_ties_go_to_cheaper_source(self, guardian: CompiledProfile) -> None:
        hyps = guardian.hypotheses
        rows = {"GENUINE": [0.9, 0.1], "ATO": [0.1, 0.9]}
        a = make_source("A_DEAR", ["X", "Y"], hyps, rows, cost_paise=500.0)
        b = make_source("B_CHEAP", ["X", "Y"], hyps, rows, cost_paise=499.5)  # within 1 paise
        prof = with_sources(guardian, {"A_DEAR": a, "B_CHEAP": b})
        belief = np.array([0.6, 0.4, 1e-6, 1e-6, 1e-6, 1e-6])
        plan = plan_next(
            prof,
            belief / belief.sum(),
            amount_paise=18_000_000,
            vulnerable=False,
            phase=Phase.GUARDIAN_ACTIVE,
            budgets=initial_budgets(prof),
        )
        assert plan.action == "B_CHEAP"

    def test_candidates_table(self, guardian: CompiledProfile) -> None:
        plan = _plan(guardian, observed=frozenset({"CALL_STATUS"}))
        table = {c.source_key: c for c in plan.candidates}  # type: ignore[attr-defined]
        assert table["CALL_STATUS"].voi_myopic_paise is None  # already observed
        assert table["OTP_STEP_UP"].voi_myopic_paise is not None
        eligible = [c for c in plan.candidates if c.eligible]  # type: ignore[attr-defined]
        nets = [c.net_value_paise for c in eligible]
        assert nets == sorted(nets, reverse=True)
        assert all(0.0 <= (c.settle_pct or 0.0) <= 100.0 for c in plan.candidates)  # type: ignore[attr-defined]

    def test_navigator_uses_its_own_depth(self, navigator_hold: CompiledProfile) -> None:
        belief = np.array([0.4, 0.1, 0.3, 0.1, 0.05, 0.05])
        plan = plan_next(
            navigator_hold, belief, amount_paise=48_000_000, vulnerable=False, phase=Phase.NAVIGATOR
        )
        assert plan.lookahead_depth == 2
        assert plan.selected_by is None or plan.propensity is None  # no exploration

    def test_vulnerability_scales_only_flagged_cells(self, guardian: CompiledProfile) -> None:
        """docs/07 §8: RELEASE is scaled ×1.5 for ATO/APP_*/UNEXPLAINED but not for MULE.
        Belief (.5,.1,.2,.1,.05,.05), A = ₹4,80,000:
        scaled part ₹43,200 + ₹91,200 + ₹45,600 + ₹14,400 = ₹1,94,400; MULE ₹12,000."""
        a = _plan(guardian)
        b = _plan(guardian, vulnerable=True)
        assert a.risk_by_decision["RELEASE"] == pytest.approx(206_400 * 100)  # type: ignore[attr-defined]
        assert b.risk_by_decision["RELEASE"] == pytest.approx(  # type: ignore[attr-defined]
            (194_400 * 1.5 + 12_000) * 100
        )
