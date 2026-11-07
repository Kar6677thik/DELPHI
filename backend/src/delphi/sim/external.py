"""The simulated external systems (docs/14 §9, #19).

- **FRI (DoT):** a risk category per phone number, in `bank.sim_phone_risk`. Numbers used by
  fraud rings (the mule accounts') are VERY_HIGH 0.35 / HIGH-MEDIUM 0.35 / LOW 0.30. Genuine
  numbers are VERY_HIGH 0.005 / MEDIUM-HIGH 0.03, the rest LOW. Each number draws from
  ``keyed_rng(seed, "fri", phone_number)``.
- **CFCFRMS:** a ticket is acknowledged after a log-normal latency (median 20 min), and 10% are
  never acknowledged within 2 h. Each ticket draws from ``keyed_rng(seed, "cfcfrms", ticket)``.
  The caller applies the demo time scale (docs/14 §8).
- **Customer outreach call:** samples `CUSTOMER_OUTREACH_CALL` from the response models, from
  ``keyed_rng(seed, "outreach", payment_id)``, so every policy hears the same answer for the
  same payment (docs/22 §1).

The suspect registry (`bank.sim_registry_listings`, §7) and the telco call status (session
telemetry) are generated with the dataset. The external banks' ledgers live in the same
database.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from delphi.sim.draws import choice
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.population import Account
from delphi.sim.response_models import ResponseModels
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType

OUTREACH_SOURCE = "CUSTOMER_OUTREACH_CALL"
_SECONDS_PER_MINUTE = 60


@dataclass(frozen=True, slots=True)
class PhoneRisk:
    """A row of ``bank.sim_phone_risk``."""

    phone_number: str
    category: str


def phone_risk(
    config: GeneratorConfig, seed: int, accounts: Iterable[Account], ring_accounts: set[UUID]
) -> tuple[PhoneRisk, ...]:
    """The FRI category of every account's phone number."""
    fri = config.external_systems.fri
    rows = []
    for account in accounts:
        if account.phone_number is None:
            continue
        weights = fri.ring_numbers if account.id in ring_accounts else fri.genuine_numbers
        rng = keyed_rng(seed, "fri", account.phone_number)
        rows.append(PhoneRisk(account.phone_number, choice(rng, weights)))
    return tuple(rows)


def cfcfrms_ack_delay(config: GeneratorConfig, seed: int, ticket_id: str) -> timedelta | None:
    """When CFCFRMS acknowledges a ticket, in simulated time; ``None`` if it never does
    within the deadline (docs/14 §9: 10% of tickets). The latency of an acknowledged ticket is
    the log-normal truncated below the deadline (redrawn from the ticket's stream), so exactly
    ``never_ack_share`` are never acknowledged (#57)."""
    cfg = config.external_systems.cfcfrms
    rng = keyed_rng(seed, "cfcfrms", ticket_id)
    if rng.random() < cfg.never_ack_share:
        return None
    while True:
        minutes = cfg.ack_minutes.median * math.exp(cfg.ack_minutes.sigma * rng.standard_normal())
        if minutes < cfg.ack_deadline_minutes:
            return timedelta(seconds=int(minutes * _SECONDS_PER_MINUTE))


def outreach_answer(
    models: ResponseModels,
    seed: int,
    payment_id: str,
    scenario: ScenarioType,
    *,
    coached: bool = False,
    sim_day: float = 0.0,
) -> str:
    """The customer's answer to a verified outreach call about a payment (docs/14 §9)."""
    rng = keyed_rng(seed, "outreach", payment_id)
    return models.sample_answer(rng, scenario, OUTREACH_SOURCE, coached=coached, sim_day=sim_day)
