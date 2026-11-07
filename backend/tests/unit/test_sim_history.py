"""The history period: genuine payments decided by B1, ledger, telemetry, truth and labels
(docs/14 §4, §5.1, §7; docs/22 §2; FR-SIM-01; #17)."""

from __future__ import annotations

import time
from collections import Counter

import pytest
import yaml

from delphi.kb.model import ClassifierThresholds, KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.export import canonical_export, canonical_tables, dataset_sha256
from delphi.sim.generator_config import GeneratorConfig, LegacyPolicyConfig, load_generator_config
from delphi.sim.history import IST
from delphi.sim.labels import LabelSource, PaymentDecision
from delphi.sim.legacy_policy import decide_b1
from delphi.sim.response_models import CONFIG_DIR, load_config
from delphi.sim.scenarios import ScenarioType
from delphi.sim.telemetry import SessionTelemetry


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def test_dataset(config: GeneratorConfig, kb: KnowledgeBase) -> Dataset:
    return generate_dataset(config, "test", kb=kb)


def test_the_test_profile_hash_matches_the_record(
    config: GeneratorConfig, kb: KnowledgeBase
) -> None:
    """FR-SIM-01: the canonical export of `test` is byte-stable (docs/14 §1, docs/21 §1)."""
    version = load_config().generator_version
    recorded = yaml.safe_load((CONFIG_DIR / "dataset_hashes.yaml").read_text(encoding="utf-8"))
    started = time.perf_counter()
    actual = dataset_sha256(generate_dataset(config, "test", kb=kb))
    assert time.perf_counter() - started < 30  # docs/14 §2: the test profile in < 30 s
    assert actual == recorded["test"].get(version), (
        f"the `test` dataset changed: bump generator_version, record {actual} for it in "
        "sim/config/dataset_hashes.yaml and describe the change in CHANGELOG.md"
    )


def test_generation_is_deterministic(
    config: GeneratorConfig, kb: KnowledgeBase, test_dataset: Dataset
) -> None:
    assert canonical_export(generate_dataset(config, "test", kb=kb)) == canonical_export(
        test_dataset
    )


def test_payments_stay_inside_the_history(test_dataset: Dataset) -> None:
    pop, history = test_dataset.population, test_dataset.history
    assert history.payments
    assert all(pop.history_start <= p.initiated_at < pop.history_end for p in history.payments)
    assert all(p.origin == "SIM_HISTORY" for p in history.payments)


def test_routine_rate_follows_the_gamma_poisson_model(test_dataset: Dataset) -> None:
    """λ ~ Gamma(2, 0.3) per day: 0.6 payments per customer per day on average; a few are not
    made because the payer cannot fund them."""
    pop, history = test_dataset.population, test_dataset.history
    customer_payments = [p for p in history.payments if p.device_id is not None]
    days = (pop.history_end - pop.history_start).days
    rate = len(customer_payments) / len(pop.customers) / days
    assert 0.45 < rate < 0.65


def test_the_ledger_is_double_entry_and_balances_never_go_negative(test_dataset: Dataset) -> None:
    history = test_dataset.history
    executed = {p.id: p for p in history.payments if p.status == "EXECUTED"}
    entries = Counter(e.payment_id for e in history.ledger)
    assert set(entries) == set(executed)
    assert set(entries.values()) == {2}
    assert sum(e.delta_paise for e in history.ledger) == 0
    assert all(e.balance_after_paise >= 0 for e in history.ledger)
    assert min(history.balances.values()) >= 0
    for e in history.ledger:
        assert abs(e.delta_paise) == executed[e.payment_id].amount_paise


def _hesitant(t: SessionTelemetry, th: ClassifierThresholds) -> bool:
    return t.paste_on_account_field or t.max_field_dwell_s > th.paste_or_dwell_max_field_dwell_s


def test_telemetry_reproduces_the_routine_targets_through_the_classifiers(
    config: GeneratorConfig, kb: KnowledgeBase
) -> None:
    """docs/14 §5.1: GENUINE_ROUTINE outcome rates, read back with the docs/08 §5 rules."""
    th = kb.config.classifiers
    targets = load_config().telemetry_targets[ScenarioType.GENUINE_ROUTINE]
    telemetry = generate_dataset(config, "test", kb=kb).history.telemetry
    n = len(telemetry)
    hesitant = sum(_hesitant(t, th) for t in telemetry) / n
    cadence = sum(t.typing_cadence_z > th.cadence_mismatch_z for t in telemetry) / n
    remote = sum(t.remote_access_active or t.screen_share_active for t in telemetry) / n
    unknown_call = sum(t.on_call and t.call_counterparty_known is False for t in telemetry) / n
    assert hesitant == pytest.approx(targets.paste_or_long_dwell, abs=0.015)
    assert cadence == pytest.approx(targets.cadence_mismatch, abs=0.01)
    assert remote == pytest.approx(targets.remote_access_active, abs=0.005)
    assert unknown_call == pytest.approx(targets.on_call_unknown_number, abs=0.015)
    assert all(t.session_duration_s > 0 and t.max_field_dwell_s >= 0 for t in telemetry)


def test_truth_and_labels(test_dataset: Dataset) -> None:
    history = test_dataset.history
    assert {t.payment_id for t in history.payment_truth} == {p.id for p in history.payments}
    assert not any(t.is_fraud for t in history.payment_truth)
    instances = {i.id: i for i in history.scenario_instances}
    assert all(
        instances[t.scenario_instance_id].scenario_type == "GENUINE_ROUTINE"
        for t in history.payment_truth
    )
    # Released genuine customer payments mature as GENUINE after 7 days on `test` (docs/14 §7).
    customer_payments = {str(p.id): p for p in history.payments if p.device_id is not None}
    assert {label.subject_id for label in history.labels} == set(customer_payments)
    for label in history.labels:
        assert label.label_source is LabelSource.NO_COMPLAINT_60D
        assert (label.due_at - customer_payments[label.subject_id].initiated_at).days == 7


def test_salaries_arrive_monthly(config: GeneratorConfig, kb: KnowledgeBase) -> None:
    """The `test` history (21 days in June) has no 1st of the month; a longer one does."""
    pop_end = config.fixed_history_end
    longer = config.model_copy(
        update={
            "profiles": {
                **config.profiles,
                "test": config.profiles["test"].model_copy(update={"history_days": 45}),
            }
        }
    )
    history = generate_dataset(longer, "test", kb=kb).history
    salaries = [p for p in history.payments if p.device_id is None]
    assert salaries
    assert {p.initiated_at.astimezone(IST).day for p in salaries} == {1}
    assert all(p.channel == "NEFT" and p.status == "EXECUTED" for p in salaries)
    assert all(p.initiated_at < pop_end for p in salaries)


@pytest.mark.parametrize(
    ("kwargs", "decision"),
    [
        ({"payee_listed": True}, PaymentDecision.DECLINED),
        ({"payee_is_new": True, "amount_paise": 5_000_000}, PaymentDecision.HELD),
        ({"payee_is_new": True, "amount_paise": 4_999_999}, PaymentDecision.RELEASED),
        ({"device_is_new": True, "amount_paise": 1_000_000}, PaymentDecision.HELD),
        ({"device_is_new": True, "amount_paise": 999_999}, PaymentDecision.RELEASED),
        ({"channel": "UPI_COLLECT_APPROVAL", "amount_paise": 2_500_000}, PaymentDecision.HELD),
        ({"channel": "UPI_COLLECT_APPROVAL", "amount_paise": 2_499_999}, PaymentDecision.RELEASED),
        ({"amount_paise": 50_000_000}, PaymentDecision.RELEASED),
    ],
)
def test_b1_follows_docs_22_section_2(kwargs: dict[str, object], decision: PaymentDecision) -> None:
    b1 = LegacyPolicyConfig(
        hold_new_payee_min_inr=50_000,
        hold_new_device_min_inr=10_000,
        hold_collect_approval_min_inr=25_000,
    )
    args: dict[str, object] = {
        "amount_paise": 100_000,
        "channel": "UPI_PUSH",
        "payee_listed": False,
        "payee_is_new": False,
        "device_is_new": False,
        **kwargs,
    }
    assert decide_b1(b1, **args) is decision  # type: ignore[arg-type]


def test_b1_config_matches_the_spec(config: GeneratorConfig) -> None:
    b1 = config.legacy_policy
    assert (
        b1.hold_new_payee_min_inr,
        b1.hold_new_device_min_inr,
        b1.hold_collect_approval_min_inr,
    ) == (50_000, 10_000, 25_000)


def test_exported_floats_are_platform_independent(test_dataset: Dataset) -> None:
    """FR-SIM-01: floats keep 6 decimals, so C-library last-bit differences never reach the
    export (docs/14 §3)."""
    floats = [
        value
        for rows in canonical_tables(test_dataset).values()
        for row in rows
        for value in row.values()
        if isinstance(value, float)
    ]
    assert floats
    assert all(value == round(value, 6) for value in floats)
    assert all(
        t.session_duration_s == round(t.session_duration_s, 6)
        for t in test_dataset.history.telemetry
    )
