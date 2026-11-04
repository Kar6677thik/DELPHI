"""Label arrival: selective labels and label noise (docs/14 §7, FR-SIM-04)."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from delphi.sim.labels import (
    FRAUD_OTHER,
    GENUINE,
    LabelConfig,
    LabelSource,
    PaymentDecision,
    PaymentOutcome,
    Resolution,
    ScheduledLabel,
    SubjectType,
    load_label_config,
    observed_hypothesis,
    schedule_payment_labels,
    schedule_registry_label,
)
from delphi.sim.scenarios import ScenarioType

T0 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
DEMO_MATURITY = 60
TEST_MATURITY = 7
KB_V1 = frozenset({"GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE"})
N = 20_000


@pytest.fixture(scope="module")
def config() -> LabelConfig:
    return load_label_config()


def _rng(seed: int = 1) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(seed))


def _payment(
    scenario: ScenarioType,
    decision: PaymentDecision = PaymentDecision.RELEASED,
    resolution: Resolution | None = None,
) -> PaymentOutcome:
    return PaymentOutcome("pay-1", scenario, decision, T0, resolution)


def _many(
    scenario: ScenarioType, config: LabelConfig, maturity: int = DEMO_MATURITY
) -> list[list[ScheduledLabel]]:
    rng = _rng(42)
    return [
        schedule_payment_labels(_payment(scenario), rng, config, maturity_days=maturity)
        for _ in range(N)
    ]


def test_config_matches_the_spec(config: LabelConfig) -> None:
    assert config.report_probability == {
        ScenarioType.APP_DIGITAL_ARREST: 0.70,
        ScenarioType.APP_INVESTMENT: 0.70,
        ScenarioType.ATO: 0.90,
        ScenarioType.UPI_COLLECT_REFUND: 0.60,
    }
    assert config.label_delay_sigma == 0.8
    assert config.complaint_median_hours(ScenarioType.APP_INVESTMENT) == 48
    assert config.complaint_median_hours(ScenarioType.ATO) == 4
    assert config.registry_delay_median_hours == 120


# --------------------------------------------------------------------- selective labels


@pytest.mark.parametrize("decision", [PaymentDecision.HELD, PaymentDecision.DECLINED])
@pytest.mark.parametrize("scenario", list(ScenarioType))
def test_b1_held_or_declined_items_stay_unlabelled(
    config: LabelConfig, decision: PaymentDecision, scenario: ScenarioType
) -> None:
    """The legacy policy's holds are never investigated, so nothing labels them (§7)."""
    labels = schedule_payment_labels(
        _payment(scenario, decision), _rng(), config, maturity_days=DEMO_MATURITY
    )
    assert labels == []


def test_a_held_payment_resolved_by_outreach_gets_that_label(config: LabelConfig) -> None:
    at = T0 + timedelta(minutes=25)
    resolution = Resolution(LabelSource.OUTREACH_CONFIRMED, "APP_DIGITAL_ARREST", at)
    labels = schedule_payment_labels(
        _payment(ScenarioType.APP_DIGITAL_ARREST, PaymentDecision.HELD, resolution),
        _rng(),
        config,
        maturity_days=DEMO_MATURITY,
    )
    assert labels == [
        ScheduledLabel(
            SubjectType.PAYMENT, "pay-1", "APP_DIGITAL_ARREST", LabelSource.OUTREACH_CONFIRMED, at
        )
    ]


def test_an_investigator_disposition_labels_a_declined_payment(config: LabelConfig) -> None:
    at = T0 + timedelta(hours=3)
    resolution = Resolution(LabelSource.INVESTIGATOR_DISPOSITION, GENUINE, at)
    (label,) = schedule_payment_labels(
        _payment(ScenarioType.GENUINE_UNUSUAL, PaymentDecision.DECLINED, resolution),
        _rng(),
        config,
        maturity_days=DEMO_MATURITY,
    )
    assert (label.hypothesis_key, label.label_source, label.due_at) == (
        GENUINE,
        LabelSource.INVESTIGATOR_DISPOSITION,
        at,
    )


def test_resolutions_are_validated(config: LabelConfig) -> None:
    complaint = Resolution(LabelSource.CUSTOMER_COMPLAINT, "ATO", T0)
    with pytest.raises(ValueError, match="outreach or an investigator"):
        schedule_payment_labels(
            _payment(ScenarioType.ATO, PaymentDecision.HELD, complaint),
            _rng(),
            config,
            maturity_days=DEMO_MATURITY,
        )
    outreach = Resolution(LabelSource.OUTREACH_CONFIRMED, "ATO", T0)
    with pytest.raises(ValueError, match="no hold"):
        schedule_payment_labels(
            _payment(ScenarioType.ATO, PaymentDecision.RELEASED, outreach),
            _rng(),
            config,
            maturity_days=DEMO_MATURITY,
        )


# --------------------------------------------------------------------- released payments


@pytest.mark.parametrize("scenario", [ScenarioType.GENUINE_ROUTINE, ScenarioType.GENUINE_UNUSUAL])
@pytest.mark.parametrize("maturity", [DEMO_MATURITY, TEST_MATURITY])
def test_released_genuine_matures_as_genuine(
    config: LabelConfig, scenario: ScenarioType, maturity: int
) -> None:
    labels = schedule_payment_labels(_payment(scenario), _rng(), config, maturity_days=maturity)
    assert labels == [
        ScheduledLabel(
            SubjectType.PAYMENT,
            "pay-1",
            GENUINE,
            LabelSource.NO_COMPLAINT_60D,
            T0 + timedelta(days=maturity),
        )
    ]


@pytest.mark.parametrize("maturity", [DEMO_MATURITY, TEST_MATURITY])
def test_unreported_fraud_is_labelled_genuine_at_maturity(
    config: LabelConfig, maturity: int
) -> None:
    """Label noise by design (§7): a fraud nobody reports looks genuine to the bank."""
    never_reported = config.model_copy(update={"report_probability": {}})
    for scenario in (
        ScenarioType.ATO,
        ScenarioType.APP_INVESTMENT,
        ScenarioType.UPI_COLLECT_REFUND,
    ):
        labels = schedule_payment_labels(
            _payment(scenario), _rng(), never_reported, maturity_days=maturity
        )
        assert [(x.hypothesis_key, x.label_source, x.due_at) for x in labels] == [
            (GENUINE, LabelSource.NO_COMPLAINT_60D, T0 + timedelta(days=maturity))
        ]


def test_mule_forwarding_is_never_reported_by_a_victim(config: LabelConfig) -> None:
    sources = {
        x.label_source for labels in _many(ScenarioType.MULE_FORWARDING, config) for x in labels
    }
    assert sources == {LabelSource.NO_COMPLAINT_60D}


@pytest.mark.parametrize(
    ("scenario", "rate"),
    [
        (ScenarioType.APP_DIGITAL_ARREST, 0.70),
        (ScenarioType.APP_INVESTMENT, 0.70),
        (ScenarioType.ATO, 0.90),
        (ScenarioType.UPI_COLLECT_REFUND, 0.60),
    ],
)
def test_report_rates_and_noise_rates(
    config: LabelConfig, scenario: ScenarioType, rate: float
) -> None:
    runs = _many(scenario, config)
    reported = sum(any(x.label_source is LabelSource.CUSTOMER_COMPLAINT for x in r) for r in runs)
    noisy = sum(
        all(x.hypothesis_key == GENUINE for x in r) for r in runs
    )  # only the wrong GENUINE label
    assert reported / N == pytest.approx(rate, abs=0.012)
    assert noisy / N == pytest.approx(1 - rate, abs=0.012)


@pytest.mark.parametrize(
    ("scenario", "median"),
    [(ScenarioType.APP_DIGITAL_ARREST, timedelta(days=2)), (ScenarioType.ATO, timedelta(hours=4))],
)
def test_complaint_delays_have_the_specified_median(
    config: LabelConfig, scenario: ScenarioType, median: timedelta
) -> None:
    delays = [
        (x.due_at - T0).total_seconds()
        for r in _many(scenario, config)
        for x in r
        if x.label_source is LabelSource.CUSTOMER_COMPLAINT
    ]
    assert min(delays) > 0
    assert statistics.median(delays) == pytest.approx(median.total_seconds(), rel=0.05)


def test_a_complaint_after_maturity_follows_the_genuine_label(config: LabelConfig) -> None:
    """On `test` (7-day maturity) some complaints come late: both labels arrive, in order."""
    runs = _many(ScenarioType.APP_INVESTMENT, config, maturity=TEST_MATURITY)
    late = [r for r in runs if len(r) == 2]
    assert late
    for first, second in late:
        assert (first.hypothesis_key, first.label_source) == (GENUINE, LabelSource.NO_COMPLAINT_60D)
        assert second.label_source is LabelSource.CUSTOMER_COMPLAINT
        assert first.due_at < second.due_at
    on_time = [
        r for r in runs if len(r) == 1 and r[0].label_source is LabelSource.CUSTOMER_COMPLAINT
    ]
    assert all(r[0].due_at <= T0 + timedelta(days=TEST_MATURITY) for r in on_time)


def test_schedule_is_deterministic_for_a_seed(config: LabelConfig) -> None:
    def run() -> list[ScheduledLabel]:
        rng = _rng(7)
        return [
            label
            for scenario in ScenarioType
            for label in schedule_payment_labels(
                _payment(scenario), rng, config, maturity_days=DEMO_MATURITY
            )
        ]

    assert run() == run()


# --------------------------------------------------------------------- registry and KB keys


def test_registry_listing_labels_the_account_mule(config: LabelConfig) -> None:
    rng = _rng(3)
    labels = [schedule_registry_label("acc-9", T0, rng, config) for _ in range(N)]
    assert {(x.subject_type, x.subject_id, x.hypothesis_key, x.label_source) for x in labels} == {
        (SubjectType.ACCOUNT, "acc-9", "MULE", LabelSource.REGISTRY_LISTING)
    }
    median = statistics.median((x.due_at - T0).total_seconds() for x in labels)
    assert median == pytest.approx(timedelta(days=5).total_seconds(), rel=0.05)


def test_complaints_outside_the_active_kb_are_fraud_other() -> None:
    def complaint(key: str) -> ScheduledLabel:
        return ScheduledLabel(SubjectType.PAYMENT, "p", key, LabelSource.CUSTOMER_COMPLAINT, T0)

    assert observed_hypothesis(complaint("ATO"), KB_V1) == "ATO"
    matured = ScheduledLabel(SubjectType.PAYMENT, "p", GENUINE, LabelSource.NO_COMPLAINT_60D, T0)
    assert observed_hypothesis(matured, KB_V1) == GENUINE


def test_collect_refund_complaints_name_the_promoted_key_only_after_promotion(
    config: LabelConfig,
) -> None:
    """The scenario's ground truth is APP_COLLECT_REFUND (docs/11 §7, docs/14 §4): FRAUD_OTHER
    under KB v1, the new typology once the Scout has promoted it."""
    rng = _rng(5)
    complaints = [
        label
        for _ in range(200)
        for label in schedule_payment_labels(
            _payment(ScenarioType.UPI_COLLECT_REFUND), rng, config, maturity_days=DEMO_MATURITY
        )
        if label.label_source is LabelSource.CUSTOMER_COMPLAINT
    ]
    assert complaints
    assert {c.hypothesis_key for c in complaints} == {"APP_COLLECT_REFUND"}
    promoted = KB_V1 | {"APP_COLLECT_REFUND"}
    for c in complaints:
        assert observed_hypothesis(c, KB_V1) == FRAUD_OTHER
        assert observed_hypothesis(c, promoted) == "APP_COLLECT_REFUND"
