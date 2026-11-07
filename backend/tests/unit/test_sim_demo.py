"""Checks on the `demo` profile itself (#56, #57): one generation, about 35 s, shared."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import Dataset, generate_dataset
from delphi.sim.generator_config import load_generator_config

CREATED_AT = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo(kb: KnowledgeBase) -> Dataset:
    return generate_dataset(load_generator_config(), "demo", kb=kb, created_at=CREATED_AT)


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
