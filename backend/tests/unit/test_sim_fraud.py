"""The fraud victims' scenarios in the history (docs/14 §4, §5.2, §7; #17)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import pairwise
from uuid import UUID

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.history import Payment
from delphi.sim.labels import LabelSource


@pytest.fixture(scope="module")
def long_test(kb: KnowledgeBase) -> Dataset:
    """The `test` population over a 150-day history: enough victims to count."""
    config = load_generator_config()
    profiles = {
        **config.profiles,
        "test": config.profiles["test"].model_copy(update={"history_days": 150}),
    }
    return generate_dataset(config.model_copy(update={"profiles": profiles}), "test", kb=kb)


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


def _instances(dataset: Dataset, scenario: str) -> dict[UUID, list[Payment]]:
    """Each instance of the scenario with its payments, in time order."""
    history = dataset.history
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    of = {t.payment_id: t.scenario_instance_id for t in history.payment_truth}
    out: dict[UUID, list[Payment]] = defaultdict(list)
    for p in sorted(history.payments, key=lambda p: p.initiated_at):
        if kinds[of[p.id]] == scenario:
            out[of[p.id]].append(p)
    return dict(out)


def _layer_one(dataset: Dataset) -> set[UUID]:
    return {a for ring in dataset.network.rings for a in ring.layers[0]}


def test_scenario_rates_per_10k_sessions(long_test: Dataset) -> None:
    """docs/14 §4: ATO 40, DA 30, INV 40 per 10,000 payments, in the history too."""
    history = long_test.history
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    scenario = {t.payment_id: kinds[t.scenario_instance_id] for t in history.payment_truth}
    sessions = Counter(scenario[p.id] for p in history.payments if p.device_id is not None)
    total = sum(sessions.values())
    for name, rate in {"ATO": 40, "APP_DIGITAL_ARREST": 30, "APP_INVESTMENT": 40}.items():
        assert 10_000 * sessions[name] / total == pytest.approx(rate, rel=0.25), name


def test_fraud_truth(long_test: Dataset) -> None:
    history = long_test.history
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    for t in history.payment_truth:
        fraud = kinds[t.scenario_instance_id] in {"ATO", "APP_DIGITAL_ARREST", "APP_INVESTMENT"}
        assert t.is_fraud == fraud
        assert t.hypothesis_key == kinds[t.scenario_instance_id] or not fraud
        if kinds[t.scenario_instance_id] == "ATO":
            assert not t.coached


def test_ato_follows_docs_14_section_4(long_test: Dataset) -> None:
    """New device in a new city, a mule payee saved after the login, 1-3 transfers within
    30 minutes that drain at most 95% of the balance."""
    pop, history = long_test.population, long_test.history
    home = {c.id: c.home_city for c in pop.customers}
    seen = {d.id: d.first_seen_at for d in history.devices}
    logins = {(login.device_id, login.customer_id): login for login in history.logins}
    customer_of = {a.id: a.customer_id for a in pop.accounts if a.customer_id}
    saved = {(p.customer_id, p.account_id): p.added_at for p in history.payees}
    layer_one = _layer_one(long_test)
    instances = _instances(long_test, "ATO")
    assert len(instances) > 20
    sizes = Counter(len(payments) for payments in instances.values())
    assert set(sizes) <= {1, 2, 3}
    for payments in instances.values():
        first = payments[0]
        customer = customer_of[first.payer_account_id]
        assert first.device_id is not None and first.device_id in seen
        login = logins[first.device_id, customer]
        assert login.city != home[customer]
        assert all(p.payee_account_id in layer_one for p in payments)
        assert saved[customer, first.payee_account_id] <= first.initiated_at
        assert payments[-1].initiated_at - seen[first.device_id] <= timedelta(minutes=30)
    # Balances: what the executed transfers took, against the balance before the first.
    debits = {e.payment_id: e for e in history.ledger if e.delta_paise < 0}
    for payments in instances.values():
        executed = [p for p in payments if p.status == "EXECUTED"]
        if len(executed) == len(payments):
            before = debits[executed[0].id].balance_after_paise + executed[0].amount_paise
            taken = sum(p.amount_paise for p in executed)
            assert 0.55 * before <= taken <= 0.95 * before


def test_digital_arrest_victims_are_older_and_pay_over_days(long_test: Dataset) -> None:
    pop = long_test.population
    age = {a.id: c.age for c in pop.customers for a in pop.accounts if a.customer_id == c.id}
    instances = _instances(long_test, "APP_DIGITAL_ARREST")
    assert len(instances) > 15
    older = [age[payments[0].payer_account_id] >= 60 for payments in instances.values()]
    assert sum(older) / len(older) == pytest.approx(0.6, abs=0.2)
    layer_one = _layer_one(long_test)
    for payments in instances.values():
        assert len(payments) <= 4
        assert payments[-1].initiated_at - payments[0].initiated_at <= timedelta(days=3)
        assert all(p.payee_account_id in layer_one for p in payments)


def test_investment_escalates_then_charges_fees(long_test: Dataset) -> None:
    """Deposits grow × 1.5 unless capped at 90% of the balance; then withdrawal fees."""
    history = long_test.history
    params = {i.id: i.params for i in history.scenario_instances}
    before = {
        e.payment_id: e.balance_after_paise - e.delta_paise
        for e in history.ledger
        if e.delta_paise < 0
    }
    instances = _instances(long_test, "APP_INVESTMENT")
    assert len(instances) > 5
    capped = 0
    for instance, payments in instances.items():
        deposits, fees = params[instance]["deposits"], params[instance]["fees"]
        assert 3 <= deposits <= 8 and 1 <= fees <= 2
        assert len(payments) <= deposits + fees
        assert len({p.payee_account_id for p in payments}) == 1  # one "platform" account
        assert payments[-1].initiated_at - payments[0].initiated_at <= timedelta(days=42)
        executed = [p for p in payments[:deposits] if p.id in before]
        for earlier, later in pairwise(executed):
            cap = int(before[later.id] * 0.9) // 100 * 100
            assert later.amount_paise >= earlier.amount_paise or later.amount_paise == cap
            capped += later.amount_paise == cap
        assert all(p.amount_paise <= 0.9 * before[p.id] for p in payments if p.id in before)
    assert capped  # low-income victims run out of money


def test_coached_victims(long_test: Dataset) -> None:
    """docs/14 §5.2: 30% of DA and INV instances are well coached."""
    history = long_test.history
    coached = {t.scenario_instance_id: t.coached for t in history.payment_truth if t.is_fraud}
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    apps = [c for i, c in coached.items() if kinds[i] != "ATO"]
    assert sum(apps) / len(apps) == pytest.approx(0.3, abs=0.12)


def test_complaints_follow_the_labels(long_test: Dataset, config: GeneratorConfig) -> None:
    pop, history = long_test.population, long_test.history
    customer_of = {a.id: a.customer_id for a in pop.accounts if a.customer_id}
    payments = {p.id: p for p in history.payments}
    complained = {
        (label.subject_id, label.due_at)
        for label in history.labels
        if label.label_source is LabelSource.CUSTOMER_COMPLAINT and label.due_at < pop.history_end
    }
    assert complained
    assert {(str(c.payment_ids[0]), c.reported_at) for c in history.complaints} == complained
    for c in history.complaints:
        payment = payments[c.payment_ids[0]]
        assert payment.status == "EXECUTED"  # only released fraud is reported
        assert c.victim_customer_id == customer_of[payment.payer_account_id]
        assert c.counterparty_account_ids == (payment.payee_account_id,)
        assert c.channel in {"NCRP_1930", "BRANCH"}
    categories = set(config.fraud.complaint_category.values())
    assert {c.category for c in history.complaints} <= categories


def test_crm_events(long_test: Dataset) -> None:
    """Background SIM-swap / lost-phone reports once per 500 days, plus a SIM swap report
    before every SIM-swap ATO."""
    pop, history = long_test.population, long_test.history
    days = (pop.history_end - pop.history_start).days
    expected = len(pop.customers) * days / 500
    sim_swaps = [i for i in history.scenario_instances if i.params.get("kind") == "SIM_SWAP"]
    assert len(history.crm_events) == pytest.approx(expected + len(sim_swaps), rel=0.35)
    reports = defaultdict(list)
    for event in history.crm_events:
        reports[event.customer_id].append(event)
    for instance in sim_swaps:
        customer = UUID(instance.params["customer_id"])
        assert any(
            e.type == "SIM_SWAP_REPORTED"
            and timedelta(hours=1) <= instance.started_at - e.at <= timedelta(hours=48)
            for e in reports[customer]
        )


def test_mule_payees_look_like_any_other_payee(long_test: Dataset) -> None:
    """Owner review of #50: no payee label marks a fraud payee out."""
    labels = {p.label for p in long_test.history.payees}
    assert "Payee" not in labels
    mules = {a.id for a in long_test.network.accounts}
    assert all(
        p.label.startswith("Family ") for p in long_test.history.payees if p.account_id in mules
    )


def test_investment_fees_follow_the_deposits_made(long_test: Dataset) -> None:
    history = long_test.history
    params = {i.id: i.params for i in history.scenario_instances}
    before = {
        e.payment_id: e.balance_after_paise - e.delta_paise
        for e in history.ledger
        if e.delta_paise < 0
    }
    checked = 0
    for instance, payments in _instances(long_test, "APP_INVESTMENT").items():
        deposits = params[instance]["deposits"]
        made = sum(p.amount_paise for p in payments[:deposits] if p.status == "EXECUTED")
        for fee in payments[deposits:]:
            expected = round(made * 0.10 / 100) * 100
            cap = int(before.get(fee.id, 10**15) * 0.9) // 100 * 100
            assert fee.amount_paise == min(expected, cap)
            checked += 1
    assert checked


def test_crm_events_fall_inside_the_history(long_test: Dataset) -> None:
    start = long_test.population.history_start
    assert all(e.at >= start for e in long_test.history.crm_events)
