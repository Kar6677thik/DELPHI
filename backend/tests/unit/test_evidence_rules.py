"""The Navigator evidence outcome rules (docs/09 §4, FR-NAV-05): every rule at and around each
boundary, with KB v1's thresholds (kb/engine.yaml `classifiers:`)."""

from __future__ import annotations

import pytest

from delphi.kb.loader import load_and_validate
from delphi.kb.model import ClassifierThresholds
from delphi.probes.evidence import (
    EVIDENCE_CLASSIFIERS,
    RECORDED_EVIDENCE,
    AccountProfileObs,
    ComplaintsObs,
    CrmObs,
    DeviceSharingObs,
    DwellObs,
    FanInObs,
    GeoVelocityObs,
    PayeeRelationshipObs,
    account_profile,
    crm_contact_history,
    device_sharing,
    inbound_fan_in,
    linked_complaints,
    login_geo_velocity,
    pass_through_dwell,
    payee_relationship,
)
from delphi.probes.registry import CLASSIFIER_NAMES, NAVIGATOR_EVIDENCE_NAMES
from tests.conftest import KB_DIR

KB = load_and_validate(KB_DIR, CLASSIFIER_NAMES)
T: ClassifierThresholds = KB.config.classifiers


def test_kb_v1_thresholds_are_the_spec_ones() -> None:
    """docs/09 §4's numbers, read from the KB rather than written into the rules."""
    assert (T.fan_in_high, T.fan_in_moderate) == (10, 3)
    assert (T.dwell_fast_min, T.dwell_slow_h, T.new_account_days) == (30, 24, 90)
    assert (T.crm_lookback_days, T.impossible_travel_kmh, T.small_payment_inr) == (14, 500, 5000)


def test_every_rule_returns_exactly_its_kb_sources_outcomes() -> None:
    sources = {s.key: s for s in KB.sources.values() if s.kind == "EVIDENCE"}
    assert set(sources) == NAVIGATOR_EVIDENCE_NAMES
    for key, classifier in EVIDENCE_CLASSIFIERS.items():
        assert classifier.outcomes == frozenset(sources[key].outcomes), key
    for key, outcomes in RECORDED_EVIDENCE.items():
        assert outcomes == frozenset(sources[key].outcomes), key


@pytest.mark.parametrize(
    ("n", "outcome"),
    [
        (0, "NOT_SHARED"),
        (1, "SHARED_1_2"),
        (2, "SHARED_1_2"),
        (3, "SHARED_3_PLUS"),
        (9, "SHARED_3_PLUS"),
    ],
)
def test_device_sharing(n: int, outcome: str) -> None:
    assert device_sharing(DeviceSharingObs(n), T) == outcome


@pytest.mark.parametrize(
    ("m", "outcome"), [(0, "LOW"), (2, "LOW"), (3, "MODERATE"), (9, "MODERATE"), (10, "HIGH")]
)
def test_inbound_fan_in(m: int, outcome: str) -> None:
    assert inbound_fan_in(FanInObs(m), T) == outcome


@pytest.mark.parametrize(
    ("minutes", "outcome"),
    [
        ([], "OVER_24_H"),  # no credits
        ([29.9], "UNDER_30_MIN"),
        ([30.0], "BETWEEN_30_MIN_24_H"),  # "< 30 min" is strict
        ([1440.0], "BETWEEN_30_MIN_24_H"),  # "≤ 24 h" includes it
        ([1440.1], "OVER_24_H"),
        ([5, 10, 2000], "UNDER_30_MIN"),  # the median, not the mean
        ([5, 50], "UNDER_30_MIN"),  # even count: the middle two averaged, 27.5
        ([20, 50], "BETWEEN_30_MIN_24_H"),  # 35
        ([10, 3000, 4000], "OVER_24_H"),  # two credits still in the account days later
        ([10, 20, 4000], "UNDER_30_MIN"),
    ],
)
def test_pass_through_dwell(minutes: list[float], outcome: str) -> None:
    assert pass_through_dwell(DwellObs(minutes), T) == outcome


@pytest.mark.parametrize(
    ("age", "kyc", "outcome"),
    [
        (89.9, "MINIMAL", "NEW_MINIMAL_KYC"),
        (89.9, "FULL", "NEW_FULL_KYC"),
        (90.0, "MINIMAL", "ESTABLISHED"),  # "age < 90 days" is strict
        (2000, "FULL", "ESTABLISHED"),
    ],
)
def test_account_profile(age: float, kyc: str, outcome: str) -> None:
    assert account_profile(AccountProfileObs(age, kyc), T) == outcome


@pytest.mark.parametrize(
    ("count", "outcome"), [(0, "NONE"), (1, "ONE_OR_MORE"), (4, "ONE_OR_MORE")]
)
def test_linked_complaints(count: int, outcome: str) -> None:
    assert linked_complaints(ComplaintsObs(count), T) == outcome


@pytest.mark.parametrize(
    ("events", "outcome"),
    [
        ((), "NONE"),
        (("SIM_SWAP_REPORTED",), "SIM_SWAP_OR_DEVICE_LOSS_REPORTED"),
        (("ADDRESS_CHANGED", "DEVICE_LOST_REPORTED"), "SIM_SWAP_OR_DEVICE_LOSS_REPORTED"),
        (("ADDRESS_CHANGED",), "NONE"),
    ],
)
def test_crm_contact_history(events: tuple[str, ...], outcome: str) -> None:
    assert crm_contact_history(CrmObs(events), T) == outcome


@pytest.mark.parametrize(
    ("speed", "new_city", "outcome"),
    [
        (None, False, "USUAL"),
        (500.0, True, "NEW_LOCATION"),  # "> 500 km/h" is strict
        (500.1, False, "IMPOSSIBLE_TRAVEL"),
        (900.0, True, "IMPOSSIBLE_TRAVEL"),  # impossible travel wins over a new location
        (40.0, False, "USUAL"),
    ],
)
def test_login_geo_velocity(speed: float | None, new_city: bool, outcome: str) -> None:
    assert login_geo_velocity(GeoVelocityObs(speed, new_city), T) == outcome


@pytest.mark.parametrize(
    ("amounts", "outcome"),
    [
        ((), "NO_PRIOR_RELATION"),
        ((499_999,), "PRIOR_SMALL_PAYMENTS"),  # ₹4,999.99
        ((100_00, 499_999), "PRIOR_SMALL_PAYMENTS"),
        ((500_000,), "ESTABLISHED_PAYEE"),  # ₹5,000 is not "< ₹5,000"
        ((100_00, 2_000_000), "ESTABLISHED_PAYEE"),
    ],
)
def test_payee_relationship(amounts: tuple[int, ...], outcome: str) -> None:
    assert payee_relationship(PayeeRelationshipObs(amounts), T) == outcome
