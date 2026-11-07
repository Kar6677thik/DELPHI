"""Checks on the `demo` profile itself (#56, #57, #64): one generation, about 35 s, shared."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.population import Payee

CREATED_AT = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo(kb: KnowledgeBase) -> Dataset:
    return generate_dataset(load_generator_config(), "demo", kb=kb, created_at=CREATED_AT)


def duplicate_family_labels(dataset: Dataset) -> list[UUID]:
    """The customers with two person payees under the same "Family N" label. Merchant payees
    are labelled by category, so two utilities are both "Utility", as in the population; only
    genuine customers save merchants, so that says nothing about mules."""
    labels: defaultdict[UUID, list[str]] = defaultdict(list)
    for payee in (*dataset.population.payees, *dataset.history.payees):
        if payee.label.startswith("Family "):
            labels[payee.customer_id].append(payee.label)
    return [c for c, names in labels.items() if len(names) != len(set(names))]


def test_person_payees_saved_in_the_history_are_not_all_mules(demo: Dataset) -> None:
    """#56: genuine customers save new people too, so a person payee saved during the
    history no longer marks a mule (before: 852 mule, 0 genuine on `demo`)."""
    pop, history = demo.population, demo.history
    mules = {a.id for a in demo.network.accounts}
    accounts = {a.id: a for a in (*pop.accounts, *demo.network.accounts)}
    people = [
        p
        for p in history.payees
        if not accounts[p.account_id].is_merchant and p.added_at < pop.history_end
    ]
    genuine = sum(p.account_id not in mules for p in people)
    assert genuine / len(people) == pytest.approx(0.5, abs=0.1)
    labels = {p.label.split(" ")[0] for p in people}
    assert labels == {"Family"}  # the same naming for both


def test_young_accounts_are_a_signal_not_a_tell(demo: Dataset) -> None:
    """#56: 10% of genuine accounts are opened 30–365 days before the history, so most young
    accounts are genuine (before: only mules were under a year old)."""
    pop, network = demo.population, demo.network
    start = pop.history_start
    genuine = [a for a in pop.accounts if not a.is_merchant]
    young = [a for a in genuine if start - a.opened_at < timedelta(days=365)]
    assert len(young) / len(genuine) == pytest.approx(0.10, rel=0.1)
    end = pop.history_end
    young_genuine = sum(end - a.opened_at < timedelta(days=365) for a in genuine)
    young_mules = sum(end - a.opened_at < timedelta(days=365) for a in network.accounts)
    assert young_mules / (young_mules + young_genuine) < 0.5


def test_the_personas_keep_their_scripted_devices(demo: Dataset) -> None:
    """#57: personas get no new phones in the history, so Kamala, Arjun and Ravi have the one
    device each that docs/14 §6 gives them, however long the history runs."""
    assert demo.personas is not None
    personas = {c.id for c in demo.population.customers if c.persona_key}
    assert not [d for d in demo.history.devices if d.customer_id in personas]
    for key in ("KAMALA", "ARJUN", "RAVI"):
        assert len(demo.personas.devices[key]) == 1, key


def test_no_customer_has_two_person_payees_with_the_same_label(demo: Dataset) -> None:
    """#64: one "Family N" count per customer across the genuine, fraud and mule payees (before:
    185 customers on `demo` had a genuine and a mule payee under the same label)."""
    assert not duplicate_family_labels(demo)


def test_family_numbers_follow_the_saving_order(demo: Dataset) -> None:
    """#64: a customer's new payees take their places after the population's, in the order they
    were saved, so the numbers do not show which ones a fraud saved."""
    places = Counter(p.customer_id for p in demo.population.payees)
    saved: defaultdict[UUID, list[Payee]] = defaultdict(list)
    for payee in demo.history.payees:
        saved[payee.customer_id].append(payee)
    numbered = 0
    for customer, payees in saved.items():
        ordered = sorted(payees, key=lambda p: (p.added_at, p.id))
        for place, payee in enumerate(ordered, start=places[customer] + 1):
            if payee.label.startswith("Family "):  # merchants keep their category
                assert payee.label == f"Family {place}"
                numbered += 1
    assert numbered > 1000


def test_personas_take_no_part_in_the_person_payee_process(demo: Dataset) -> None:
    """#64 (owner decision): persona customers save no new person payees and nobody saves a
    persona's account, so the persona scripts stay fully controlled (before: Ravi saved 2,
    Arjun 1)."""
    manifest = demo.personas
    assert manifest is not None
    personas = set(manifest.customers.values())
    merchants = {a.id for a in demo.population.accounts if a.is_merchant}
    people = [p for p in demo.history.payees if p.account_id not in merchants]
    assert not [p for p in people if p.customer_id in personas]
    assert not {p.account_id for p in demo.history.payees} & set(manifest.accounts.values())
