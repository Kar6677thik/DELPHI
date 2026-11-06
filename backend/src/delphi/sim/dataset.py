"""A whole generated dataset: the population, its mule network and its history (docs/14, #17)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from delphi.kb.model import KnowledgeBase
from delphi.sim.external import PhoneRisk, phone_risk
from delphi.sim.generator_config import GeneratorConfig, history_end
from delphi.sim.history import History, generate_history
from delphi.sim.labels import load_label_config
from delphi.sim.mules import AccountTruth, MuleNetwork, account_truth, generate_mule_network
from delphi.sim.persona_config import load_persona_config
from delphi.sim.personas import PersonaManifest, persona_manifest, with_persona_fri
from delphi.sim.population import Population, generate_population
from delphi.sim.response_models import load_config


@dataclass(frozen=True, slots=True)
class Dataset:
    population: Population
    network: MuleNetwork
    history: History
    account_truth: tuple[AccountTruth, ...]  # one row per account
    phone_risk: tuple[PhoneRisk, ...] = ()  # the FRI category of every phone (docs/14 §9)
    personas: PersonaManifest | None = None  # the demo personas' rows (docs/14 §6)


def generate_dataset(
    config: GeneratorConfig,
    profile: str,
    *,
    kb: KnowledgeBase,
    created_at: datetime | None = None,
    personas: bool | None = None,
) -> Dataset:
    """The profile's dataset under KB v1's thresholds. ``created_at`` is needed for `demo`,
    whose history ends when it is created (docs/14 §2).

    ``personas`` adds the demo personas (docs/14 §6). By default only the profiles that
    `personas.yaml` lists get them (`demo`); the E2E stack also runs `test` with them
    (docs/21 §1). Without personas, every other row is the same."""
    persona_config = load_persona_config()
    if personas is None:
        personas = profile in persona_config.profiles
    scripted = persona_config if personas else None
    end = history_end(config, profile, created_at=created_at)
    population = generate_population(
        config,
        profile,
        history_end=end,
        vulnerable_age=kb.config.engine.vulnerable_age_threshold,
        personas=scripted,
    )
    network = generate_mule_network(config, population, scripted)
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
    accounts = (
        *population.accounts,
        *network.accounts,
        *history.payroll_accounts,
        *history.cash_out_accounts,
    )
    seed = config.profiles[profile].seed
    mules = {a.id for a in network.accounts}
    risk = phone_risk(config, seed, accounts, mules)
    manifest = None
    if scripted is not None:
        manifest = persona_manifest(scripted, population, network)
        phones = {a.id: a.phone_number for a in accounts}
        risk = with_persona_fri(scripted, manifest, phones, risk)
    return Dataset(
        population,
        network,
        history,
        account_truth(network, accounts),
        risk,
        manifest,
    )
