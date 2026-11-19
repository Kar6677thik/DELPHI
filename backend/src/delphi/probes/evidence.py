"""Deterministic outcome rules for the Navigator evidence sources (docs/09 §4, FR-NAV-05).

Each rule maps one evidence source's facts (what its adapter counted over the bank, #102) to one
of the source's outcome keys in ``kb/sources/evidence``. Thresholds come from ``kb/engine.yaml``
(``classifiers:``); the look-back windows are the rules' own (docs/09 §4) and are named here.
Windows are relative to the case's ``as_of`` time, which the adapters apply.

Rules are pure: no I/O, no clock and no randomness. ``CUSTOMER_OUTREACH_CALL`` has no rule: its
outcome is the investigator's recorded result, or a response-model sample in simulation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from delphi.kb.model import ClassifierThresholds
from delphi.probes.classifiers import Classifier

# docs/09 §4: the look-back of each rule, from the case's as_of.
DEVICE_SHARING_WINDOW = timedelta(days=30)
FAN_IN_WINDOW = timedelta(hours=48)
DWELL_WINDOW = timedelta(days=7)
GEO_VELOCITY_WINDOW = timedelta(hours=24)
DWELL_OUT_SHARE = 0.8  # "until 80% of that credit's amount has left the account"
DEVICE_SHARING_MANY = 3  # "n ≥ 3 → SHARED_3_PLUS; 1–2 → SHARED_1_2"
_PAISE_PER_INR = 100
_MINUTES_PER_HOUR = 60

# ---------------------------------------------------------------- observations


@dataclass(frozen=True, slots=True)
class DeviceSharingObs:
    """Other accounts whose owners logged in from a device the subject's owner used (30 days)."""

    other_accounts: int


@dataclass(frozen=True, slots=True)
class FanInObs:
    """Distinct payers into the subject account in the last 48 h with no prior relation to it."""

    unrelated_payers: int


@dataclass(frozen=True, slots=True)
class DwellObs:
    """Minutes until 80% of each credit in the last 7 days had left the account (FIFO). A credit
    not yet 80% gone by ``as_of`` counts at the minutes it has dwelt so far (a lower bound), so
    the rule and the narrative (docs/15 §7 `median_dwell_minutes`) use the same number."""

    minutes_to_out: Sequence[float]


@dataclass(frozen=True, slots=True)
class AccountProfileObs:
    age_days: float
    kyc_level: Literal["FULL", "MINIMAL"] | str


@dataclass(frozen=True, slots=True)
class ComplaintsObs:
    """Complaints naming the subject account, known as of now."""

    count: int


@dataclass(frozen=True, slots=True)
class CrmObs:
    """The payer's CRM events in the KB's look-back (``crm_lookback_days``)."""

    event_types: Sequence[str]


@dataclass(frozen=True, slots=True)
class GeoVelocityObs:
    """The fastest travel implied between two of the payer's logins in the last 24 h (km/h,
    ``None`` with fewer than two), and whether the last login's city was never seen before."""

    max_speed_kmh: float | None
    last_city_new: bool


@dataclass(frozen=True, slots=True)
class PayeeRelationshipObs:
    """Earlier executed payments from the payer to the payee, in paise."""

    prior_amounts_paise: Sequence[int]


# ---------------------------------------------------------------- rules (docs/09 §4 order)


def device_sharing(obs: DeviceSharingObs, t: ClassifierThresholds) -> str:
    if obs.other_accounts >= DEVICE_SHARING_MANY:
        return "SHARED_3_PLUS"
    if obs.other_accounts >= 1:
        return "SHARED_1_2"
    return "NOT_SHARED"


def inbound_fan_in(obs: FanInObs, t: ClassifierThresholds) -> str:
    if obs.unrelated_payers >= t.fan_in_high:
        return "HIGH"
    if obs.unrelated_payers >= t.fan_in_moderate:
        return "MODERATE"
    return "LOW"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def median_dwell(obs: DwellObs) -> float | None:
    """The median dwell in minutes, ``None`` with no credits."""
    return _median(obs.minutes_to_out) if obs.minutes_to_out else None


def pass_through_dwell(obs: DwellObs, t: ClassifierThresholds) -> str:
    median = median_dwell(obs)
    if median is None:
        return "OVER_24_H"  # docs/09 §4: "also OVER_24_H if there are no credits"
    if median < t.dwell_fast_min:
        return "UNDER_30_MIN"
    if median <= t.dwell_slow_h * _MINUTES_PER_HOUR:
        return "BETWEEN_30_MIN_24_H"
    return "OVER_24_H"


def account_profile(obs: AccountProfileObs, t: ClassifierThresholds) -> str:
    if obs.age_days < t.new_account_days:
        return "NEW_MINIMAL_KYC" if obs.kyc_level == "MINIMAL" else "NEW_FULL_KYC"
    return "ESTABLISHED"


def linked_complaints(obs: ComplaintsObs, t: ClassifierThresholds) -> str:
    return "ONE_OR_MORE" if obs.count >= 1 else "NONE"


CRM_REPORTS = frozenset({"SIM_SWAP_REPORTED", "DEVICE_LOST_REPORTED"})


def crm_contact_history(obs: CrmObs, t: ClassifierThresholds) -> str:
    if CRM_REPORTS.intersection(obs.event_types):
        return "SIM_SWAP_OR_DEVICE_LOSS_REPORTED"
    return "NONE"


def login_geo_velocity(obs: GeoVelocityObs, t: ClassifierThresholds) -> str:
    if obs.max_speed_kmh is not None and obs.max_speed_kmh > t.impossible_travel_kmh:
        return "IMPOSSIBLE_TRAVEL"
    if obs.last_city_new:
        return "NEW_LOCATION"
    return "USUAL"


def payee_relationship(obs: PayeeRelationshipObs, t: ClassifierThresholds) -> str:
    if not obs.prior_amounts_paise:
        return "NO_PRIOR_RELATION"
    small = t.small_payment_inr * _PAISE_PER_INR
    if all(a < small for a in obs.prior_amounts_paise):
        return "PRIOR_SMALL_PAYMENTS"
    return "ESTABLISHED_PAYEE"


# ---------------------------------------------------------------- registry

EVIDENCE_CLASSIFIERS: Mapping[str, Classifier] = {
    "BENEFICIARY_DEVICE_SHARING": Classifier(
        device_sharing, frozenset({"SHARED_3_PLUS", "SHARED_1_2", "NOT_SHARED"})
    ),
    "INBOUND_FAN_IN": Classifier(inbound_fan_in, frozenset({"HIGH", "MODERATE", "LOW"})),
    "PASS_THROUGH_DWELL": Classifier(
        pass_through_dwell, frozenset({"UNDER_30_MIN", "BETWEEN_30_MIN_24_H", "OVER_24_H"})
    ),
    "ACCOUNT_PROFILE": Classifier(
        account_profile, frozenset({"NEW_MINIMAL_KYC", "NEW_FULL_KYC", "ESTABLISHED"})
    ),
    "LINKED_COMPLAINTS": Classifier(linked_complaints, frozenset({"ONE_OR_MORE", "NONE"})),
    "CRM_CONTACT_HISTORY": Classifier(
        crm_contact_history, frozenset({"SIM_SWAP_OR_DEVICE_LOSS_REPORTED", "NONE"})
    ),
    "LOGIN_GEO_VELOCITY": Classifier(
        login_geo_velocity, frozenset({"IMPOSSIBLE_TRAVEL", "NEW_LOCATION", "USUAL"})
    ),
    "PAYEE_RELATIONSHIP": Classifier(
        payee_relationship,
        frozenset({"NO_PRIOR_RELATION", "PRIOR_SMALL_PAYMENTS", "ESTABLISHED_PAYEE"}),
    ),
}

# The investigator records it (or the simulation samples it): no rule, but a known source.
RECORDED_EVIDENCE: Mapping[str, frozenset[str]] = {
    "CUSTOMER_OUTREACH_CALL": frozenset(
        {"CONFIRMS_COERCION", "CONFIRMS_GENUINE_PURPOSE", "DENIES_INITIATING", "UNREACHABLE"}
    ),
}
