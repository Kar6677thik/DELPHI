"""docs/22 §4 metrics and their customer cluster bootstrap on hand-built micro-streams (#124)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import numpy as np
import pytest

from delphi.evaluation.config import BootstrapConfig, load_evaluation_config
from delphi.evaluation.metrics import evaluate_policy, weighted_quantile
from delphi.evaluation.outcomes import (
    Decision,
    FinalStatus,
    LabelRecord,
    PaymentOutcome,
    ProbeMode,
    ProbeRecord,
)
from delphi.sim.scenarios import ScenarioType

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
KB_V1 = frozenset({"GENUINE", "ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT", "MULE"})
CONFIG = load_evaluation_config().model_copy(
    update={"bootstrap": BootstrapConfig(replicates=200, level=0.95, seed=7)}
)
C1, C2, C3 = UUID(int=1), UUID(int=2), UUID(int=3)


def _payment(
    customer: UUID,
    scenario: ScenarioType,
    amount: int,
    decision: Decision,
    final: FinalStatus,
    **kw: object,
) -> PaymentOutcome:
    return PaymentOutcome(
        payment_id=uuid4(),
        customer_id=customer,
        vulnerable=bool(kw.pop("vulnerable", False)),
        amount_paise=amount,
        scenario=scenario,
        otp_would_pass=bool(kw.pop("otp_would_pass", False)),
        decision=decision,
        decided_at=T0,
        final_status=final,
        **kw,  # type: ignore[arg-type]
    )


def _probe(mode: ProbeMode, friction: int, bits: float | None) -> ProbeRecord:
    return ProbeRecord("CALLER_CHECK", mode, friction, bits)


def _micro_stream() -> tuple[list[PaymentOutcome], list[LabelRecord]]:
    arrest = _payment(
        C1,
        ScenarioType.APP_DIGITAL_ARREST,
        10_000,
        Decision.HOLD_FOR_REVIEW,
        FinalStatus.HELD,
        otp_would_pass=True,
        held_at=T0,
        resolved_at=T0 + timedelta(minutes=30),
        probes=(_probe(ProbeMode.ACTIVE, 500, 0.8), _probe(ProbeMode.PASSIVE, 0, None)),
        argmax_hypothesis="APP_DIGITAL_ARREST",
    )
    routine = _payment(
        C1,
        ScenarioType.GENUINE_ROUTINE,
        2_000,
        Decision.RELEASE,
        FinalStatus.EXECUTED,
        probes=(_probe(ProbeMode.ACTIVE, 100, 0.2),),
    )
    ato = _payment(C2, ScenarioType.ATO, 30_000, Decision.RELEASE, FinalStatus.EXECUTED)
    unusual = _payment(
        C2,
        ScenarioType.GENUINE_UNUSUAL,
        5_000,
        Decision.PROTECTIVE_HOLD,
        FinalStatus.HELD,
        vulnerable=True,
        held_at=T0,  # the run ends with it still held
    )
    refund = _payment(
        C3,
        ScenarioType.UPI_COLLECT_REFUND,
        6_000,
        Decision.DECLINE,
        FinalStatus.DECLINED,
        argmax_hypothesis="ATO",
    )
    labels = [
        LabelRecord(unusual.payment_id, "OUTREACH_CONFIRMED", T0 + timedelta(days=2)),
        LabelRecord(refund.payment_id, "CUSTOMER_COMPLAINT", T0 + timedelta(days=8)),
    ]
    return [arrest, routine, ato, unusual, refund], labels


def test_the_point_values_follow_the_docs22_definitions() -> None:
    outcomes, labels = _micro_stream()
    m = {
        k: e.value
        for k, e in evaluate_policy(outcomes, labels, kb_hypotheses=KB_V1, config=CONFIG).items()
    }
    # F: the arrest (stopped), the ATO (executed), the collect-refund scam (declined).
    assert m["fraud_prevented_paise"] == 16_000
    assert m["fraud_recall_count"] == pytest.approx(2 / 3)
    assert m["fraud_recall_amount"] == pytest.approx(16_000 / 46_000)
    assert m["coercion_catch_rate"] == 1.0  # the arrest, whose OTP would pass
    assert m["genuine_hold_rate"] == 0.5
    assert m["genuine_hold_rate_vulnerable"] == 1.0
    # Held: 30 minutes and one never resolved (+∞).
    assert m["hold_resolution_minutes_median"] == 30.0
    assert m["hold_resolution_minutes_p90"] == math.inf
    assert m["friction_per_fraud_paise_prevented"] == pytest.approx(600 / 16_000)
    assert m["active_probes_per_genuine_payment"] == 0.5
    # Held decisions: the arrest (no label), the unusual payment (2 days), the refund (8 days).
    assert m["label_coverage_held"] == pytest.approx(1 / 3)
    assert m["dd_pack_completeness"] is None  # no DD packs: a baseline
    # The refund scam's APP_COLLECT_REFUND is not a KB v1 hypothesis, so only the arrest counts.
    assert m["typology_attribution_accuracy"] == 1.0
    assert m["info_yield_bits[CALLER_CHECK]"] == pytest.approx(0.5)  # unobserved probes skipped


def test_every_ci_brackets_its_value_and_a_rerun_is_identical() -> None:
    outcomes, labels = _micro_stream()
    first = evaluate_policy(outcomes, labels, kb_hypotheses=KB_V1, config=CONFIG)
    assert first == evaluate_policy(outcomes, labels, kb_hypotheses=KB_V1, config=CONFIG)
    for name, e in first.items():
        if e.value is not None:
            assert e.low is not None and e.high is not None
            assert e.low <= e.value <= e.high, name


def test_the_bootstrap_resamples_customers_not_payments() -> None:
    """A customer's payments move together: with one customer whose frauds are all stopped and
    one whose frauds all execute, a replicate's recall can only be 0, ½ or 1."""
    stopped = [
        _payment(C1, ScenarioType.ATO, 1_000, Decision.DECLINE, FinalStatus.DECLINED)
        for _ in range(20)
    ]
    executed = [
        _payment(C2, ScenarioType.ATO, 1_000, Decision.RELEASE, FinalStatus.EXECUTED)
        for _ in range(20)
    ]
    e = evaluate_policy([*stopped, *executed], [], kb_hypotheses=KB_V1, config=CONFIG)[
        "fraud_recall_count"
    ]
    assert e.value == 0.5
    assert (e.low, e.high) == (0.0, 1.0)


def test_an_empty_denominator_is_undefined_not_zero() -> None:
    routine = _payment(
        C1, ScenarioType.GENUINE_ROUTINE, 1_000, Decision.RELEASE, FinalStatus.EXECUTED
    )
    m = evaluate_policy([routine], [], kb_hypotheses=KB_V1, config=CONFIG)
    for name in (
        "fraud_recall_count",
        "coercion_catch_rate",
        "hold_resolution_minutes_median",
        "friction_per_fraud_paise_prevented",
        "label_coverage_held",
        "typology_attribution_accuracy",
    ):
        assert m[name].value is None and m[name].low is None and m[name].high is None, name
    assert m["genuine_hold_rate"].value == 0.0


def test_dd_completeness_counts_only_the_payments_with_a_pack() -> None:
    def delphi(complete: bool) -> PaymentOutcome:
        return _payment(
            C1,
            ScenarioType.GENUINE_ROUTINE,
            1_000,
            Decision.RELEASE,
            FinalStatus.EXECUTED,
            dd_pack_complete=complete,
        )

    m = evaluate_policy(
        [delphi(True), delphi(True), delphi(False)], [], kb_hypotheses=KB_V1, config=CONFIG
    )
    assert m["dd_pack_completeness"].value == pytest.approx(2 / 3)


def test_uncertainty_reduced_per_investigator_minute_pools_the_evidence() -> None:
    def investigated(minutes: float, bits: float) -> PaymentOutcome:
        return _payment(
            C1,
            ScenarioType.ATO,
            1_000,
            Decision.HOLD_FOR_REVIEW,
            FinalStatus.DECLINED,
            held_at=T0,
            resolved_at=T0,
            evidence_minutes=minutes,
            entropy_reduced_bits=bits,
        )

    m = evaluate_policy(
        [investigated(10.0, 1.0), investigated(2.0, 2.0)], [], kb_hypotheses=KB_V1, config=CONFIG
    )
    assert m["uncertainty_reduced_bits_per_investigator_minute"].value == pytest.approx(3 / 12)


@pytest.mark.parametrize("q", [0.1, 0.5, 0.9, 1.0])
def test_weighted_quantile_matches_numpy_on_repeated_values(q: float) -> None:
    rng = np.random.default_rng(3)
    values = rng.normal(size=30)
    weights = rng.integers(0, 4, size=30).astype(np.float64)
    expected = np.quantile(np.repeat(values, weights.astype(int)), q, method="inverted_cdf")
    assert weighted_quantile(values, weights, q) == expected


def test_the_shipped_config_matches_docs22() -> None:
    config = load_evaluation_config()
    assert config.bootstrap.replicates == 1000 and config.bootstrap.level == 0.95
    assert config.label_coverage_days == 7  # docs/12 §4
