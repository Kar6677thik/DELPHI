"""The fraud victims' scenarios in the history: ATO, digital arrest, investment (docs/14 §4, #17).

Each scenario runs at its rate per 10,000 payments. The history's planned genuine sessions
set the scale: a scenario gets ``genuine_sessions × rate / 9,850`` payments, made up of whole
instances. Each scenario draws its instances from ``keyed_rng(seed, "fraud", scenario)``.
A victim pays layer-1 mules of a ring. The plan holds the payments as `FraudSession`s; the
history turns each one into a customer session and B1 decides it. Amounts that depend on the
victim's balance are settled when the payment is made.

The module also draws genuine customers' background CRM events, from
``keyed_rng(seed, "crm", i)`` per customer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from delphi.sim.generator_config import GeneratorConfig, Range
from delphi.sim.ids import IdStream
from delphi.sim.mules import MuleNetwork
from delphi.sim.population import Customer, Device, Payee, Population, _between, _choice
from delphi.sim.records import CrmEvent, ScenarioInstance
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType, ground_truth_key

SIM_SWAP = "SIM_SWAP"
SIM_SWAP_REPORTED = "SIM_SWAP_REPORTED"
_PAISE_PER_INR = 100
_FINGERPRINT_BYTES = 16
_VICTIM_SCENARIOS = (
    ScenarioType.ATO,
    ScenarioType.APP_DIGITAL_ARREST,
    ScenarioType.APP_INVESTMENT,
)


@dataclass(frozen=True, slots=True)
class FraudSession:
    """A victim's payment, before B1 and the funds check."""

    scenario: ScenarioType
    customer_index: int
    payee: UUID
    at: datetime
    channel: str
    instance: UUID
    coached: bool
    amount_paise: int = 0  # planned, capped at max_balance_share of the balance at the time
    balance_share: float | None = None  # else: this share of the balance at the time
    max_balance_share: float | None = None
    device: Device | None = None  # None: the customer's current device
    city: str | None = None  # None: the home city


@dataclass(frozen=True, slots=True)
class FraudPlan:
    sessions: tuple[FraudSession, ...]
    instances: tuple[ScenarioInstance, ...]
    devices: tuple[Device, ...]  # attackers' devices (ATO)
    payees: tuple[Payee, ...]  # mule payees the victims save
    crm_events: tuple[CrmEvent, ...]


def _uniform(rng: np.random.Generator, r: Range) -> float:
    return r.min + (r.max - r.min) * rng.random()


def _count(rng: np.random.Generator, r: Range) -> int:
    return int(rng.integers(int(r.min), int(r.max) + 1))


class _Planner:
    def __init__(self, config: GeneratorConfig, population: Population, network: MuleNetwork):
        self.cfg = config.fraud
        self.config = config
        self.pop = population
        self.network = network
        self.seed = config.profiles[population.profile].seed
        self.ids = {
            kind: IdStream(self.seed, kind)
            for kind in ("fraud_instance", "fraud_device", "fraud_payee", "crm_event")
        }
        self.sessions: list[FraudSession] = []
        self.instances: list[ScenarioInstance] = []
        self.devices: list[Device] = []
        self.payees: list[Payee] = []
        self.crm: list[CrmEvent] = []
        self.saved: dict[tuple[UUID, UUID], int] = {}  # index into payees
        older = config.fraud.digital_arrest.older_age
        customers = list(enumerate(population.customers))
        self.older = [i for i, c in customers if c.age >= older]
        self.younger = [i for i, c in customers if c.age < older]

    def _mule(self, rng: np.random.Generator) -> tuple[UUID, UUID]:
        """A ring, and one of its layer-1 accounts."""
        ring = self.network.rings[int(rng.integers(len(self.network.rings)))]
        layer = ring.layers[0]
        return ring.id, layer[int(rng.integers(len(layer)))]

    def _save(self, customer: Customer, account: UUID, at: datetime) -> None:
        """A victim saves each mule payee once, at the earliest instance that pays it."""
        index = self.saved.get((customer.id, account))
        if index is None:
            self.saved[customer.id, account] = len(self.payees)
            payee = Payee(self.ids["fraud_payee"].next(), customer.id, account, "Payee", at)
            self.payees.append(payee)
        elif at < self.payees[index].added_at:
            self.payees[index] = replace(self.payees[index], added_at=at)

    def _instance(
        self, scenario: ScenarioType, customer: Customer, ring: UUID, at: datetime, **params: object
    ) -> UUID:
        instance = ScenarioInstance(
            id=self.ids["fraud_instance"].next(),
            scenario_type=scenario.value,
            hypothesis_key=ground_truth_key(scenario),
            params={"customer_id": str(customer.id), "ring_id": str(ring), **params},
            started_at=at,
        )
        self.instances.append(instance)
        return instance.id

    def _start(self, rng: np.random.Generator, span: timedelta) -> datetime:
        return _between(
            rng,
            self.pop.history_start,
            max(self.pop.history_end - span, self.pop.history_start + timedelta(seconds=1)),
        )

    def ato(self, rng: np.random.Generator) -> int:
        cfg = self.cfg.ato
        index = int(rng.integers(len(self.pop.customers)))
        customer = self.pop.customers[index]
        login_at = self._start(rng, timedelta(minutes=cfg.window_minutes))
        kind = _choice(rng, cfg.kind)
        if kind == SIM_SWAP:
            lead = timedelta(hours=_uniform(rng, cfg.sim_swap_lead_hours))
            self.crm.append(
                CrmEvent(
                    self.ids["crm_event"].next(), customer.id, SIM_SWAP_REPORTED, login_at - lead
                )
            )
        device = Device(
            id=self.ids["fraud_device"].next(),
            customer_id=customer.id,
            fingerprint=rng.bytes(_FINGERPRINT_BYTES).hex(),
            os=_choice(rng, self.config.population.device_os),
            first_seen_at=login_at,
        )
        self.devices.append(device)
        cities = sorted(c for c in self.config.cities if c != customer.home_city)
        city = cities[int(rng.integers(len(cities)))]
        ring, mule = self._mule(rng)
        payee_at = login_at + timedelta(minutes=_uniform(rng, cfg.payee_lead_minutes))
        self._save(customer, mule, payee_at)
        instance = self._instance(ScenarioType.ATO, customer, ring, login_at, kind=kind)
        k = _count(rng, cfg.transfers)
        drain = _uniform(rng, cfg.drain_share)
        window = (login_at + timedelta(minutes=cfg.window_minutes) - payee_at).total_seconds()
        offsets = sorted(int(rng.random() * window) for _ in range(k))
        for j, offset in enumerate(offsets):
            # Transfer j takes drain / k of the first balance: a share of what is left.
            share = (drain / k) / (1 - j * drain / k)
            self.sessions.append(
                FraudSession(
                    ScenarioType.ATO,
                    index,
                    mule,
                    payee_at + timedelta(seconds=offset + 1),
                    _choice(rng, cfg.channel),
                    instance,
                    coached=False,
                    balance_share=share,
                    device=device,
                    city=city,
                )
            )
        return k

    def digital_arrest(self, rng: np.random.Generator, coached_share: float) -> int:
        cfg = self.cfg.digital_arrest
        pool = self.older if rng.random() < cfg.older_share and self.older else self.younger
        index = pool[int(rng.integers(len(pool)))]
        customer = self.pop.customers[index]
        span = timedelta(days=_uniform(rng, cfg.span_days))
        start = self._start(rng, span)
        ring, _ = self._mule(rng)
        coached = bool(rng.random() < coached_share)
        instance = self._instance(ScenarioType.APP_DIGITAL_ARREST, customer, ring, start)
        k = _count(rng, cfg.transfers)
        offsets = sorted(rng.random() * span.total_seconds() for _ in range(k))
        layer = next(r for r in self.network.rings if r.id == ring).layers[0]
        for offset in offsets:
            at = start + timedelta(seconds=int(offset))
            mule = layer[int(rng.integers(len(layer)))]
            self._save(
                customer, mule, at - timedelta(minutes=_uniform(rng, self.cfg.payee_lead_minutes))
            )
            self.sessions.append(
                FraudSession(
                    ScenarioType.APP_DIGITAL_ARREST,
                    index,
                    mule,
                    at,
                    _choice(rng, cfg.channel),
                    instance,
                    coached=coached,
                    balance_share=_uniform(rng, cfg.balance_share),
                )
            )
        return k

    def investment(self, rng: np.random.Generator, coached_share: float) -> int:
        cfg = self.cfg.investment
        index = int(rng.integers(len(self.pop.customers)))
        customer = self.pop.customers[index]
        history = self.pop.history_end - self.pop.history_start
        span = min(timedelta(days=_uniform(rng, cfg.span_days)), history)
        start = self._start(rng, span)
        ring, platform = self._mule(rng)
        coached = bool(rng.random() < coached_share)
        deposits, fees = _count(rng, cfg.payments), _count(rng, cfg.fee_payments)
        instance = self._instance(
            ScenarioType.APP_INVESTMENT, customer, ring, start, deposits=deposits, fees=fees
        )
        self._save(
            customer,
            platform,
            start - timedelta(minutes=_uniform(rng, self.cfg.payee_lead_minutes)),
        )
        deposit_span = span.total_seconds() * (1 - cfg.fee_phase_share)
        offsets = [0.0, *sorted(rng.random() * deposit_span for _ in range(deposits - 1))]
        scale = self.config.population.income_amount_scale[customer.income_band]
        first = (
            cfg.first_amount_inr.median
            * scale
            * float(np.exp(cfg.first_amount_inr.sigma * rng.standard_normal()))
        )
        amounts = [round(first * cfg.escalation**j) * _PAISE_PER_INR for j in range(deposits)]
        fee = round(sum(amounts) * cfg.fee_share / _PAISE_PER_INR) * _PAISE_PER_INR
        offsets += sorted(
            deposit_span + rng.random() * (span.total_seconds() - deposit_span) for _ in range(fees)
        )
        amounts += [fee] * fees
        for offset, amount in zip(offsets, amounts, strict=True):
            self.sessions.append(
                FraudSession(
                    ScenarioType.APP_INVESTMENT,
                    index,
                    platform,
                    start + timedelta(seconds=int(offset)),
                    _choice(rng, cfg.channel),
                    instance,
                    coached=coached,
                    amount_paise=max(amount, _PAISE_PER_INR),
                    max_balance_share=cfg.max_balance_share,
                )
            )
        return deposits + fees

    def background_crm(self) -> None:
        cfg = self.config.crm
        for i, customer in enumerate(self.pop.customers):
            rng = keyed_rng(self.seed, "crm", str(i))
            at = self.pop.history_start
            while True:
                at += timedelta(days=float(rng.exponential(cfg.background_mean_days)))
                if at >= self.pop.history_end:
                    break
                self.crm.append(
                    CrmEvent(self.ids["crm_event"].next(), customer.id, _choice(rng, cfg.types), at)
                )


def plan_fraud(
    config: GeneratorConfig,
    population: Population,
    network: MuleNetwork,
    *,
    genuine_sessions: int,
    coached_share: float,
) -> FraudPlan:
    """The history's fraud victims, scaled to ``genuine_sessions`` (the planned genuine
    customer sessions), and the customers' background CRM events. ``coached_share`` is the
    response models' share of well-coached DA and INV victims (docs/14 §5.2)."""
    planner = _Planner(config, population, network)
    planner.background_crm()
    for scenario in _VICTIM_SCENARIOS:
        target = round(
            genuine_sessions
            * config.fraud.rate_per_10k[scenario.value]
            / config.fraud.genuine_rate_per_10k
        )
        rng = keyed_rng(planner.seed, "fraud", scenario.value)
        planned = 0
        while planned < target:
            if scenario is ScenarioType.ATO:
                planned += planner.ato(rng)
            elif scenario is ScenarioType.APP_DIGITAL_ARREST:
                planned += planner.digital_arrest(rng, coached_share)
            else:
                planned += planner.investment(rng, coached_share)
    return FraudPlan(
        sessions=tuple(planner.sessions),
        instances=tuple(planner.instances),
        devices=tuple(planner.devices),
        payees=tuple(planner.payees),
        crm_events=tuple(planner.crm),
    )
