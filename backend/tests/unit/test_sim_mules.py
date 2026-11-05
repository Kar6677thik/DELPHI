"""Mule networks and false registry listings (docs/14 §4, §7, §9; #17)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.export import canonical_tables
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.labels import MULE, LabelSource, SubjectType
from delphi.sim.mules import MuleNetwork, generate_mule_network, ring_count
from delphi.sim.population import Population, generate_population


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


def _with(config: GeneratorConfig, **mule_rings: object) -> GeneratorConfig:
    return config.model_copy(update={"mule_rings": config.mule_rings.model_copy(update=mule_rings)})


@pytest.fixture(scope="module")
def population(config: GeneratorConfig) -> Population:
    return generate_population(
        config, "test", history_end=config.fixed_history_end.astimezone(UTC), vulnerable_age=70
    )


@pytest.fixture(scope="module")
def many_rings(config: GeneratorConfig, population: Population) -> MuleNetwork:
    """200 rings and a 10% false-listing share, for the distribution checks."""
    return generate_mule_network(
        _with(config, customers_per_ring=1, false_listing_share=0.1), population
    )


def test_ring_count(config: GeneratorConfig) -> None:
    assert ring_count(config, 200) == 2  # at least two rings, even on `test`
    assert ring_count(config, 2000) == 5
    assert ring_count(config, 20000) == 50


def test_rings_follow_docs_14_section_4(many_rings: MuleNetwork, population: Population) -> None:
    accounts = {a.id: a for a in many_rings.accounts}
    sizes, layer_counts = [], []
    for ring in many_rings.rings:
        members = [a for layer in ring.layers for a in layer]
        sizes.append(len(members))
        layer_counts.append(len(ring.layers))
        assert 5 <= len(members) <= 30
        assert 1 <= len(ring.layers) <= 4
        assert all(ring.layers)  # every layer has an account
        assert len({accounts[a].bank_code for a in members}) >= 2  # across banks
    assert set(layer_counts) == {1, 2, 3, 4}
    assert min(sizes) < 8 and max(sizes) > 27
    truth = {t.account_id: t for t in many_rings.mule_truth}
    assert set(truth) == set(accounts)
    for ring in many_rings.rings:
        for layer, ids in enumerate(ring.layers, start=1):
            assert all(truth[a].ring_id == ring.id and truth[a].layer == layer for a in ids)
    home = sum(a.bank_code == "DLPH" for a in many_rings.accounts) / len(many_rings.accounts)
    assert home == pytest.approx(0.42, abs=0.04)  # 0.4, nudged up by the shared devices


def test_home_bank_mules_are_customers_with_devices(many_rings: MuleNetwork) -> None:
    customers = {c.id: c for c in many_rings.customers}
    home = [a for a in many_rings.accounts if a.bank_code == "DLPH"]
    assert {a.customer_id for a in home} == set(customers)
    assert all(a.customer_id is None for a in many_rings.accounts if a.bank_code != "DLPH")
    assert all(19 <= c.age <= 35 and c.income_band == "LOW" for c in customers.values())
    assert Counter(d.customer_id for d in many_rings.devices) == Counter(customers.keys())
    opened = {a.customer_id: a.opened_at for a in home}
    assert all(d.first_seen_at >= opened[d.customer_id] for d in many_rings.devices)
    minimal = sum(a.kyc_level == "MINIMAL" for a in many_rings.accounts)
    assert minimal / len(many_rings.accounts) == pytest.approx(0.5, abs=0.04)


def test_account_age_and_balances(many_rings: MuleNetwork, population: Population) -> None:
    ages = [(population.history_start - a.opened_at).days for a in many_rings.accounts]
    assert min(ages) >= 4 and max(ages) <= 400
    assert all(a.balance_paise > 0 for a in many_rings.accounts)


def test_half_the_rings_share_a_device(many_rings: MuleNetwork) -> None:
    fingerprints = Counter(d.fingerprint for d in many_rings.devices)
    shared = [r for r in many_rings.rings if r.shared_fingerprint is not None]
    assert len(shared) / len(many_rings.rings) == pytest.approx(0.5, abs=0.08)
    for ring in shared:
        assert ring.shared_fingerprint is not None
        assert fingerprints[ring.shared_fingerprint] >= 3
    assert {f for f, n in fingerprints.items() if n > 1} == {r.shared_fingerprint for r in shared}


def test_numbering_continues_the_population(
    many_rings: MuleNetwork, population: Population
) -> None:
    """Mules look like any other customer or external counterparty, and never collide."""
    every = (*population.accounts, *many_rings.accounts)
    assert len({a.vpa for a in every}) == len(every)
    assert len({a.phone_number for a in every}) == len(every)
    names = [c.display_name for c in (*population.customers, *many_rings.customers)]
    assert len(set(names)) == len(names)
    assert all(n.startswith("Customer ") for n in names)


def test_false_listings(many_rings: MuleNetwork, population: Population) -> None:
    candidates = [a for a in population.accounts if not a.is_merchant]
    listed = {listing.account_id: listing.listed_at for listing in many_rings.listings}
    assert len(listed) / len(candidates) == pytest.approx(0.1, abs=0.03)
    merchants = {a.id for a in population.accounts if a.is_merchant}
    assert not set(listed) & merchants
    assert all(population.history_start <= at < population.history_end for at in listed.values())
    assert {(label.subject_id, label.due_at) for label in many_rings.labels} == {
        (str(a), at) for a, at in listed.items()
    }
    for label in many_rings.labels:
        assert label.subject_type is SubjectType.ACCOUNT
        assert label.hypothesis_key == MULE
        assert label.label_source is LabelSource.REGISTRY_LISTING


def test_a_listing_is_the_accounts_own_draw(
    config: GeneratorConfig, population: Population, many_rings: MuleNetwork
) -> None:
    """Changing the ring count leaves every account's listing as it was."""
    other = generate_mule_network(
        _with(config, customers_per_ring=50, false_listing_share=0.1), population
    )
    assert other.listings == many_rings.listings


@pytest.fixture(scope="module")
def listed_dataset(config: GeneratorConfig, kb: KnowledgeBase) -> Dataset:
    return generate_dataset(_with(config, false_listing_share=0.3), "test", kb=kb)


def test_b1_declines_payments_to_listed_payees(listed_dataset: Dataset) -> None:
    """docs/22 §2: DECLINE if the payee is listed by the time of the payment."""
    listed = {r.account_id: r.listed_at for r in listed_dataset.network.listings}
    to_listed = [p for p in listed_dataset.history.payments if p.payee_account_id in listed]
    after = [p for p in to_listed if p.initiated_at >= listed[p.payee_account_id]]
    before = [p for p in to_listed if p.initiated_at < listed[p.payee_account_id]]
    assert after and before
    assert all(p.status == "DECLINED" for p in after)
    assert not any(p.status == "DECLINED" for p in before)
    labelled = {label.subject_id for label in listed_dataset.history.labels}
    assert not any(str(p.id) in labelled for p in after)  # declined: never investigated


def test_dataset_exports_mules_and_truth(listed_dataset: Dataset) -> None:
    tables = canonical_tables(listed_dataset)
    accounts = {row["id"] for row in tables["accounts"]}
    truth = {row["account_id"]: row for row in tables["account_truth"]}
    assert set(truth) == accounts
    mules = {str(a.id) for a in listed_dataset.network.accounts}
    assert {a for a, row in truth.items() if row["is_mule"]} == mules
    assert all(row["ring_id"] is None for a, row in truth.items() if a not in mules)
    customers = {row["id"] for row in tables["customers"]}
    assert {str(c.id) for c in listed_dataset.network.customers} <= customers
    assert {row["account_id"] for row in tables["sim_registry_listings"]} == {
        str(r.account_id) for r in listed_dataset.network.listings
    }
    registry_labels = [
        row for row in tables["label_schedule"] if row["label_source"] == "REGISTRY_LISTING"
    ]
    assert len(registry_labels) == len(listed_dataset.network.listings)
