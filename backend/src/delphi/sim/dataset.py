"""A whole generated dataset: the population, its mule network and its history (docs/14, #17)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from delphi.kb.model import KnowledgeBase
from delphi.sim.generator_config import GeneratorConfig, history_end
from delphi.sim.history import History, generate_history
from delphi.sim.labels import load_label_config
from delphi.sim.mules import AccountTruth, MuleNetwork, account_truth, generate_mule_network
from delphi.sim.population import Population, generate_population
from delphi.sim.response_models import load_config


@dataclass(frozen=True, slots=True)
class Dataset:
    population: Population
    network: MuleNetwork
    history: History
    account_truth: tuple[AccountTruth, ...]  # one row per account


def generate_dataset(
    config: GeneratorConfig, profile: str, *, kb: KnowledgeBase, created_at: datetime | None = None
) -> Dataset:
    """The profile's dataset under KB v1's thresholds. ``created_at`` is needed for `demo`,
    whose history ends when it is created (docs/14 §2)."""
    end = history_end(config, profile, created_at=created_at)
    population = generate_population(
        config, profile, history_end=end, vulnerable_age=kb.config.engine.vulnerable_age_threshold
    )
    network = generate_mule_network(config, population)
    response_config = load_config()
    history = generate_history(
        config,
        population,
        targets=response_config.telemetry_targets,
        thresholds=kb.config.classifiers,
        label_config=load_label_config(),
        network=network,
        coached_share=response_config.coached_victims.share,
    )
    accounts = (*population.accounts, *network.accounts, *history.payroll_accounts)
    return Dataset(population, network, history, account_truth(network, accounts))
