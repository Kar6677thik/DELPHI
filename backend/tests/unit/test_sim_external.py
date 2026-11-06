"""The simulated external systems (docs/14 §9, #19)."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, timedelta

import pytest

from delphi.kb.model import KnowledgeBase
from delphi.sim.dataset import generate_dataset
from delphi.sim.external import cfcfrms_ack_delay, outreach_answer, phone_risk
from delphi.sim.generator_config import GeneratorConfig, load_generator_config
from delphi.sim.mules import generate_mule_network
from delphi.sim.population import generate_population
from delphi.sim.response_models import load_response_models
from delphi.sim.scenarios import ScenarioType


@pytest.fixture(scope="module")
def config() -> GeneratorConfig:
    return load_generator_config()


def _shares(categories: list[str]) -> dict[str, float]:
    counts = Counter(categories)
    return {c: counts[c] / len(categories) for c in ("VERY_HIGH", "HIGH", "MEDIUM", "LOW")}


def test_fri_categories_follow_docs_14_section_9(config: GeneratorConfig) -> None:
    population = generate_population(
        config, "demo", history_end=config.fixed_history_end.astimezone(UTC), vulnerable_age=70
    )
    rings = config.mule_rings.model_copy(update={"customers_per_ring": 10})  # 200 rings
    network = generate_mule_network(config.model_copy(update={"mule_rings": rings}), population)
    mules = {a.id for a in network.accounts}
    rows = phone_risk(config, 1, (*population.accounts, *network.accounts), mules)
    category = {r.phone_number: r.category for r in rows}
    ring = _shares([category[a.phone_number] for a in network.accounts if a.phone_number])
    genuine = _shares([category[a.phone_number] for a in population.accounts if a.phone_number])
    assert ring["VERY_HIGH"] == pytest.approx(0.35, abs=0.03)
    assert ring["HIGH"] + ring["MEDIUM"] == pytest.approx(0.35, abs=0.03)
    assert ring["LOW"] == pytest.approx(0.30, abs=0.03)
    assert genuine["VERY_HIGH"] == pytest.approx(0.005, abs=0.003)
    assert genuine["HIGH"] + genuine["MEDIUM"] == pytest.approx(0.03, abs=0.008)
    # Each number's category is its own draw: the same with or without the other accounts.
    alone = phone_risk(config, 1, population.accounts[:5], mules)
    assert all(category[r.phone_number] == r.category for r in alone)


def test_every_phone_has_an_fri_category(config: GeneratorConfig, kb: KnowledgeBase) -> None:
    dataset = generate_dataset(config, "test", kb=kb)
    phones = {
        a.phone_number
        for a in (*dataset.population.accounts, *dataset.network.accounts)
        if a.phone_number
    }
    assert {r.phone_number for r in dataset.phone_risk} == phones


def test_cfcfrms_acknowledges_most_tickets_in_about_20_minutes(config: GeneratorConfig) -> None:
    delays = [cfcfrms_ack_delay(config, 1, f"ticket-{i}") for i in range(20_000)]
    acknowledged = [d.total_seconds() / 60 for d in delays if d is not None]
    never = 1 - len(acknowledged) / len(delays)
    assert never == pytest.approx(0.10 + 0.013, abs=0.01)  # 10%, plus latencies past 2 h
    assert statistics.median(acknowledged) == pytest.approx(20, rel=0.05)
    assert max(acknowledged) < 120
    assert cfcfrms_ack_delay(config, 1, "ticket-7") == delays[7]  # per-ticket stream
    assert all(d is None or d == timedelta(seconds=int(d.total_seconds())) for d in delays)


def test_outreach_answers_come_from_the_response_models() -> None:
    models = load_response_models()
    scenario = ScenarioType.APP_DIGITAL_ARREST
    answers = [outreach_answer(models, 1, f"payment-{i}", scenario) for i in range(20_000)]
    probs = models.answer_probs(scenario, "CUSTOMER_OUTREACH_CALL")
    counts = Counter(answers)
    for outcome, p in zip(models.outcomes["CUSTOMER_OUTREACH_CALL"], probs, strict=True):
        assert counts[outcome] / len(answers) == pytest.approx(p, abs=0.015)
    # Common random numbers (docs/22 §1): the same payment, the same answer, every time.
    assert outreach_answer(models, 1, "payment-3", scenario) == answers[3]
