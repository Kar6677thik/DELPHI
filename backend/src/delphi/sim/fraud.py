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

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

import numpy as np

from delphi.sim.draws import IST, between, choice
from delphi.sim.generator_config import GeneratorConfig, Range
from delphi.sim.ids import IdStream
from delphi.sim.mules import MuleNetwork
from delphi.sim.population import FAMILY, Customer, Device, Payee, Population
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
    fee_share: float | None = None  # a fee: this share of the instance's deposits made so far
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
    def __init__(
        self,
        config: GeneratorConfig,
        population: Population,
        network: MuleNetwork,
        span: tuple[datetime, datetime],
        stream: str,
    ):
        self.start, self.end = span
        self.stream = stream
        self.habits: Mapping[int, float] = {}  # customer index → habitual local hour (#166)
        self.saved_before: frozenset[tuple[UUID, UUID]] = frozenset()
        self.cfg = config.fraud
        self.config = config
        self.pop = population
        self.network = network
        self.seed = config.profiles[population.profile].seed
        self.ids = {
            # The window's ids come from their own streams, so they never meet the history's.
            kind: IdStream(self.seed, kind if stream == "history" else f"{stream}_{kind}")
            for kind in ("fraud_instance", "fraud_device", "fraud_payee", "crm_event")
        }
        self.sessions: list[FraudSession] = []
        self.instances: list[ScenarioInstance] = []
        self.devices: list[Device] = []
        self.payees: list[Payee] = []
        self.crm: list[CrmEvent] = []
        self.saved: dict[tuple[UUID, UUID], int] = {}  # index into payees
        older = config.fraud.digital_arrest.older_age
        # Personas are scripted (docs/14 §6): no fraud happens to them or through their rings
        # before their story, so the S-4 victims have "no history of the scam" (§8).
        customers = [(i, c) for i, c in enumerate(population.customers) if c.persona_key is None]
        self.victims = [i for i, _ in customers]
        self.older = [i for i, c in customers if c.age >= older]
        self.younger = [i for i, c in customers if c.age < older]
        self.rings = [r for r in network.rings if r.persona_key is None]
        self._unlisted: list[tuple[UUID, UUID]] | None = None  # see _unlisted_mules

    def _mule(self, rng: np.random.Generator) -> tuple[UUID, UUID]:
        """A ring, and one of its layer-1 accounts."""
        ring = self.rings[int(rng.integers(len(self.rings)))]
        layer = ring.layers[0]
        return ring.id, layer[int(rng.integers(len(layer)))]

    def _save(self, customer: Customer, account: UUID, at: datetime) -> None:
        """A victim saves each mule payee once, at the earliest instance that pays it. It is a
        person payee like any family one; the history numbers it ("Family N") with the
        customer's other payees in the order they were saved (#64), so nothing in
        `bank.payees` marks a mule out."""
        # Intended: a save is clamped to the start of the stream it belongs to. An early save
        # (#166) near the start of the window stream so keeps only part of its lead; the
        # history before the window already has the customer's real payees.
        at = max(at, self.start)
        if (customer.id, account) in self.saved_before:
            return  # saved during the history already
        index = self.saved.get((customer.id, account))
        if index is None:
            self.saved[customer.id, account] = len(self.payees)
            payee = Payee(self.ids["fraud_payee"].next(), customer.id, account, FAMILY, at)
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

    def _timed(self, scenario: ScenarioType, index: int, at: datetime, j: int = 0) -> datetime:
        """#166: with the scenario's ``habitual_share``, the payment moves to the victim's
        habitual hour that local day (+ N(0, hour_sd)); otherwise, or if that falls outside
        the span or the customer has no recorded habit, it keeps ``at``. Each payment draws
        from its own stream, so no other draw moves."""
        timing = self.cfg.timing
        share = timing.habitual_share.get(scenario.value, 0.0)
        rng = keyed_rng(
            self.seed, self.stream, "fraud_hour", scenario.value, str(len(self.instances)), str(j)
        )
        if rng.random() >= share or index not in self.habits:
            return at
        hour = self.habits[index] + timing.hour_sd * float(rng.standard_normal())
        day = at.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        moved = (day + timedelta(seconds=int(hour * 3600))).astimezone(at.tzinfo)
        return moved if self.start <= moved < self.end else at

    def _start(self, rng: np.random.Generator, span: timedelta) -> datetime:
        return between(rng, self.start, max(self.end - span, self.start + timedelta(seconds=1)))

    def ato(self, rng: np.random.Generator) -> int:
        cfg = self.cfg.ato
        index = self.victims[int(rng.integers(len(self.victims)))]
        customer = self.pop.customers[index]
        login_at = self._start(rng, timedelta(minutes=cfg.window_minutes))
        login_at = min(
            self._timed(ScenarioType.ATO, index, login_at),
            self.end - timedelta(minutes=cfg.window_minutes),
        )
        kind = choice(rng, cfg.kind)
        if kind == SIM_SWAP:
            lead = timedelta(hours=_uniform(rng, cfg.sim_swap_lead_hours))
            self.crm.append(
                CrmEvent(
                    self.ids["crm_event"].next(),
                    customer.id,
                    SIM_SWAP_REPORTED,
                    max(login_at - lead, self.pop.history_start),
                )
            )
        device = Device(
            id=self.ids["fraud_device"].next(),
            customer_id=customer.id,
            fingerprint=rng.bytes(_FINGERPRINT_BYTES).hex(),
            os=choice(rng, self.config.population.device_os),
            first_seen_at=login_at,
        )
        self.devices.append(device)
        cities = sorted(c for c in self.config.cities if c != customer.home_city)
        city = cities[int(rng.integers(len(cities)))]
        ring, mule = self._mule(rng)
        payee_at = login_at + timedelta(minutes=_uniform(rng, cfg.payee_lead_minutes))
        # #166: a staged takeover saves the mule in an earlier visit and drains the account
        # once the new-payee cooling-off has passed. Its own stream, so no other draw moves.
        staging = keyed_rng(self.seed, self.stream, "ato_staged", str(len(self.instances)))
        start = payee_at
        if staging.random() < cfg.staged_share:
            hours = _uniform(staging, cfg.staged_lead_hours)
            start = login_at
            payee_at = max(login_at - timedelta(hours=hours), self.pop.history_start)
        self._save(customer, mule, payee_at)
        instance = self._instance(ScenarioType.ATO, customer, ring, login_at, kind=kind)
        k = _count(rng, cfg.transfers)
        drain = _uniform(rng, cfg.drain_share)
        window = (login_at + timedelta(minutes=cfg.window_minutes) - start).total_seconds()
        offsets = sorted(int(rng.random() * window) for _ in range(k))
        for j, offset in enumerate(offsets):
            # Transfer j takes drain / k of the first balance: a share of what is left.
            share = (drain / k) / (1 - j * drain / k)
            self.sessions.append(
                FraudSession(
                    ScenarioType.ATO,
                    index,
                    mule,
                    start + timedelta(seconds=offset + 1),
                    choice(rng, cfg.channel),
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
        times = sorted(
            max(
                self._timed(
                    ScenarioType.APP_DIGITAL_ARREST, index, start + timedelta(seconds=int(o)), j
                ),
                start,
            )
            for j, o in enumerate(offsets)
        )
        # #166: payee timing from its own stream, so the scenario's other draws do not move.
        payees = keyed_rng(self.seed, self.stream, "fraud_payee", str(len(self.instances)))
        paid: list[UUID] = []
        for at in times:
            mule = layer[int(rng.integers(len(layer)))]
            if paid and payees.random() < cfg.reuse_payee_share:
                mule = paid[int(payees.integers(len(paid)))]  # back to an account already paid
            lead = timedelta(minutes=_uniform(rng, self.cfg.payee_lead_minutes))
            if not paid and payees.random() < cfg.early_payee_share:
                lead = timedelta(days=_uniform(payees, cfg.early_payee_days))
            self._save(customer, mule, at - lead)
            paid.append(mule)
            self.sessions.append(
                FraudSession(
                    ScenarioType.APP_DIGITAL_ARREST,
                    index,
                    mule,
                    at,
                    choice(rng, cfg.channel),
                    instance,
                    coached=coached,
                    balance_share=_uniform(rng, cfg.balance_share),
                )
            )
        return k

    def investment(self, rng: np.random.Generator, coached_share: float) -> int:
        cfg = self.cfg.investment
        index = self.victims[int(rng.integers(len(self.victims)))]
        customer = self.pop.customers[index]
        span = min(timedelta(days=_uniform(rng, cfg.span_days)), self.end - self.start)
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
        offsets += sorted(
            deposit_span + rng.random() * (span.total_seconds() - deposit_span) for _ in range(fees)
        )
        # A fee is fee_share of the deposits actually made, which the history settles.
        amounts += [0] * fees
        # #166: deposits and fees move to the victim's habitual hour, keeping their order (the
        # deposits escalate) and never before the platform payee was saved (at ``start``).
        moments = [
            max(
                self._timed(
                    ScenarioType.APP_INVESTMENT, index, start + timedelta(seconds=int(o)), j
                ),
                start,
            )
            for j, o in enumerate(offsets)
        ]
        offsets = [(m - start).total_seconds() for m in sorted(moments)]
        for j, (offset, amount) in enumerate(zip(offsets, amounts, strict=True)):
            self.sessions.append(
                FraudSession(
                    ScenarioType.APP_INVESTMENT,
                    index,
                    platform,
                    start + timedelta(seconds=int(offset)),
                    choice(rng, cfg.channel),
                    instance,
                    coached=coached,
                    amount_paise=max(amount, _PAISE_PER_INR),
                    max_balance_share=cfg.max_balance_share,
                    fee_share=cfg.fee_share if j >= deposits else None,
                )
            )
        return deposits + fees

    def _unlisted_mules(self) -> list[tuple[UUID, UUID]]:
        """Every (ring, layer-1 account) the sim registry never lists: the registry's own
        per-account draw (``mule_listing``, docs/14 §7) says no, so the account stays
        CLEAN_UNKNOWN to Guardian's beneficiary check (v18, #36)."""
        if self._unlisted is None:
            share = self.config.forwarding.registry_listing_share
            self._unlisted = [
                (ring.id, account)
                for ring in self.rings
                for account in ring.layers[0]
                if keyed_rng(self.seed, "mule_listing", str(account)).random() >= share
            ]
        return self._unlisted

    def prize_advance_fee(self, rng: np.random.Generator) -> int:
        """The hidden PRIZE_ADVANCE_FEE scenario (docs/14 §4, S-4's stand-in): told they won a
        prize, the victim pays its "tax" by UPI push to a fresh, never-listed layer-1 mule;
        ₹15k–₹1.5L, log-uniform."""
        cfg = self.config.prize_advance_fee
        index = self.victims[int(rng.integers(len(self.victims)))]
        customer = self.pop.customers[index]
        at = self._timed(ScenarioType.PRIZE_ADVANCE_FEE, index, between(rng, self.start, self.end))
        unlisted = self._unlisted_mules()
        ring, mule = unlisted[int(rng.integers(len(unlisted)))] if unlisted else self._mule(rng)
        instance = self._instance(ScenarioType.PRIZE_ADVANCE_FEE, customer, ring, at)
        low, high = math.log(cfg.amount_inr.min), math.log(cfg.amount_inr.max)
        rupees = math.exp(low + (high - low) * rng.random())
        self.sessions.append(
            FraudSession(
                ScenarioType.PRIZE_ADVANCE_FEE,
                index,
                mule,
                at,
                cfg.channel,
                instance,
                coached=False,
                amount_paise=round(rupees) * _PAISE_PER_INR,
                max_balance_share=cfg.max_balance_share,
            )
        )
        return 1

    def background_crm(self, until: datetime) -> None:
        cfg = self.config.crm
        for i, customer in enumerate(self.pop.customers):
            rng = keyed_rng(self.seed, "crm", str(i))
            at = self.pop.history_start
            while True:
                at += timedelta(days=float(rng.exponential(cfg.background_mean_days)))
                if at >= until:
                    break
                self.crm.append(
                    CrmEvent(self.ids["crm_event"].next(), customer.id, choice(rng, cfg.types), at)
                )


def plan_fraud(
    config: GeneratorConfig,
    population: Population,
    network: MuleNetwork,
    *,
    genuine_sessions: int,
    coached_share: float,
    window: tuple[datetime, datetime] | None = None,
    crm_until: datetime | None = None,
    saved: frozenset[tuple[UUID, UUID]] = frozenset(),
    habits: Mapping[int, float] | None = None,
) -> FraudPlan:
    """The fraud victims, scaled to ``genuine_sessions`` (the genuine customer sessions made).
    ``coached_share`` is the response models' share of well-coached DA and INV victims
    (docs/14 §5.2).

    Without ``window`` this plans the history, plus the customers' background CRM events up to
    ``crm_until`` (the end of the window, so they run on through it; #55). With
    ``window`` (start, end) it plans the live/eval window instead, from ``"window"``-prefixed
    streams, and adds the window-only PRIZE_ADVANCE_FEE scenario."""
    span = window or (population.history_start, population.history_end)
    stream = "window" if window else "history"
    planner = _Planner(config, population, network, span, stream)
    planner.saved_before = saved  # (customer, account) pairs already saved as payees
    planner.habits = habits or {}
    if window is None:
        planner.background_crm(crm_until or span[1])
    scenarios = (
        (*_VICTIM_SCENARIOS, ScenarioType.PRIZE_ADVANCE_FEE) if window else _VICTIM_SCENARIOS
    )
    for scenario in scenarios:
        rate = (
            config.prize_advance_fee.rate_per_10k
            if scenario is ScenarioType.PRIZE_ADVANCE_FEE
            else config.fraud.rate_per_10k[scenario.value]
        )
        target = round(genuine_sessions * rate / config.fraud.genuine_rate_per_10k)
        names = ("fraud", scenario.value) if window is None else ("window", "fraud", scenario.value)
        rng = keyed_rng(planner.seed, *names)
        planned = 0
        while planned < target:
            if scenario is ScenarioType.ATO:
                planned += planner.ato(rng)
            elif scenario is ScenarioType.APP_DIGITAL_ARREST:
                planned += planner.digital_arrest(rng, coached_share)
            elif scenario is ScenarioType.APP_INVESTMENT:
                planned += planner.investment(rng, coached_share)
            else:
                planned += planner.prize_advance_fee(rng)
    return FraudPlan(
        sessions=tuple(planner.sessions),
        instances=tuple(planner.instances),
        devices=tuple(planner.devices),
        payees=tuple(planner.payees),
        crm_events=tuple(planner.crm),
    )
