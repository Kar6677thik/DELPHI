"""Deterministic outcome classifiers for the Guardian probes (docs/08 §5, FR-GDN-05).

Each classifier maps one probe's raw observation (adapter data or the customer's response) to
one of the probe's outcome keys in ``kb/sources/probes``. Rules are applied in the order of
docs/08 §5; the first match wins. Thresholds come from ``kb/engine.yaml`` (``classifiers:``).

Classifiers are pure: no I/O, no clock and no randomness. Time is passed in (``as_of``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from delphi.kb.model import ClassifierThresholds

# ---------------------------------------------------------------- observations


@dataclass(frozen=True, slots=True)
class BeneficiaryIntelObs:
    """The payee account as the bank sees it at ``as_of``."""

    listed_in_suspect_registry: bool
    p_mule: float
    is_registered_merchant: bool
    earlier_payments: int  # payments to this payee account before as_of, by any customer
    earlier_distinct_customers: int
    first_payment_at: datetime | None  # earliest of those payments
    as_of: datetime


@dataclass(frozen=True, slots=True)
class PhoneRiskObs:
    fri_category: str | None  # sim FRI category of the phone linked to the payee VPA


@dataclass(frozen=True, slots=True)
class DevicePostureObs:
    remote_access_active: bool
    screen_share_active: bool
    device_first_seen_at: datetime
    as_of: datetime


@dataclass(frozen=True, slots=True)
class SessionBehaviourObs:
    paste_on_account_field: bool
    max_field_dwell_s: float
    typing_cadence_z: float  # vs the customer's 30-day baseline


@dataclass(frozen=True, slots=True)
class CallStatusObs:
    on_call: bool
    counterparty_in_contacts: bool | None  # None when not on a call or unknown


@dataclass(frozen=True, slots=True)
class OtpObs:
    correct: bool
    attempts: int
    timed_out: bool


@dataclass(frozen=True, slots=True)
class BiometricObs:
    result: Literal["MATCH", "NO_MATCH", "CANCEL", "TIMEOUT"]


@dataclass(frozen=True, slots=True)
class AnswerObs:
    """A question probe's response: the chosen option key and how it was given."""

    option: str
    latency_s: float
    app_switch_count: int


@dataclass(frozen=True, slots=True)
class CoolingOffObs:
    cancelled: bool
    other_channel_attempts: int
    limit_increase_requests: int
    send_now_presses: int


# ---------------------------------------------------------------- classifiers

HIGH_RISK_PURPOSES = frozenset(
    {"INVESTMENT", "GOVT_POLICE_OR_COURT_FEE", "SAFE_ACCOUNT_TRANSFER", "CUSTOMS_OR_PARCEL"}
)
RECEIVING_MONEY_PURPOSE = "RECEIVING_REFUND_OR_PRIZE"
COLLECT_RECEIVING_OPTION = "YES_RECEIVING"
YES_OPTION = "YES"


def beneficiary_intel(obs: BeneficiaryIntelObs, t: ClassifierThresholds) -> str:
    if obs.listed_in_suspect_registry:
        return "LISTED_SUSPECT"
    if obs.p_mule >= t.mule_score_high:
        return "HIGH_MULE_SCORE"
    established = (
        obs.first_payment_at is not None
        and obs.earlier_payments >= t.established_payee_min_payments
        and obs.earlier_distinct_customers >= t.established_payee_min_distinct_customers
        and obs.as_of - obs.first_payment_at >= timedelta(days=t.established_payee_min_age_days)
    )
    if obs.is_registered_merchant or established:
        return "CLEAN_KNOWN_ENTITY"
    return "CLEAN_UNKNOWN"


def counterparty_phone_risk(obs: PhoneRiskObs, t: ClassifierThresholds) -> str:
    if obs.fri_category == "VERY_HIGH":
        return "VERY_HIGH"
    if obs.fri_category in {"HIGH", "MEDIUM"}:
        return "MEDIUM_HIGH"
    return "LOW_OR_UNKNOWN"


def device_posture(obs: DevicePostureObs, t: ClassifierThresholds) -> str:
    if obs.remote_access_active or obs.screen_share_active:
        return "REMOTE_ACCESS_ACTIVE"
    # docs/13 §3: is_new_device = device_age_days < new_device_days
    if obs.as_of - obs.device_first_seen_at < timedelta(days=t.new_device_days):
        return "NEW_DEVICE"
    return "KNOWN_DEVICE"


def session_behaviour(obs: SessionBehaviourObs, t: ClassifierThresholds) -> str:
    if obs.paste_on_account_field or obs.max_field_dwell_s > t.paste_or_dwell_max_field_dwell_s:
        return "HESITANT_OR_PASTED"
    if obs.typing_cadence_z > t.cadence_mismatch_z:
        return "CADENCE_MISMATCH"
    return "NORMAL"


def call_status(obs: CallStatusObs, t: ClassifierThresholds) -> str:
    if obs.on_call and not obs.counterparty_in_contacts:
        return "ON_CALL_UNKNOWN_NUMBER"
    if obs.on_call:
        return "ON_CALL_KNOWN_CONTACT"
    return "NOT_ON_CALL"


def otp_step_up(obs: OtpObs, t: ClassifierThresholds) -> str:
    if obs.correct and obs.attempts <= t.otp_max_attempts and not obs.timed_out:
        return "PASSED"
    return "FAILED_OR_ABANDONED"


def biometric_step_up(obs: BiometricObs, t: ClassifierThresholds) -> str:
    return "PASSED" if obs.result == "MATCH" else "FAILED_OR_ABANDONED"


def purpose_question(obs: AnswerObs, t: ClassifierThresholds) -> str:
    if obs.option == RECEIVING_MONEY_PURPOSE:
        return "BELIEVES_RECEIVING_MONEY"
    if obs.option in HIGH_RISK_PURPOSES:
        return "HIGH_RISK_PURPOSE"
    if (
        obs.latency_s < t.purpose_fast_s
        or obs.latency_s > t.purpose_slow_s
        or obs.app_switch_count >= 1
    ):
        return "TIMING_ANOMALOUS"
    return "PLAUSIBLE_NORMAL_TIMING"


def spell_breaker(obs: AnswerObs, t: ClassifierThresholds) -> str:
    if obs.option == YES_OPTION:
        return "ANSWERED_YES"
    if obs.latency_s > t.spell_breaker_hesitation_s or obs.app_switch_count >= 1:
        return "HESITATION_OR_APP_SWITCH"
    return "ANSWERED_NO_NORMAL"


def cooling_off(obs: CoolingOffObs, t: ClassifierThresholds) -> str:
    if obs.cancelled:
        return "CANCELLED_BY_CUSTOMER"
    if (
        obs.other_channel_attempts + obs.limit_increase_requests >= 1
        or obs.send_now_presses >= t.cooling_off_send_now_presses
    ):
        return "PERSISTED_OR_ESCALATED"
    return "WAITED_NORMALLY"


def collect_intent_check(obs: AnswerObs, t: ClassifierThresholds) -> str:
    if obs.option == COLLECT_RECEIVING_OPTION:
        return "EXPECTS_TO_RECEIVE"
    return "UNDERSTANDS_PAYING"


# ---------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class Classifier:
    """A registered classifier and every outcome key it can return."""

    classify: Callable[[Any, ClassifierThresholds], str]
    outcomes: frozenset[str]


GUARDIAN_CLASSIFIERS: Mapping[str, Classifier] = {
    "BENEFICIARY_INTEL": Classifier(
        beneficiary_intel,
        frozenset({"LISTED_SUSPECT", "HIGH_MULE_SCORE", "CLEAN_KNOWN_ENTITY", "CLEAN_UNKNOWN"}),
    ),
    "COUNTERPARTY_PHONE_RISK": Classifier(
        counterparty_phone_risk, frozenset({"VERY_HIGH", "MEDIUM_HIGH", "LOW_OR_UNKNOWN"})
    ),
    "DEVICE_POSTURE": Classifier(
        device_posture, frozenset({"REMOTE_ACCESS_ACTIVE", "NEW_DEVICE", "KNOWN_DEVICE"})
    ),
    "SESSION_BEHAVIOUR": Classifier(
        session_behaviour, frozenset({"HESITANT_OR_PASTED", "CADENCE_MISMATCH", "NORMAL"})
    ),
    "CALL_STATUS": Classifier(
        call_status,
        frozenset({"ON_CALL_UNKNOWN_NUMBER", "ON_CALL_KNOWN_CONTACT", "NOT_ON_CALL"}),
    ),
    "OTP_STEP_UP": Classifier(otp_step_up, frozenset({"PASSED", "FAILED_OR_ABANDONED"})),
    "BIOMETRIC_STEP_UP": Classifier(
        biometric_step_up, frozenset({"PASSED", "FAILED_OR_ABANDONED"})
    ),
    "PURPOSE_QUESTION": Classifier(
        purpose_question,
        frozenset(
            {
                "PLAUSIBLE_NORMAL_TIMING",
                "HIGH_RISK_PURPOSE",
                "TIMING_ANOMALOUS",
                "BELIEVES_RECEIVING_MONEY",
            }
        ),
    ),
    "SPELL_BREAKER_DIGITAL_ARREST": Classifier(
        spell_breaker,
        frozenset({"HESITATION_OR_APP_SWITCH", "ANSWERED_YES", "ANSWERED_NO_NORMAL"}),
    ),
    "SPELL_BREAKER_INVESTMENT": Classifier(
        spell_breaker,
        frozenset({"HESITATION_OR_APP_SWITCH", "ANSWERED_YES", "ANSWERED_NO_NORMAL"}),
    ),
    "COOLING_OFF": Classifier(
        cooling_off,
        frozenset({"PERSISTED_OR_ESCALATED", "CANCELLED_BY_CUSTOMER", "WAITED_NORMALLY"}),
    ),
    "COLLECT_INTENT_CHECK": Classifier(
        collect_intent_check, frozenset({"EXPECTS_TO_RECEIVE", "UNDERSTANDS_PAYING"})
    ),
}
