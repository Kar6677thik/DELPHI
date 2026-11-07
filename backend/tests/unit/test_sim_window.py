"""The live/eval window and the payments due after the history (docs/14 §4, §8; docs/22 §1)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from delphi.cli import main
from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.history import IST
from delphi.sim.scenarios import ScenarioType
from delphi.sim.stats import dataset_stats, format_report


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def long_window(config: GeneratorConfig, kb: KnowledgeBase) -> Dataset:
    """The `test` population with a 30-day window: enough sessions for every scenario."""
    test = config.profiles["test"].model_copy(update={"window_days": 30})
    return generate_dataset(
        config.model_copy(update={"profiles": {**config.profiles, "test": test}}), "test", kb=kb
    )


def _kinds(dataset: Dataset) -> dict:
    history = dataset.history
    kinds = {i.id: ScenarioType(i.scenario_type) for i in history.scenario_instances}
    assert history.window is not None
    return {t.payment_id: kinds[t.scenario_instance_id] for t in history.window.payment_truth}


def test_the_window_is_undecided(long_window: Dataset) -> None:
    """docs/22 §1: each policy decides the window on its own copy of the bank."""
    pop, history = long_window.population, long_window.history
    window = history.window
    assert window is not None and window.payments
    end = pop.history_end
    assert all(p.status == "PENDING_GUARDIAN" and p.origin == "SIM_LIVE" for p in window.payments)
    assert all(end <= p.initiated_at < end + timedelta(days=30) for p in window.payments)
    assert all(p.executed_at is None and p.device_id is not None for p in window.payments)
    ids = {p.id for p in window.payments}
    assert not ids & {p.id for p in history.payments}
    assert not ids & {e.payment_id for e in history.ledger}  # no ledger
    assert not {str(i) for i in ids} & {label.subject_id for label in history.labels}
    assert {t.payment_id for t in window.payment_truth} == ids
    assert {t.payment_id for t in window.telemetry} == ids
    assert len(window.logins) == len(ids)
    assert ScenarioType.MULE_FORWARDING not in set(_kinds(long_window).values())


def test_collect_refund_runs_in_the_window_only(
    long_window: Dataset, config: GeneratorConfig
) -> None:
    history = long_window.history
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    assert not any(
        kinds[t.scenario_instance_id] == "UPI_COLLECT_REFUND" for t in history.payment_truth
    )
    assert history.window is not None
    refunds = [
        p
        for p in history.window.payments
        if _kinds(long_window)[p.id] is ScenarioType.UPI_COLLECT_REFUND
    ]
    assert refunds
    mules = {a for ring in long_window.network.rings for a in ring.layers[0]}
    for p in refunds:
        assert p.channel == "UPI_COLLECT_APPROVAL"
        assert 0 < p.amount_paise <= 5_000_000  # ₹2k–₹50k, at most 90% of the balance
        assert p.payee_account_id in mules
    truth = {t.payment_id: t for t in history.window.payment_truth}
    assert all(
        truth[p.id].is_fraud and truth[p.id].hypothesis_key == "APP_COLLECT_REFUND" for p in refunds
    )


def test_payments_due_after_the_history_are_scheduled(long_window: Dataset) -> None:
    """docs/14 §8: in-flight mule transfers and the window's salaries are sim_schedule rows."""
    pop, history = long_window.population, long_window.history
    assert history.schedule
    reasons = {row.payload["reason"] for row in history.schedule}
    assert "SALARY" in reasons and reasons <= {"SALARY", "MULE_FORWARDING"}
    for row in history.schedule:
        assert row.kind == "TRANSFER" and row.done_at is None
        assert row.due_at >= pop.history_end
        assert row.payload["amount_paise"] > 0
        if row.payload["reason"] == "SALARY":
            assert row.due_at.astimezone(IST).day == 1
        else:  # a mule-ring payment still in flight when the history ended
            assert {"flow", "session"} <= set(row.payload)
    ids = [row.id for row in history.schedule]
    assert len(ids) == len(set(ids))


def test_payees_stay_unique_across_history_and_window(long_window: Dataset) -> None:
    pairs = [
        (p.customer_id, p.account_id)
        for p in (*long_window.population.payees, *long_window.history.payees)
    ]
    assert len(pairs) == len(set(pairs))


def test_the_stats_report(long_window: Dataset, config: GeneratorConfig) -> None:
    report = dataset_stats(long_window, config)
    assert report.history_sessions > 2_000 and report.window_sessions > 3_000
    routine = report.rate(ScenarioType.GENUINE_ROUTINE)
    assert routine.window_per_10k == pytest.approx(9_700, rel=0.02)
    assert report.rate(ScenarioType.MULE_FORWARDING).window_per_10k == 0
    assert 0 < report.held_share < 0.1 and 0.9 < report.label_coverage <= 1
    text = format_report(report)
    assert all(s.value in text for s in ScenarioType)


@pytest.mark.slow
def test_eval_matches_the_configured_rates_within_ten_percent(
    config: GeneratorConfig, kb: KnowledgeBase
) -> None:
    """#17 acceptance (FR-SIM-02): the `eval` stats report within ±10% of the configured
    rates. History: every history scenario, MULE_FORWARDING and the 40% listing share. Window:
    every scenario it holds, UPI_COLLECT_REFUND included."""
    assert os.environ.get("DELPHI_RUN_SLOW") == "1"
    dataset = generate_dataset(config, "eval", kb=kb)
    assert not _sim_swaps_without_a_report(dataset)  # #55, on eval too
    report = dataset_stats(dataset, config)
    print(format_report(report))
    for rate in report.rates:
        if rate.scenario is not ScenarioType.UPI_COLLECT_REFUND:
            assert rate.history_per_10k == pytest.approx(rate.configured_per_10k, rel=0.10), rate
        if rate.scenario is not ScenarioType.MULE_FORWARDING:
            assert rate.window_per_10k == pytest.approx(rate.configured_per_10k, rel=0.10), rate
    assert report.mule_listing_share == pytest.approx(
        config.forwarding.registry_listing_share, rel=0.10
    )


def test_cli_prints_the_stats_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sim", "stats", "--profile", "test"]) == 0
    out = capsys.readouterr().out
    assert "profile test:" in out and "MULE_FORWARDING" in out


def _sim_swaps_without_a_report(dataset: Dataset) -> list[UUID]:
    """Window SIM-swap ATO instances with no SIM_SWAP_REPORTED 1–48 h before the login."""
    end = dataset.population.history_end
    reports: dict[str, list[datetime]] = {}
    for event in dataset.history.crm_events:
        if event.type == "SIM_SWAP_REPORTED":
            reports.setdefault(str(event.customer_id), []).append(event.at)
    missing = []
    for instance in dataset.history.scenario_instances:
        if instance.params.get("kind") != "SIM_SWAP" or instance.started_at < end:
            continue
        before = [
            instance.started_at - at for at in reports.get(instance.params["customer_id"], [])
        ]
        if not any(timedelta(hours=1) <= gap <= timedelta(hours=48) for gap in before):
            missing.append(instance.id)
    return missing


def test_window_sim_swaps_are_reported_before_the_login(long_window: Dataset) -> None:
    """#55: the window's SIM-swap ATO cases keep their CRM report (docs/14 §4)."""
    end = long_window.population.history_end
    window_swaps = [
        i
        for i in long_window.history.scenario_instances
        if i.params.get("kind") == "SIM_SWAP" and i.started_at >= end
    ]
    assert window_swaps
    assert not _sim_swaps_without_a_report(long_window)


def test_background_crm_runs_on_through_the_window(long_window: Dataset) -> None:
    """#55: genuine CRM reports continue in the window, so a window report is not an ATO tell."""
    end = long_window.population.history_end
    in_window = [e for e in long_window.history.crm_events if e.at >= end]
    assert any(e.type == "DEVICE_LOST_REPORTED" for e in in_window)
    ato_customers = {
        i.params["customer_id"]
        for i in long_window.history.scenario_instances
        if i.scenario_type == "ATO" and i.started_at >= end
    }
    assert any(str(e.customer_id) not in ato_customers for e in in_window)


def test_no_window_payment_takes_more_than_the_balance(long_window: Dataset) -> None:
    """#55: window payments settle against the payer's running balance: the end-of-history
    balance, plus the window's salaries, minus earlier window payments."""
    history = long_window.history
    assert history.window is not None
    running = dict(history.balances)
    salaries = [
        (
            row.due_at,
            0,
            UUID(row.payload["payer_account_id"]),
            UUID(row.payload["payee_account_id"]),
            row.payload["amount_paise"],
        )
        for row in history.schedule
        if row.payload.get("reason") == "SALARY"
    ]
    payments = [
        (p.initiated_at, 1, p.payer_account_id, p.payee_account_id, p.amount_paise)
        for p in history.window.payments
    ]
    for _, kind, payer, payee, amount in sorted(salaries + payments, key=lambda e: (e[0], e[1])):
        if kind == 1:
            assert 0 < amount <= running[payer]
        running[payer] -= amount
        running[payee] += amount
