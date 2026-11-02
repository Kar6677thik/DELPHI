"""Engine-level demo-story behaviour with fixture priors (docs/06 §17, docs/21 §3 ENG-S1/S2).

These pin the behaviours the demo stories depend on, with KB v1 and every guardrail active.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pytest

from delphi.engine import (
    CompiledProfile,
    Phase,
    SessionFacts,
    decide,
    initial_budgets,
    loss_matrix,
    plan_next,
    update,
    with_sources,
)

H_ORDER = ("GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE", "UNEXPLAINED")
ALL_CONSENTS = SessionFacts(consents=frozenset({"call_status"}))


@dataclass
class Trace:
    active_probes: list[str]
    belief: np.ndarray
    decision: str

    def p(self, profile: CompiledProfile, h: str) -> float:
        return float(self.belief[profile.hypotheses.index(h)])


def run(
    profile: CompiledProfile,
    prior: tuple[float, ...],
    amount_inr: float,
    passive: dict[str, str],
    answers: dict[str, str],
    *,
    vulnerable: bool = False,
) -> Trace:
    """Phase A applies the given passive outcomes; phase B plans until STOP (no exploration)."""
    assert profile.hypotheses == H_ORDER
    belief = np.asarray(prior, dtype=np.float64)
    observed: set[str] = set()
    for key in profile.sources:  # KB source order (docs/08 §2 step 4)
        if key in passive:
            src = profile.sources[key]
            belief = update(belief, src.likelihood[src.outcomes.index(passive[key])])
            observed.add(key)
    budgets = initial_budgets(profile)
    actives: list[str] = []
    while True:
        plan = plan_next(
            profile,
            belief,
            amount_paise=amount_inr * 100,
            vulnerable=vulnerable,
            phase=Phase.GUARDIAN_ACTIVE,
            observed=frozenset(observed),
            facts=ALL_CONSENTS,
            budgets=budgets,
        )
        if plan.is_stop:
            break
        src = profile.sources[plan.action]
        belief = update(belief, src.likelihood[src.outcomes.index(answers[plan.action])])
        observed.add(plan.action)
        budgets = budgets.after(src)
        actives.append(plan.action)
    idx, _ = decide(belief, loss_matrix(profile, amount_inr * 100, vulnerable))
    return Trace(actives, belief, profile.decisions[idx])


ARJUN_PRIOR = (0.69, 0.20, 0.04, 0.03, 0.02, 0.02)
ARJUN_PASSIVE = {
    "BENEFICIARY_INTEL": "CLEAN_KNOWN_ENTITY",
    "COUNTERPARTY_PHONE_RISK": "LOW_OR_UNKNOWN",
    "DEVICE_POSTURE": "NEW_DEVICE",
    "SESSION_BEHAVIOUR": "NORMAL",
    "CALL_STATUS": "NOT_ON_CALL",
}
GENUINE_ANSWERS = {
    "OTP_STEP_UP": "PASSED",
    "BIOMETRIC_STEP_UP": "PASSED",
    "PURPOSE_QUESTION": "PLAUSIBLE_NORMAL_TIMING",
    "SPELL_BREAKER_DIGITAL_ARREST": "ANSWERED_NO_NORMAL",
    "SPELL_BREAKER_INVESTMENT": "ANSWERED_NO_NORMAL",
    "COOLING_OFF": "WAITED_NORMALLY",
}

KAMALA_PRIOR = (0.54, 0.08, 0.30, 0.05, 0.01, 0.02)
KAMALA_PASSIVE = {  # S-1 persona: coached, typing normally (docs/14 §6)
    "BENEFICIARY_INTEL": "CLEAN_UNKNOWN",
    "COUNTERPARTY_PHONE_RISK": "LOW_OR_UNKNOWN",
    "DEVICE_POSTURE": "KNOWN_DEVICE",
    "SESSION_BEHAVIOUR": "NORMAL",
    "CALL_STATUS": "ON_CALL_UNKNOWN_NUMBER",
}
KAMALA_ANSWERS = {
    **GENUINE_ANSWERS,
    "PURPOSE_QUESTION": "PLAUSIBLE_NORMAL_TIMING",  # coached "family" answer
    "SPELL_BREAKER_DIGITAL_ARREST": "HESITATION_OR_APP_SWITCH",
    "COOLING_OFF": "PERSISTED_OR_ESCALATED",
}


class TestArjun:
    """S-2: genuine, new phone, known hospital merchant, ₹1,80,000."""

    def test_biometric_first_never_otp_and_release(self, guardian: CompiledProfile) -> None:
        trace = run(guardian, ARJUN_PRIOR, 180_000, ARJUN_PASSIVE, GENUINE_ANSWERS)
        assert trace.active_probes[0] == "BIOMETRIC_STEP_UP"
        assert "OTP_STEP_UP" not in trace.active_probes
        assert len(trace.active_probes) <= 2
        assert trace.decision == "RELEASE"

    def test_without_exclusive_group_both_step_ups_would_run(
        self, guardian: CompiledProfile
    ) -> None:
        """docs/06 §17: the STEP_UP_AUTH group is what prevents a double challenge."""
        ungrouped = with_sources(
            guardian, {k: replace(s, exclusive_group=None) for k, s in guardian.sources.items()}
        )
        trace = run(ungrouped, ARJUN_PRIOR, 180_000, ARJUN_PASSIVE, GENUINE_ANSWERS)
        assert {"OTP_STEP_UP", "BIOMETRIC_STEP_UP"} <= set(trace.active_probes)


class TestKamala:
    """S-1: coached digital-arrest victim, ₹4,80,000."""

    def test_coached_victim_gets_spell_breaker_and_protective_hold(
        self, guardian: CompiledProfile
    ) -> None:
        trace = run(guardian, KAMALA_PRIOR, 480_000, KAMALA_PASSIVE, KAMALA_ANSWERS)
        assert trace.active_probes[:2] == ["PURPOSE_QUESTION", "SPELL_BREAKER_DIGITAL_ARREST"]
        assert "OTP_STEP_UP" not in trace.active_probes
        assert trace.decision == "PROTECTIVE_HOLD"
        top = guardian.hypotheses[int(np.argmax(trace.belief))]
        assert top == "APP_DIGITAL_ARREST"
        assert trace.p(guardian, "APP_DIGITAL_ARREST") >= 0.65

    def test_hesitant_session_needs_no_active_probe(self, guardian: CompiledProfile) -> None:
        passive = {**KAMALA_PASSIVE, "SESSION_BEHAVIOUR": "HESITANT_OR_PASTED"}
        trace = run(guardian, KAMALA_PRIOR, 480_000, passive, KAMALA_ANSWERS)
        assert trace.active_probes == []
        assert trace.decision == "PROTECTIVE_HOLD"
        assert trace.p(guardian, "APP_DIGITAL_ARREST") == pytest.approx(0.84, abs=0.02)


def test_genuine_elderly_mirror_is_released(guardian: CompiledProfile) -> None:
    """Same amount, on a call with a known contact: some questions, then RELEASE."""
    passive = {**KAMALA_PASSIVE, "CALL_STATUS": "ON_CALL_KNOWN_CONTACT"}
    trace = run(guardian, KAMALA_PRIOR, 480_000, passive, GENUINE_ANSWERS)
    assert trace.decision == "RELEASE"
    assert 1 <= len(trace.active_probes) <= 3
    assert "OTP_STEP_UP" not in trace.active_probes
