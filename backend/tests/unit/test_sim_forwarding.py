"""Money moving down the mule rings (docs/14 §4, §7; #17)."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, timedelta
from itertools import pairwise

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.forwarding import CASH_OUT, Forwarder
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.mules import MuleNetwork, generate_mule_network
from delphi.sim.population import generate_population


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


@pytest.fixture(scope="module")
def network(config: GeneratorConfig) -> MuleNetwork:
    population = generate_population(
        config, "test", history_end=config.fixed_history_end.astimezone(UTC), vulnerable_age=70
    )
    rings = config.mule_rings.model_copy(update={"customers_per_ring": 4})  # 50 rings
    return generate_mule_network(config.model_copy(update={"mule_rings": rings}), population)


def _forwarder(config: GeneratorConfig, network: MuleNetwork) -> Forwarder:
    return Forwarder(config, network, seed=1, start=config.fixed_history_end.astimezone(UTC))


def test_dwell_splits_and_targets(config: GeneratorConfig, network: MuleNetwork) -> None:
    forwarder = _forwarder(config, network)
    at = config.fixed_history_end.astimezone(UTC)
    dwell: dict[bool, list[float]] = {True: [], False: []}
    parts = Counter()
    for ring in network.rings:
        for layer, accounts in enumerate(ring.layers[:-1]):
            for account in accounts:
                for _ in range(10):
                    hops = forwarder.arrive(account, 3_000_000, at)
                    parts[len(hops)] += 1
                    assert sum(h.amount_paise for h in hops) == 3_000_000
                    assert all(h.amount_paise % 100 == 0 for h in hops)  # whole rupees
                    assert {h.payee for h in hops} <= set(ring.layers[layer + 1])
                    assert len({h.payee for h in hops}) == 1  # one onward transfer
                    assert all(h.session == (h.payer in _home(network)) for h in hops)
                    minutes = (hops[0].at - at).total_seconds() / 60
                    dwell[layer == 0].append(minutes)
                    gaps = [b.at - a.at for a, b in pairwise(hops)]
                    assert all(timedelta(minutes=1) <= g <= timedelta(minutes=5) for g in gaps)
    assert set(parts) == {1, 2, 3}
    assert statistics.median(dwell[True]) == pytest.approx(12, rel=0.1)  # docs/14
    assert statistics.median(dwell[False]) == pytest.approx(25, rel=0.1)


def _home(network: MuleNetwork) -> set:
    return {a.id for a in network.accounts if a.bank_code == "DLPH"}


def test_final_layer_cashes_out_forty_percent(
    config: GeneratorConfig, network: MuleNetwork
) -> None:
    forwarder = _forwarder(config, network)
    at = config.fixed_history_end.astimezone(UTC)
    bank = {a.id: a.bank_code for a in network.accounts}
    flows = 0
    cashed = 0
    for ring in network.rings:
        for account in ring.layers[-1]:
            for _ in range(10):
                flows += 1
                hops = forwarder.arrive(account, 500_000, at)
                if hops:
                    cashed += 1
                    (hop,) = hops
                    assert hop.channel == CASH_OUT and not hop.session
                    sink = forwarder.cash_accounts[bank[account]]
                    assert hop.payee == sink.id and hop.amount_paise == 500_000
    assert cashed / flows == pytest.approx(0.4, abs=0.05)  # docs/14


def test_a_transfer_moves_on_once_all_its_payments_resolve(
    config: GeneratorConfig, network: MuleNetwork
) -> None:
    forwarder = _forwarder(config, network)
    at = config.fixed_history_end.astimezone(UTC)
    ring = next(r for r in network.rings if len(r.layers) >= 3)
    hops = []
    while len(hops) < 2:  # a transfer with at least two payments
        hops = forwarder.arrive(ring.layers[0][0], 1_000_000, at)
    flow = hops[0].flow
    assert forwarder.resolve(flow, hops[0].amount_paise, hops[0].at) == []
    for hop in hops[1:-1]:
        assert forwarder.resolve(flow, hop.amount_paise, hop.at) == []
    # The last payment was held: only what was executed moves on, from the next layer.
    onward = forwarder.resolve(flow, 0, hops[-1].at)
    assert sum(h.amount_paise for h in onward) == 1_000_000 - hops[-1].amount_paise
    assert {h.payer for h in onward} == {hops[0].payee}
    assert all(h.payee in ring.layers[2] for h in onward)
    # Nothing executed: the flow stops.
    stopped = forwarder.arrive(ring.layers[0][0], 100, at)
    assert forwarder.resolve(stopped[0].flow, 0, at) == []


@pytest.fixture(scope="module")
def long_test(config: GeneratorConfig, kb: KnowledgeBase) -> Dataset:
    profiles = {
        **config.profiles,
        "test": config.profiles["test"].model_copy(update={"history_days": 150}),
    }
    return generate_dataset(config.model_copy(update={"profiles": profiles}), "test", kb=kb)


def test_forwarding_in_the_history(long_test: Dataset) -> None:
    network, history = long_test.network, long_test.history
    kinds = {i.id: i.scenario_type for i in history.scenario_instances}
    scenario = {t.payment_id: kinds[t.scenario_instance_id] for t in history.payment_truth}
    truth = {t.payment_id: t for t in history.payment_truth}
    mules = {a.id: a for a in network.accounts}
    sinks = {a.id for a in history.cash_out_accounts}
    forwarding = [p for p in history.payments if scenario[p.id] == "MULE_FORWARDING"]
    assert forwarding
    for p in forwarding:
        assert p.payer_account_id in mules and truth[p.id].is_fraud
        assert truth[p.id].hypothesis_key == "MULE"
        # Home-bank mules forward in app sessions; external mules and cash-outs do not.
        session = mules[p.payer_account_id].bank_code == "DLPH" and p.channel != CASH_OUT
        assert (p.device_id is not None) == session
        assert (p.channel == CASH_OUT) == (p.payee_account_id in sinks)
    assert any(p.channel == CASH_OUT for p in forwarding)
    assert min(history.balances[a] for a in mules) >= 0
    sessions = [p for p in history.payments if p.device_id is not None]
    rate = 10_000 * sum(scenario[p.id] == "MULE_FORWARDING" for p in sessions) / len(sessions)
    assert 10 < rate < 60  # 30 per 10k on average over many rings; `test` has two


def test_registry_lists_forty_percent_of_credited_mules(long_test: Dataset) -> None:
    network, history = long_test.network, long_test.history
    mules = {a.id for a in network.accounts}
    first_credit: dict = {}
    for p in sorted(history.payments, key=lambda p: p.initiated_at):
        if p.payee_account_id in mules and p.status == "EXECUTED":
            first_credit.setdefault(p.payee_account_id, p.initiated_at)
    listed = {r.account_id: r.listed_at for r in history.registry_listings}
    assert set(listed) <= set(first_credit)
    assert len(listed) / len(first_credit) == pytest.approx(0.4, abs=0.2)
    labels = {label.subject_id: label for label in history.account_labels}
    for account, at in listed.items():
        assert labels[str(account)].due_at == at > first_credit[account]  # docs/14 §7
    # B1 declines payments to a mule once it is listed.
    for p in history.payments:
        if p.payee_account_id in listed and p.device_id is not None:
            assert (p.status == "DECLINED") == (p.initiated_at >= listed[p.payee_account_id])


def test_home_bank_mules_save_their_payees_first(long_test: Dataset) -> None:
    """Owner review of #51: a forwarding payee is saved before the first payment to it, so
    B1's new-payee input follows docs/13 §3 rather than staying new forever."""
    network, history = long_test.network, long_test.history
    customer_of = {a.id: a.customer_id for a in network.accounts if a.customer_id}
    saved = {(p.customer_id, p.account_id): p for p in history.payees}
    sessions = [
        p
        for p in sorted(history.payments, key=lambda p: p.initiated_at)
        if p.payer_account_id in customer_of and p.device_id is not None
    ]
    assert sessions
    for p in sessions:
        payee = saved[customer_of[p.payer_account_id], p.payee_account_id]
        assert payee.added_at <= p.initiated_at
        assert payee.label.startswith("Family ")
