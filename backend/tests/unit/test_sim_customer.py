"""The simulated customer's raw answers classify back to the drawn outcome (docs/14 §5.2,
docs/08 §5, #95): the simulation answers through the channel exactly as the response models
say, with nothing lost or invented between the outcome and the classifier."""

from __future__ import annotations

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.probes.api import (
    GUARDIAN_CLASSIFIERS,
    AnswerObs,
    BiometricObs,
    CoolingOffObs,
    OtpObs,
)
from delphi.sim.customer import AnswerStep, load_timings, raw_answer
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType

ACTIVE_PROBES = (
    "PURPOSE_QUESTION",
    "SPELL_BREAKER_DIGITAL_ARREST",
    "SPELL_BREAKER_INVESTMENT",
    "COLLECT_INTENT_CHECK",
    "BIOMETRIC_STEP_UP",
    "OTP_STEP_UP",
    "COOLING_OFF",
)
CODE = "424242"
TIMEOUT_S = 60.0


def _classify(kb: KnowledgeBase, probe: str, steps: list[AnswerStep]) -> str:
    """What the Guardian makes of the steps (the same rules as GuardianService.respond)."""
    t = kb.config.classifiers
    if probe == "OTP_STEP_UP":
        for attempt, step in enumerate(steps, start=1):
            if step.answer["otp"] == CODE:
                return GUARDIAN_CLASSIFIERS[probe].classify(OtpObs(True, attempt, False), t)
        return GUARDIAN_CLASSIFIERS[probe].classify(OtpObs(False, len(steps), False), t)
    [step] = steps
    answer = step.answer
    if probe == "BIOMETRIC_STEP_UP":
        obs: object = BiometricObs(answer["biometric"])
    elif probe == "COOLING_OFF":
        raw = answer["cooling_off"]
        obs = CoolingOffObs(
            raw["cancelled"],
            raw.get("other_channel_attempts", 0),
            raw.get("limit_increase_requests", 0),
            raw.get("send_now_presses", 0),
        )
    else:
        obs = AnswerObs(answer["option_key"], step.latency_s, step.app_switches)
    return GUARDIAN_CLASSIFIERS[probe].classify(obs, t)


@pytest.mark.parametrize("probe", ACTIVE_PROBES)
def test_every_outcome_round_trips_through_the_real_classifier(
    kb: KnowledgeBase, probe: str
) -> None:
    options = (
        tuple(o.key for v in kb.question_bank[probe].variants for o in v.options)
        if probe in kb.question_bank
        else ()
    )
    for outcome in GUARDIAN_CLASSIFIERS[probe].outcomes:
        for scenario in ScenarioType:
            for seed in range(40):
                rng = keyed_rng(seed, "test", probe, outcome, scenario.value)
                steps = raw_answer(
                    probe,
                    outcome,
                    rng,
                    kb.config.classifiers,
                    scenario=scenario,
                    options=options,
                    otp_code=CODE,
                    timeout_s=TIMEOUT_S,
                )
                assert _classify(kb, probe, steps) == outcome, (probe, outcome, scenario, seed)
                for step in steps:
                    assert 0 < step.latency_s <= TIMEOUT_S  # always inside the deadline


def test_a_scam_pushes_its_own_high_risk_purpose(kb: KnowledgeBase) -> None:
    options = [o.key for o in kb.question_bank["PURPOSE_QUESTION"].variants[0].options]
    picked = {
        raw_answer(
            "PURPOSE_QUESTION",
            "HIGH_RISK_PURPOSE",
            keyed_rng(seed, "x"),
            kb.config.classifiers,
            scenario=ScenarioType.APP_INVESTMENT,
            options=options,
            otp_code=None,
            timeout_s=TIMEOUT_S,
        )[0].answer["option_key"]
        for seed in range(20)
    }
    assert picked == {"INVESTMENT"}


def test_an_unknown_probe_has_no_simulated_answer(kb: KnowledgeBase) -> None:
    with pytest.raises(ValueError, match="no simulated answer"):
        raw_answer(
            "CALL_STATUS",
            "NOT_ON_CALL",
            keyed_rng(1, "x"),
            kb.config.classifiers,
            scenario=ScenarioType.GENUINE_ROUTINE,
            options=(),
            otp_code=None,
            timeout_s=TIMEOUT_S,
        )


def test_all_wrong_otp_attempts_fit_inside_the_deadline(kb: KnowledgeBase) -> None:
    for seed in range(50):
        steps = raw_answer(
            "OTP_STEP_UP",
            "FAILED_OR_ABANDONED",
            keyed_rng(seed, "otp"),
            kb.config.classifiers,
            scenario=ScenarioType.ATO,
            options=(),
            otp_code=CODE,
            timeout_s=TIMEOUT_S,
        )
        assert len(steps) == kb.config.classifiers.otp_max_attempts
        assert sum(s.latency_s for s in steps) < TIMEOUT_S


def test_a_question_without_the_wanted_option_is_refused(kb: KnowledgeBase) -> None:
    with pytest.raises(ValueError, match="none of"):
        raw_answer(
            "PURPOSE_QUESTION",
            "BELIEVES_RECEIVING_MONEY",
            keyed_rng(1, "x"),
            kb.config.classifiers,
            scenario=ScenarioType.PRIZE_ADVANCE_FEE,
            options=("FAMILY_OR_FRIEND", "MEDICAL"),  # no RECEIVING_REFUND_OR_PRIZE offered
            otp_code=None,
            timeout_s=TIMEOUT_S,
        )


def test_the_timings_config_loads_and_stays_inside_the_kb_thresholds(kb: KnowledgeBase) -> None:
    t, c = kb.config.classifiers, load_timings()
    assert c.quick_answer_s[1] < t.spell_breaker_hesitation_s - c.margin_s
    assert c.quick_answer_s[0] > t.purpose_fast_s  # quick answers are not "too fast" purposes
    assert (
        c.otp_typing_s[1] * t.otp_max_attempts
        <= kb.config.guardrails.active_probe_response_timeout_s
    )
