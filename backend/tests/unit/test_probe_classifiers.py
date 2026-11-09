"""Deterministic outcome classifiers for the Guardian probes (docs/08 §5, FR-GDN-05).

One test per classifier rule and per threshold edge; every threshold comes from KB v1.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from delphi.kb.model import ClassifierThresholds, KnowledgeBase
from delphi.probes.classifiers import (
    COLLECT_RECEIVING_OPTION,
    GUARDIAN_CLASSIFIERS,
    HIGH_RISK_PURPOSES,
    RECEIVING_MONEY_PURPOSE,
    AnswerObs,
    BeneficiaryIntelObs,
    BiometricObs,
    CallStatusObs,
    CoolingOffObs,
    DevicePostureObs,
    OtpObs,
    PhoneRiskObs,
    SessionBehaviourObs,
    beneficiary_intel,
    biometric_step_up,
    call_status,
    collect_intent_check,
    cooling_off,
    counterparty_phone_risk,
    device_posture,
    otp_step_up,
    purpose_question,
    session_behaviour,
    spell_breaker,
)
from delphi.probes.registry import CLASSIFIER_NAMES, NAVIGATOR_EVIDENCE_NAMES

AS_OF = datetime(2026, 11, 8, 18, 10, tzinfo=UTC)
TINY = 1e-6


@pytest.fixture(scope="module")
def t(kb: KnowledgeBase) -> ClassifierThresholds:
    return kb.config.classifiers


# ---------------------------------------------------------------- registry vs KB


def test_every_guardian_probe_has_a_classifier_with_exactly_its_kb_outcomes(
    kb: KnowledgeBase,
) -> None:
    probes = {key: src for key, src in kb.sources.items() if key not in NAVIGATOR_EVIDENCE_NAMES}
    assert set(probes) == set(GUARDIAN_CLASSIFIERS)
    for key, source in probes.items():
        assert source.classifier == key
        assert GUARDIAN_CLASSIFIERS[key].outcomes == frozenset(source.outcomes), key


def test_registry_lists_every_kb_classifier(kb: KnowledgeBase) -> None:
    assert {source.classifier for source in kb.sources.values()} == CLASSIFIER_NAMES


def test_answer_options_used_by_the_rules_exist_in_the_question_bank(kb: KnowledgeBase) -> None:
    def options(probe: str) -> set[str]:
        return {o.key for v in kb.question_bank[probe].variants for o in v.options}

    assert {*HIGH_RISK_PURPOSES, RECEIVING_MONEY_PURPOSE} <= options("PURPOSE_QUESTION")
    assert COLLECT_RECEIVING_OPTION in options("COLLECT_INTENT_CHECK")
    for probe in ("SPELL_BREAKER_DIGITAL_ARREST", "SPELL_BREAKER_INVESTMENT"):
        assert options(probe) == {"YES", "NO"}


# ---------------------------------------------------------------- BENEFICIARY_INTEL


def _payee(t: ClassifierThresholds, **changes: object) -> BeneficiaryIntelObs:
    established = BeneficiaryIntelObs(
        listed_in_suspect_registry=False,
        p_mule=0.1,
        is_registered_merchant=False,
        earlier_payments=t.established_payee_min_payments,
        earlier_distinct_customers=t.established_payee_min_distinct_customers,
        first_payment_at=AS_OF - timedelta(days=t.established_payee_min_age_days),
        as_of=AS_OF,
    )
    return replace(established, **changes)  # type: ignore[arg-type]


def test_beneficiary_listed_wins_over_everything(t: ClassifierThresholds) -> None:
    obs = _payee(t, listed_in_suspect_registry=True, p_mule=0.99, is_registered_merchant=True)
    assert beneficiary_intel(obs, t) == "LISTED_SUSPECT"


def test_beneficiary_mule_score_edge(t: ClassifierThresholds) -> None:
    assert beneficiary_intel(_payee(t, p_mule=t.mule_score_high), t) == "HIGH_MULE_SCORE"
    below = _payee(t, p_mule=t.mule_score_high - TINY)
    assert beneficiary_intel(below, t) == "CLEAN_KNOWN_ENTITY"


def test_beneficiary_registered_merchant_is_known(t: ClassifierThresholds) -> None:
    obs = _payee(t, is_registered_merchant=True, earlier_payments=0, first_payment_at=None)
    assert beneficiary_intel(obs, t) == "CLEAN_KNOWN_ENTITY"


def test_beneficiary_established_payee_at_every_minimum_is_known(t: ClassifierThresholds) -> None:
    assert beneficiary_intel(_payee(t), t) == "CLEAN_KNOWN_ENTITY"


@pytest.mark.parametrize(
    "change",
    [
        "payments",
        "customers",
        "age",
        "never_paid",
    ],
)
def test_beneficiary_one_short_of_established_is_unknown(
    t: ClassifierThresholds, change: str
) -> None:
    changes: dict[str, object] = {
        "payments": {"earlier_payments": t.established_payee_min_payments - 1},
        "customers": {"earlier_distinct_customers": t.established_payee_min_distinct_customers - 1},
        "age": {
            "first_payment_at": AS_OF
            - timedelta(days=t.established_payee_min_age_days)
            + timedelta(seconds=1)
        },
        "never_paid": {"first_payment_at": None},
    }[change]  # type: ignore[assignment]
    assert beneficiary_intel(_payee(t, **changes), t) == "CLEAN_UNKNOWN"  # type: ignore[arg-type]


# ---------------------------------------------------------------- COUNTERPARTY_PHONE_RISK


@pytest.mark.parametrize(
    ("category", "outcome"),
    [
        ("VERY_HIGH", "VERY_HIGH"),
        ("HIGH", "MEDIUM_HIGH"),
        ("MEDIUM", "MEDIUM_HIGH"),
        ("LOW", "LOW_OR_UNKNOWN"),
        (None, "LOW_OR_UNKNOWN"),
    ],
)
def test_counterparty_phone_risk(
    t: ClassifierThresholds, category: str | None, outcome: str
) -> None:
    assert counterparty_phone_risk(PhoneRiskObs(category), t) == outcome


# ---------------------------------------------------------------- DEVICE_POSTURE


def _device(t: ClassifierThresholds, age: timedelta, **flags: bool) -> DevicePostureObs:
    return DevicePostureObs(
        remote_access_active=flags.get("remote", False),
        screen_share_active=flags.get("screen", False),
        device_first_seen_at=AS_OF - age,
        as_of=AS_OF,
    )


@pytest.mark.parametrize("flag", ["remote", "screen"])
def test_device_remote_access_or_screen_share_wins(t: ClassifierThresholds, flag: str) -> None:
    assert device_posture(_device(t, timedelta(0), **{flag: True}), t) == "REMOTE_ACCESS_ACTIVE"


def test_device_new_device_edge(t: ClassifierThresholds) -> None:
    week = timedelta(days=t.new_device_days)
    assert device_posture(_device(t, week - timedelta(seconds=1)), t) == "NEW_DEVICE"
    assert device_posture(_device(t, week), t) == "KNOWN_DEVICE"


# ---------------------------------------------------------------- SESSION_BEHAVIOUR


def _session(
    t: ClassifierThresholds, paste: bool = False, dwell: float = 1.0, z: float = 0.0
) -> SessionBehaviourObs:
    return SessionBehaviourObs(paste, dwell, z)


def test_session_paste_is_hesitant_even_with_cadence_mismatch(t: ClassifierThresholds) -> None:
    assert session_behaviour(_session(t, paste=True, z=99.0), t) == "HESITANT_OR_PASTED"


def test_session_dwell_edge(t: ClassifierThresholds) -> None:
    limit = t.paste_or_dwell_max_field_dwell_s
    assert session_behaviour(_session(t, dwell=limit + TINY), t) == "HESITANT_OR_PASTED"
    assert session_behaviour(_session(t, dwell=limit), t) == "NORMAL"


def test_session_cadence_edge(t: ClassifierThresholds) -> None:
    z = t.cadence_mismatch_z
    assert session_behaviour(_session(t, z=z + TINY), t) == "CADENCE_MISMATCH"
    assert session_behaviour(_session(t, z=z), t) == "NORMAL"


# ---------------------------------------------------------------- CALL_STATUS


@pytest.mark.parametrize(
    ("on_call", "in_contacts", "outcome"),
    [
        (True, False, "ON_CALL_UNKNOWN_NUMBER"),
        (True, None, "ON_CALL_UNKNOWN_NUMBER"),
        (True, True, "ON_CALL_KNOWN_CONTACT"),
        (False, None, "NOT_ON_CALL"),
    ],
)
def test_call_status(
    t: ClassifierThresholds, on_call: bool, in_contacts: bool | None, outcome: str
) -> None:
    assert call_status(CallStatusObs(on_call, in_contacts), t) == outcome


# ---------------------------------------------------------------- step-ups


def test_otp_attempts_edge(t: ClassifierThresholds) -> None:
    assert otp_step_up(OtpObs(True, t.otp_max_attempts, False), t) == "PASSED"
    assert otp_step_up(OtpObs(True, t.otp_max_attempts + 1, False), t) == "FAILED_OR_ABANDONED"


@pytest.mark.parametrize(("correct", "timed_out"), [(False, False), (True, True)])
def test_otp_wrong_or_late_fails(t: ClassifierThresholds, correct: bool, timed_out: bool) -> None:
    assert otp_step_up(OtpObs(correct, 1, timed_out), t) == "FAILED_OR_ABANDONED"


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ("MATCH", "PASSED"),
        ("NO_MATCH", "FAILED_OR_ABANDONED"),
        ("CANCEL", "FAILED_OR_ABANDONED"),
        ("TIMEOUT", "FAILED_OR_ABANDONED"),
    ],
)
def test_biometric(t: ClassifierThresholds, result: str, outcome: str) -> None:
    assert biometric_step_up(BiometricObs(result), t) == outcome  # type: ignore[arg-type]


# ---------------------------------------------------------------- PURPOSE_QUESTION


def test_purpose_receiving_money_wins_over_timing(t: ClassifierThresholds) -> None:
    obs = AnswerObs(RECEIVING_MONEY_PURPOSE, latency_s=0.1, app_switch_count=3)
    assert purpose_question(obs, t) == "BELIEVES_RECEIVING_MONEY"


@pytest.mark.parametrize("option", sorted(HIGH_RISK_PURPOSES))
def test_purpose_high_risk_wins_over_timing(t: ClassifierThresholds, option: str) -> None:
    assert purpose_question(AnswerObs(option, 0.1, 2), t) == "HIGH_RISK_PURPOSE"


def test_purpose_timing_edges(t: ClassifierThresholds) -> None:
    def outcome(latency: float, switches: int = 0) -> str:
        return purpose_question(AnswerObs("FAMILY_OR_FRIEND", latency, switches), t)

    assert outcome(t.purpose_fast_s - TINY) == "TIMING_ANOMALOUS"
    assert outcome(t.purpose_fast_s) == "PLAUSIBLE_NORMAL_TIMING"
    assert outcome(t.purpose_slow_s) == "PLAUSIBLE_NORMAL_TIMING"
    assert outcome(t.purpose_slow_s + TINY) == "TIMING_ANOMALOUS"
    assert outcome(5.0, switches=1) == "TIMING_ANOMALOUS"


# ---------------------------------------------------------------- SPELL_BREAKER_*


def test_spell_breaker_yes_wins(t: ClassifierThresholds) -> None:
    assert spell_breaker(AnswerObs("YES", 30.0, 2), t) == "ANSWERED_YES"


def test_spell_breaker_hesitation_edges(t: ClassifierThresholds) -> None:
    limit = t.spell_breaker_hesitation_s
    assert spell_breaker(AnswerObs("NO", limit + TINY, 0), t) == "HESITATION_OR_APP_SWITCH"
    assert spell_breaker(AnswerObs("NO", limit, 0), t) == "ANSWERED_NO_NORMAL"
    assert spell_breaker(AnswerObs("NO", 2.0, 1), t) == "HESITATION_OR_APP_SWITCH"


def test_both_spell_breakers_share_the_rule() -> None:
    assert (
        GUARDIAN_CLASSIFIERS["SPELL_BREAKER_DIGITAL_ARREST"].classify
        is GUARDIAN_CLASSIFIERS["SPELL_BREAKER_INVESTMENT"].classify
        is spell_breaker
    )


# ---------------------------------------------------------------- COOLING_OFF


def test_cooling_off_cancel_wins(t: ClassifierThresholds) -> None:
    assert cooling_off(CoolingOffObs(True, 3, 3, 9), t) == "CANCELLED_BY_CUSTOMER"


@pytest.mark.parametrize(("other", "limit"), [(1, 0), (0, 1)])
def test_cooling_off_escalation(t: ClassifierThresholds, other: int, limit: int) -> None:
    assert cooling_off(CoolingOffObs(False, other, limit, 0), t) == "PERSISTED_OR_ESCALATED"


def test_cooling_off_send_now_edge(t: ClassifierThresholds) -> None:
    presses = t.cooling_off_send_now_presses
    assert cooling_off(CoolingOffObs(False, 0, 0, presses), t) == "PERSISTED_OR_ESCALATED"
    assert cooling_off(CoolingOffObs(False, 0, 0, presses - 1), t) == "WAITED_NORMALLY"


# ---------------------------------------------------------------- COLLECT_INTENT_CHECK


@pytest.mark.parametrize(
    ("option", "outcome"),
    [("YES_RECEIVING", "EXPECTS_TO_RECEIVE"), ("NO_I_AM_PAYING", "UNDERSTANDS_PAYING")],
)
def test_collect_intent_check(t: ClassifierThresholds, option: str, outcome: str) -> None:
    assert collect_intent_check(AnswerObs(option, 3.0, 0), t) == outcome
