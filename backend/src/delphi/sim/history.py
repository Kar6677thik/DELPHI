"""The history period's payments, decided by the legacy policy B1 (docs/14 §4, #17).

GENUINE_UNUSUAL events (a large one-off to a new merchant, or from a new phone; 30% at night)
run at each customer's routine rate × 150 / 9,700, genuine new phones appear once per 400 days,
and every customer session starts with a login. The fraud victims' sessions come from
`delphi.sim.fraud`, scaled to the genuine sessions a dry run of the history makes.

Each customer plans its GENUINE_ROUTINE payments from its own stream,
``keyed_rng(seed, "routine", i)``: a daily rate λ ~ Gamma(2, 0.3), Poisson counts per day,
Zipf-weighted payees, log-normal amounts per payee type at a habitual local hour, plus a monthly
salary credit. One chronological pass then applies B1 (which declines payments to a payee the
sim registry lists by then), keeps balances non-negative (a payment the payer cannot fund is
not made), writes the double-entry ledger (docs/05 §1), schedules each customer payment's
labels (docs/14 §7) and files the complaints the bank receives. A held or declined history
payment is never investigated, so it never gets a label.
"""

from __future__ import annotations

import bisect
import heapq
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import numpy as np

from delphi.kb.model import ClassifierThresholds
from delphi.sim.forwarding import Forwarder, Hop
from delphi.sim.fraud import FraudPlan, plan_fraud
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.ids import IdStream
from delphi.sim.labels import (
    LabelConfig,
    LabelSource,
    PaymentDecision,
    PaymentOutcome,
    ScheduledLabel,
    schedule_payment_labels,
    schedule_registry_label,
)
from delphi.sim.legacy_policy import decide_b1, payee_is_new
from delphi.sim.logins import LoginEvent, login_before
from delphi.sim.mules import MuleNetwork, RegistryListing
from delphi.sim.population import (
    ACTIVE,
    FULL_KYC,
    Account,
    Customer,
    Device,
    Payee,
    Population,
    _choice,
)
from delphi.sim.records import (
    Complaint,
    CrmEvent,
    ScenarioInstance,
    ScheduledTransfer,
    ScheduleTruth,
)
from delphi.sim.response_models import TelemetryTargets, draw_signals
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType, ground_truth_key
from delphi.sim.telemetry import SessionTelemetry, render_telemetry

IST = timezone(timedelta(hours=5, minutes=30))  # app chrome and habits are local (ADR-0014)
SIM_HISTORY = "SIM_HISTORY"
SIM_LIVE = "SIM_LIVE"
PENDING_GUARDIAN = "PENDING_GUARDIAN"
TRANSFER = "TRANSFER"
SALARY = "SALARY"
MULE_FORWARDING_REASON = "MULE_FORWARDING"
SALARY_CHANNEL = "NEFT"
_STATUS = {
    PaymentDecision.RELEASED: "EXECUTED",  # in the simulation RELEASED → EXECUTED is immediate
    PaymentDecision.HELD: "HELD",
    PaymentDecision.DECLINED: "DECLINED",
}
_PAISE_PER_INR = 100
_HOURS_PER_DAY = 24
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


@dataclass(frozen=True, slots=True)
class Payment:
    id: UUID
    payer_account_id: UUID
    payee_account_id: UUID
    amount_paise: int
    channel: str
    initiated_at: datetime
    device_id: UUID | None
    status: str
    origin: str
    executed_at: datetime | None
    scheduled_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    account_id: UUID
    payment_id: UUID
    delta_paise: int
    balance_after_paise: int
    at: datetime


@dataclass(frozen=True, slots=True)
class PaymentTruth:
    payment_id: UUID
    scenario_instance_id: UUID
    is_fraud: bool
    hypothesis_key: str
    coached: bool


@dataclass(frozen=True, slots=True)
class WindowStream:
    """The live/eval window's customer sessions (docs/14 §4, docs/22 §1): undecided
    (`PENDING_GUARDIAN`, origin `SIM_LIVE`), with no ledger and no labels, for each policy to
    decide on its own copy of the bank."""

    payments: tuple[Payment, ...]
    telemetry: tuple[SessionTelemetry, ...]
    logins: tuple[LoginEvent, ...]
    payment_truth: tuple[PaymentTruth, ...]


@dataclass(frozen=True, slots=True)
class History:
    payroll_accounts: tuple[Account, ...]
    payments: tuple[Payment, ...]
    ledger: tuple[LedgerEntry, ...]
    telemetry: tuple[SessionTelemetry, ...]
    scenario_instances: tuple[ScenarioInstance, ...]
    payment_truth: tuple[PaymentTruth, ...]
    labels: tuple[ScheduledLabel, ...]
    balances: Mapping[UUID, int]  # every account's balance at the end of the history
    devices: tuple[Device, ...] = ()  # phones first seen during the history
    payees: tuple[Payee, ...] = ()  # payees saved during the history
    logins: tuple[LoginEvent, ...] = ()
    crm_events: tuple[CrmEvent, ...] = ()
    complaints: tuple[Complaint, ...] = ()  # those reported before the history ends
    window: WindowStream | None = None  # the live/eval window's undecided sessions
    schedule: tuple[ScheduledTransfer, ...] = ()  # payments due after the history ends
    schedule_truth: tuple[ScheduleTruth, ...] = ()  # why each of them exists (sim_truth)
    cash_out_accounts: tuple[Account, ...] = ()  # one per bank, where cash-outs go
    registry_listings: tuple[RegistryListing, ...] = ()  # true listings of mule accounts
    account_labels: tuple[ScheduledLabel, ...] = ()  # their REGISTRY_LISTING labels


@dataclass(frozen=True, slots=True)
class _Intent:
    """A payment the customer would make, before B1 and the funds check."""

    id: UUID
    payer: UUID
    payee: UUID
    amount_paise: int
    channel: str
    at: datetime
    device: UUID | None
    instance: UUID
    decided: bool  # a customer payment B1 decides; a salary credit is not
    telemetry: SessionTelemetry | None
    scenario: ScenarioType = ScenarioType.GENUINE_ROUTINE
    login: LoginEvent | None = None
    is_fraud: bool = False
    coached: bool = False
    balance_share: float | None = None  # the amount is this share of the payer's balance
    max_balance_share: float | None = None  # the amount is at most this share of the balance
    fee_share: float | None = None  # a fee of this share of the instance's deposits made
    flow: int | None = None  # the mule-ring flow a forwarding payment belongs to

    def settle(self, balance: int, deposited: int = 0) -> int:
        """The amount actually sent, given the payer's balance at the time and, for a fee,
        what the instance has deposited so far."""
        if self.balance_share is not None:
            return int(balance * self.balance_share) // _PAISE_PER_INR * _PAISE_PER_INR
        amount = self.amount_paise
        if self.fee_share is not None:
            amount = round(deposited * self.fee_share / _PAISE_PER_INR) * _PAISE_PER_INR
        if self.max_balance_share is not None:
            cap = int(balance * self.max_balance_share) // _PAISE_PER_INR * _PAISE_PER_INR
            return min(amount, cap)
        return amount


def _lognormal_paise(rng: np.random.Generator, median_inr: float, sigma: float) -> int:
    rupees = median_inr * math.exp(sigma * rng.standard_normal())
    return max(1, round(rupees)) * _PAISE_PER_INR


def _payee_type(account: Account) -> str:
    if not account.is_merchant:
        return "FAMILY"
    return "UTILITY" if account.merchant_category == "UTILITY" else "MERCHANT"


class _Planner:
    def __init__(
        self,
        config: GeneratorConfig,
        population: Population,
        targets: Mapping[ScenarioType, TelemetryTargets],
        thresholds: ClassifierThresholds,
    ) -> None:
        self.cfg = config
        self.routine = config.genuine_routine
        self.pop = population
        # Customers plan through the live/eval window too; the window's sessions are not
        # decided here (docs/22 §1).
        window_days = config.profiles[population.profile].window_days or 0
        self.horizon = population.history_end + timedelta(days=window_days)
        self.seed = config.profiles[population.profile].seed
        self.targets = targets
        self.thresholds = thresholds
        self.payment_ids = IdStream(self.seed, "payment")
        self.instance_ids = IdStream(self.seed, "scenario_instance")
        self.accounts = {a.id: a for a in population.accounts}
        self.home_account = {a.customer_id: a for a in population.accounts if a.customer_id}
        self.payees: dict[UUID, list[UUID]] = {}
        for payee in population.payees:
            self.payees.setdefault(payee.customer_id, []).append(payee.account_id)
        self.devices: dict[UUID, list[Device]] = {}
        for device in population.devices:
            self.devices.setdefault(device.customer_id, []).append(device)
        self.new_device_ids = IdStream(self.seed, "history_device")
        self.new_payee_ids = IdStream(self.seed, "history_payee")
        self.login_ids = IdStream(self.seed, "login")
        self.new_devices: list[Device] = []
        self.new_payees: list[Payee] = []
        # Merchants each customer saves during the history (a payee is saved only once).
        self.saved_during: defaultdict[UUID, list[UUID]] = defaultdict(list)
        # People a customer can add as a new payee (#56): other customers' home-bank accounts
        # and the external person accounts.
        self.home_accounts = [self.home_account[c.id].id for c in population.customers]
        self.external_people = [
            a.id for a in population.accounts if a.customer_id is None and not a.is_merchant
        ]
        self.payee_count = Counter(p.customer_id for p in population.payees)
        self.phones: dict[UUID, list[Device]] = {}  # each customer's genuine new phones
        self.mule_customer: dict[UUID, Customer] = {}  # home-bank mule account → customer
        self.mule_device: dict[UUID, Device] = {}
        self.flow_streams: dict[int, np.random.Generator] = {}
        self.mule_payee_ids = IdStream(self.seed, "mule_payee")
        self.mule_payee_count: Counter[UUID] = Counter()
        self.unusual_merchants = [
            a.id
            for a in population.accounts
            if a.is_merchant and a.merchant_category in config.genuine_unusual.merchant_categories
        ]
        payroll_ids = IdStream(self.seed, "payroll_account")
        banks = config.population.external_banks
        self.payroll = tuple(
            Account(
                id=payroll_ids.next(),
                customer_id=None,
                bank_code=bank,
                is_merchant=False,
                merchant_category=None,
                opened_at=population.history_start
                - timedelta(days=self.routine.payroll_account_age_days),
                kyc_level=FULL_KYC,
                status=ACTIVE,
                balance_paise=self.routine.payroll_opening_balance_inr * _PAISE_PER_INR,
                phone_number=None,
                vpa=f"payroll@{bank.lower()}",
            )
            for bank in banks
        )

    def _local_days(self) -> list[datetime]:
        """Local (IST) midnights of every history and window day."""
        first = self.pop.history_start.astimezone(IST).replace(hour=0, minute=0, second=0)
        days = []
        day = first
        while day < self.horizon:
            days.append(day)
            day += timedelta(days=1)
        return days

    def _new_device(self, rng: np.random.Generator, customer_id: UUID, at: datetime) -> Device:
        device = Device(
            id=self.new_device_ids.next(),
            customer_id=customer_id,
            fingerprint=rng.bytes(16).hex(),
            os=_choice(rng, self.cfg.population.device_os),
            first_seen_at=at,
        )
        self.new_devices.append(device)
        return device

    @staticmethod
    def _device_at(
        rng: np.random.Generator, original: list[Device], phones: list[Device], at: datetime
    ) -> Device:
        """The newest phone first seen by `at`, else one of the customer's original devices."""
        current = [d for d in phones if d.first_seen_at <= at]
        if current:
            return max(current, key=lambda d: d.first_seen_at)
        return original[int(rng.integers(len(original)))]

    def _session(
        self,
        rng: np.random.Generator,
        scenario: ScenarioType,
        *,
        customer: Customer,
        payer: UUID,
        payee: UUID,
        amount_paise: int,
        channel: str,
        at: datetime,
        device: Device,
        instance: UUID,
        city: str | None = None,
    ) -> _Intent:
        payment_id = self.payment_ids.next()
        signals = draw_signals(rng, self.targets[scenario])
        telemetry = render_telemetry(rng, payment_id, signals, self.cfg.telemetry, self.thresholds)
        city = city or customer.home_city
        login = login_before(
            rng,
            login_id=self.login_ids.next(),
            customer_id=customer.id,
            device_id=device.id,
            fingerprint=device.fingerprint,
            city_name=city,
            city=self.cfg.cities[city],
            session_at=at,
            lead_s=self.cfg.login_lead_s,
            not_before=self.pop.history_start,
        )
        return _Intent(
            id=payment_id,
            payer=payer,
            payee=payee,
            amount_paise=amount_paise,
            channel=channel,
            at=at,
            device=device.id,
            instance=instance,
            decided=True,
            telemetry=telemetry,
            scenario=scenario,
            login=login,
        )

    def _unusual(
        self,
        rng: np.random.Generator,
        *,
        index: int,
        day: datetime,
        habit: float,
        original: list[Device],
        phones: list[Device],
        intents: list[_Intent],
        instances: list[ScenarioInstance],
    ) -> None:
        cfg = self.cfg.genuine_unusual
        customer = self.pop.customers[index]
        account = self.home_account[customer.id]
        scale = self.cfg.population.income_amount_scale[customer.income_band]
        if rng.random() < cfg.night_share:
            hour = cfg.night_hours.min + rng.random() * (cfg.night_hours.max - cfg.night_hours.min)
        else:
            hour = habit + self.routine.hour_sd * rng.standard_normal()
        at = day + timedelta(seconds=int(hour % _HOURS_PER_DAY * _SECONDS_PER_HOUR))
        lead = timedelta(
            minutes=cfg.lead_minutes.min
            + rng.random() * (cfg.lead_minutes.max - cfg.lead_minutes.min)
        )
        kind = _choice(rng, cfg.kind)
        start = self.pop.history_start
        if not start <= at < self.horizon:
            return  # outside the history and window: no payment, so no payee or phone either
        saved = self.payees.get(customer.id, [])
        taken = {*saved, *self.saved_during[customer.id]}
        options = [m for m in self.unusual_merchants if m not in taken]
        if kind == "NEW_MERCHANT" and not options:
            kind = "NEW_PHONE"  # every unusual merchant is saved already
        if kind == "NEW_MERCHANT":
            payee = options[int(rng.integers(len(options)))]
            self.saved_during[customer.id].append(payee)
            self.new_payees.append(
                Payee(
                    id=self.new_payee_ids.next(),
                    customer_id=customer.id,
                    account_id=payee,
                    label=(self.accounts[payee].merchant_category or "").title().replace("_", " "),
                    added_at=max(at - lead, start),
                )
            )
            device = self._device_at(rng, original, phones, at)
        else:  # NEW_PHONE: a large payment to family from a phone first seen minutes ago
            family = [a for a in saved if not self.accounts[a].is_merchant] or saved
            payee = family[int(rng.integers(len(family)))]
            device = self._new_device(rng, customer.id, max(at - lead, start))
            phones.append(device)
        instance = ScenarioInstance(
            id=self.instance_ids.next(),
            scenario_type=ScenarioType.GENUINE_UNUSUAL.value,
            hypothesis_key=ground_truth_key(ScenarioType.GENUINE_UNUSUAL),
            params={"customer_id": str(customer.id), "kind": kind},
            started_at=at - lead,
        )
        instances.append(instance)
        session = self._session(
            rng,
            ScenarioType.GENUINE_UNUSUAL,
            customer=customer,
            payer=account.id,
            payee=payee,
            amount_paise=_lognormal_paise(rng, cfg.amount_inr.median * scale, cfg.amount_inr.sigma),
            channel=cfg.channel,
            at=at,
            device=device,
            instance=instance.id,
        )
        # A one-off takes at most this share of the balance at the time (#55): the customer
        # pays what they can, so the payment is made in the window as in the history.
        intents.append(replace(session, max_balance_share=cfg.max_balance_share))

    def forward(self, hop: Hop, network: MuleNetwork, instance: UUID) -> _Intent:
        """A forwarding payment: a home-bank mule's app session (MULE_FORWARDING, decided by
        B1, from the flow's own stream), else a payment B1 never sees."""
        if not hop.session:
            return _Intent(
                id=self.payment_ids.next(),
                payer=hop.payer,
                payee=hop.payee,
                amount_paise=hop.amount_paise,
                channel=hop.channel,
                at=hop.at,
                device=None,
                instance=instance,
                decided=False,
                telemetry=None,
                scenario=ScenarioType.MULE_FORWARDING,
                is_fraud=True,
                flow=hop.flow,
            )
        if not self.mule_customer:
            self.forward_customers(network)
        customer = self.mule_customer[hop.payer]
        if hop.flow not in self.flow_streams:
            self.flow_streams[hop.flow] = keyed_rng(self.seed, "flow_session", str(hop.flow))
        rng = self.flow_streams[hop.flow]
        intent = self._session(
            rng,
            ScenarioType.MULE_FORWARDING,
            customer=customer,
            payer=hop.payer,
            payee=hop.payee,
            amount_paise=hop.amount_paise,
            channel=hop.channel,
            at=hop.at,
            device=self.mule_device[customer.id],
            instance=instance,
        )
        return replace(intent, is_fraud=True, flow=hop.flow)

    def mule_payee(self, hop: Hop, network: MuleNetwork) -> Payee:
        """A home-bank mule saves its next-layer account as a payee before first paying it,
        labelled like any family payee, from ``keyed_rng(seed, "mule_payee", payer/payee)``."""
        if not self.mule_customer:
            self.forward_customers(network)
        customer = self.mule_customer[hop.payer]
        rng = keyed_rng(self.seed, "mule_payee", f"{hop.payer}/{hop.payee}")
        lead = self.cfg.forwarding.payee_lead_minutes
        minutes = lead.min + (lead.max - lead.min) * rng.random()
        added = max(
            hop.at - timedelta(seconds=int(minutes * _SECONDS_PER_MINUTE)), self.pop.history_start
        )
        self.mule_payee_count[customer.id] += 1
        payee = Payee(
            id=self.mule_payee_ids.next(),
            customer_id=customer.id,
            account_id=hop.payee,
            label=f"Family {self.mule_payee_count[customer.id]}",
            added_at=added,
        )
        self.new_payees.append(payee)
        return payee

    def forward_customers(self, network: MuleNetwork) -> None:
        customers = {c.id: c for c in network.customers}
        self.mule_customer = {
            a.id: customers[a.customer_id] for a in network.accounts if a.customer_id
        }
        self.mule_device = {d.customer_id: d for d in network.devices}

    def fraud(self, plan: FraudPlan) -> list[_Intent]:
        """The victims' payments as customer sessions, each instance from its own stream."""
        intents = []
        streams: dict[UUID, np.random.Generator] = {}
        for session in plan.sessions:
            customer = self.pop.customers[session.customer_index]
            rng = streams.setdefault(
                session.instance, keyed_rng(self.seed, "fraud_session", str(session.instance))
            )
            device = session.device or self._device_at(
                rng, self.devices[customer.id], self.phones[customer.id], session.at
            )
            intent = self._session(
                rng,
                session.scenario,
                customer=customer,
                payer=self.home_account[customer.id].id,
                payee=session.payee,
                amount_paise=session.amount_paise,
                channel=session.channel,
                at=session.at,
                device=device,
                instance=session.instance,
                city=session.city,
            )
            intents.append(
                replace(
                    intent,
                    is_fraud=True,
                    coached=session.coached,
                    balance_share=session.balance_share,
                    max_balance_share=session.max_balance_share,
                    fee_share=session.fee_share,
                )
            )
        return intents

    def customer(
        self, index: int, intents: list[_Intent], instances: list[ScenarioInstance]
    ) -> None:
        customer = self.pop.customers[index]
        account = self.home_account[customer.id]
        rng = keyed_rng(self.seed, "routine", str(index))
        routine = self.routine
        scale = self.cfg.population.income_amount_scale[customer.income_band]
        instance = ScenarioInstance(
            id=self.instance_ids.next(),
            scenario_type=ScenarioType.GENUINE_ROUTINE.value,
            hypothesis_key=ground_truth_key(ScenarioType.GENUINE_ROUTINE),
            params={"customer_id": str(customer.id)},
            started_at=self.pop.history_start,
        )
        instances.append(instance)
        rate = rng.gamma(routine.daily_rate_gamma["shape"], routine.daily_rate_gamma["scale"])
        unusual = self.cfg.genuine_unusual
        unusual_rate = (
            rate * unusual.rate_per_10k / unusual.routine_rate_per_10k * unusual.made_share
        )
        habit = routine.habitual_hour.min + rng.random() * (
            routine.habitual_hour.max - routine.habitual_hour.min
        )
        payees = self.payees.get(customer.id, [])
        weights = np.asarray(
            [1 / (rank + 1) ** routine.zipf_exponent for rank in range(len(payees))]
        )
        cdf = list(np.cumsum(weights) / weights.sum()) if payees else []
        original = self.devices[customer.id]
        # docs/14 §3: genuine new phones, once per new_device_mean_days on average.
        phones: list[Device] = []
        at = self.pop.history_start
        while True:
            at += timedelta(days=float(rng.exponential(self.cfg.new_device_mean_days)))
            if at >= self.horizon:
                break
            phones.append(self._new_device(rng, customer.id, at))
        self.phones[customer.id] = phones
        employer = self.payroll[int(rng.integers(len(self.payroll)))]
        for day in self._local_days():
            if day.day == 1:
                hour = routine.salary_hour.min + rng.random() * (
                    routine.salary_hour.max - routine.salary_hour.min
                )
                intents.append(
                    _Intent(
                        id=self.payment_ids.next(),
                        payer=employer.id,
                        payee=account.id,
                        amount_paise=_lognormal_paise(
                            rng, routine.salary_inr.median * scale, routine.salary_inr.sigma
                        ),
                        channel=SALARY_CHANNEL,
                        at=day + timedelta(seconds=int(hour * _SECONDS_PER_HOUR)),
                        device=None,
                        instance=instance.id,
                        decided=False,
                        telemetry=None,
                    )
                )
            for _ in range(int(rng.poisson(rate)) if payees else 0):
                payee = payees[min(bisect.bisect_right(cdf, rng.random()), len(payees) - 1)]
                payee_type = _payee_type(self.accounts[payee])
                amount = routine.amount_inr[payee_type]
                channel = _choice(rng, routine.channel[payee_type])
                hour = (habit + routine.hour_sd * rng.standard_normal()) % _HOURS_PER_DAY
                at = day + timedelta(seconds=int(hour * _SECONDS_PER_HOUR))
                intents.append(
                    self._session(
                        rng,
                        ScenarioType.GENUINE_ROUTINE,
                        customer=customer,
                        payer=account.id,
                        payee=payee,
                        amount_paise=_lognormal_paise(rng, amount.median * scale, amount.sigma),
                        channel=channel,
                        at=at,
                        device=self._device_at(rng, original, phones, at),
                        instance=instance.id,
                    )
                )
            for _ in range(int(rng.poisson(unusual_rate)) if payees else 0):
                self._unusual(
                    rng,
                    index=index,
                    day=day,
                    habit=habit,
                    original=original,
                    phones=phones,
                    intents=intents,
                    instances=instances,
                )
        self._new_people(
            index, habit=habit, devices=(original, phones), intents=intents, instance=instance.id
        )

    def _new_people(
        self,
        index: int,
        *,
        habit: float,
        devices: tuple[list[Device], list[Device]],
        intents: list[_Intent],
        instance: UUID,
    ) -> None:
        """A genuine customer saves a new person (family or friend) as a payee now and then,
        minutes before first paying them, and pays them again later (#56). Its own stream,
        ``keyed_rng(seed, "person_payee", i)``, so the routine draws do not move."""
        cfg = self.routine.new_person_payee
        customer = self.pop.customers[index]
        account = self.home_account[customer.id]
        rng = keyed_rng(self.seed, "person_payee", str(index))
        scale = self.cfg.population.income_amount_scale[customer.income_band]
        amount = self.routine.amount_inr["FAMILY"]
        original, phones = devices
        at = self.pop.history_start
        while True:
            at += timedelta(days=float(rng.exponential(cfg.mean_days)))
            if at >= self.horizon:
                return
            taken = {*self.payees.get(customer.id, []), *self.saved_during[customer.id]}
            home = rng.random() < self.cfg.population.counterparty_home_bank_share
            pool = self.home_accounts if home else self.external_people
            payee = pool[int(rng.integers(len(pool)))]
            if payee in taken or payee == account.id:
                continue  # already a payee, or their own account: nothing new to save
            self.saved_during[customer.id].append(payee)
            self.payee_count[customer.id] += 1
            span = cfg.lead_minutes.max - cfg.lead_minutes.min
            lead = timedelta(minutes=cfg.lead_minutes.min + rng.random() * span)
            day = at.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
            hour = (habit + self.routine.hour_sd * rng.standard_normal()) % _HOURS_PER_DAY
            first = day + timedelta(seconds=int(hour * _SECONDS_PER_HOUR))
            self.new_payees.append(
                Payee(
                    id=self.new_payee_ids.next(),
                    customer_id=customer.id,
                    account_id=payee,
                    label=f"Family {self.payee_count[customer.id]}",
                    added_at=max(first - lead, self.pop.history_start),
                )
            )
            more = cfg.follow_up_payments
            follow_ups = int(rng.integers(int(more.min), int(more.max) + 1))
            later = [rng.random() * cfg.follow_up_days for _ in range(follow_ups)]
            for when in sorted([first, *(first + timedelta(days=float(d)) for d in later)]):
                if not self.pop.history_start <= when < self.horizon:
                    continue
                intents.append(
                    self._session(
                        rng,
                        ScenarioType.GENUINE_ROUTINE,
                        customer=customer,
                        payer=account.id,
                        payee=payee,
                        amount_paise=_lognormal_paise(rng, amount.median * scale, amount.sigma),
                        channel=_choice(rng, self.routine.channel["FAMILY"]),
                        at=when,
                        device=self._device_at(rng, original, phones, when),
                        instance=instance,
                    )
                )


def generate_history(
    config: GeneratorConfig,
    population: Population,
    *,
    targets: Mapping[ScenarioType, TelemetryTargets],
    thresholds: ClassifierThresholds,
    label_config: LabelConfig,
    network: MuleNetwork,
    coached_share: float,
) -> History:
    """The history's genuine and fraud payments and salaries, decided by B1.

    ``targets`` are the scenarios' telemetry targets (docs/14 §5.1) and ``thresholds`` the KB's
    classifier thresholds (docs/08 §5). ``network`` holds the mule accounts, whose balances the
    history carries, and the registry listings B1 declines payments to. ``coached_share`` is
    the share of well-coached DA and INV victims (docs/14 §5.2)."""
    planner = _Planner(config, population, targets, thresholds)
    genuine: list[_Intent] = []
    instances: list[ScenarioInstance] = []
    for index in range(len(population.customers)):
        planner.customer(index, genuine, instances)
    inside = population.history_start, population.history_end
    in_window = [i for i in genuine if population.history_end <= i.at < planner.horizon]
    genuine = sorted(
        (i for i in genuine if inside[0] <= i.at < inside[1]), key=lambda i: (i.at, str(i.id))
    )
    # The fraud rates are per 10,000 payments made: a dry run of the genuine payments counts
    # the sessions that happen (some are never made, for want of funds).
    dry = _Bank(planner, network)
    made = sum(dry.process(i) is not None and i.decided for i in genuine)
    # Background CRM events run on through the window, so a window SIM-swap report is not
    # an ATO tell (#55); the history's events are the same either way.
    plan = plan_fraud(
        config,
        population,
        network,
        genuine_sessions=made,
        coached_share=coached_share,
        crm_until=planner.horizon,
    )
    instances.extend(plan.instances)
    planner.new_devices.extend(plan.devices)
    planner.new_payees.extend(plan.payees)
    fraud = [i for i in planner.fraud(plan) if inside[0] <= i.at < inside[1]]
    # One MULE_FORWARDING instance per ring holds the ring's onward payments.
    rings = {}
    for ring in network.rings:
        instance = ScenarioInstance(
            id=planner.instance_ids.next(),
            scenario_type=ScenarioType.MULE_FORWARDING.value,
            hypothesis_key=ground_truth_key(ScenarioType.MULE_FORWARDING),
            params={"ring_id": str(ring.id)},
            started_at=population.history_start,
        )
        instances.append(instance)
        rings[ring.id] = instance.id
    context = _RunContext(
        network,
        instances,
        plan,
        label_config,
        Forwarder(config, network, seed=planner.seed, start=population.history_start),
        rings,
    )
    history = _run(planner, [*genuine, *fraud], context=context)
    # The window comes after the history pass: its payments settle against the balances the
    # history ends with.
    window = _plan_window(planner, network, in_window, history.balances, coached_share)
    stream, salaries = _window_stream(planner, window.intents, history.balances)
    extra = window.plan
    return replace(
        history,
        window=stream,
        schedule=(*history.schedule, *(row for row, _ in salaries)),
        schedule_truth=(*history.schedule_truth, *(truth for _, truth in salaries)),
        scenario_instances=(*history.scenario_instances, *(extra.instances if extra else ())),
        devices=(*history.devices, *(extra.devices if extra else ())),
        payees=(*history.payees, *(extra.payees if extra else ())),
        crm_events=(*history.crm_events, *(extra.crm_events if extra else ())),
    )


class _WindowBank:
    """Running balances through the window. Window payments are undecided (docs/22 §1), so
    each one settles as if made: against the balance at that moment, which starts at the
    end of the history and moves with the window's salaries and earlier window payments. A
    payment the payer cannot fund is not made, as in the history (#55)."""

    def __init__(self, balances: Mapping[UUID, int]) -> None:
        self.balances = dict(balances)
        self.deposited: dict[UUID, int] = {}  # each investment scam's deposits so far

    def process(self, intent: _Intent) -> int | None:
        """The amount the payment moves, or None if it is not made."""
        balance = self.balances[intent.payer]
        amount = (
            intent.settle(balance, self.deposited.get(intent.instance, 0))
            if intent.decided
            else intent.amount_paise
        )
        if amount <= 0 or amount > balance:
            return None
        if intent.scenario is ScenarioType.APP_INVESTMENT and intent.fee_share is None:
            self.deposited[intent.instance] = self.deposited.get(intent.instance, 0) + amount
        self.balances[intent.payer] -= amount
        self.balances[intent.payee] += amount
        return amount


@dataclass(frozen=True, slots=True)
class _WindowPlan:
    intents: list[_Intent]
    plan: FraudPlan | None


def _plan_window(
    planner: _Planner,
    network: MuleNetwork,
    genuine: list[_Intent],
    balances: Mapping[UUID, int],
    coached_share: float,
) -> _WindowPlan:
    """The window's fraud victims (and UPI_COLLECT_REFUND), at their rates against the
    window's genuine sessions made: a dry run settles them against running balances, as the
    history's dry run does."""
    pop = planner.pop
    if planner.horizon <= pop.history_end:
        return _WindowPlan([], None)
    dry = _WindowBank(balances)
    made = sum(dry.process(i) is not None and i.decided for i in sorted(genuine, key=_intent_order))
    saved = frozenset((p.customer_id, p.account_id) for p in (*pop.payees, *planner.new_payees))
    plan = plan_fraud(
        planner.cfg,
        pop,
        network,
        genuine_sessions=made,
        coached_share=coached_share,
        window=(pop.history_end, planner.horizon),
        saved=saved,
    )
    fraud = [i for i in planner.fraud(plan) if i.at < planner.horizon]
    return _WindowPlan([*genuine, *fraud], plan)


def _intent_order(intent: _Intent) -> tuple[datetime, str]:
    return intent.at, str(intent.id)


def _window_stream(
    planner: _Planner, intents: list[_Intent], balances: Mapping[UUID, int]
) -> tuple[WindowStream, list[tuple[ScheduledTransfer, ScheduleTruth]]]:
    """The window's sessions as undecided payments, and its salaries as scheduled transfers.
    Each payment settles against the payer's running balance in the window (`_WindowBank`)."""
    ids = IdStream(planner.seed, "window_schedule")
    bank = _WindowBank(balances)
    payments: list[Payment] = []
    telemetry: list[SessionTelemetry] = []
    logins: list[LoginEvent] = []
    truth: list[PaymentTruth] = []
    salaries: list[tuple[ScheduledTransfer, ScheduleTruth]] = []
    for intent in sorted(intents, key=_intent_order):
        amount = bank.process(intent)
        if amount is None:
            continue
        if not intent.decided:
            row = _transfer(
                ids,
                intent.at,
                payer=intent.payer,
                payee=intent.payee,
                amount_paise=amount,
                channel=intent.channel,
            )
            salaries.append((row, ScheduleTruth(row.id, SALARY, flow=None, session=False)))
            continue
        payments.append(
            Payment(
                id=intent.id,
                payer_account_id=intent.payer,
                payee_account_id=intent.payee,
                amount_paise=amount,
                channel=intent.channel,
                initiated_at=intent.at,
                device_id=intent.device,
                status=PENDING_GUARDIAN,
                origin=SIM_LIVE,
                executed_at=None,
            )
        )
        if intent.telemetry is not None:
            telemetry.append(intent.telemetry)
        if intent.login is not None:
            logins.append(intent.login)
        truth.append(
            PaymentTruth(
                payment_id=intent.id,
                scenario_instance_id=intent.instance,
                is_fraud=intent.is_fraud,
                hypothesis_key=ground_truth_key(intent.scenario),
                coached=intent.coached,
            )
        )
    stream = WindowStream(tuple(payments), tuple(telemetry), tuple(logins), tuple(truth))
    return stream, salaries


def _in_flight(ids: IdStream, hop: Hop) -> tuple[ScheduledTransfer, ScheduleTruth]:
    """A mule-ring payment due after the history (docs/14 §8: SIM_TICK advances it)."""
    row = _transfer(
        ids,
        hop.at,
        payer=hop.payer,
        payee=hop.payee,
        amount_paise=hop.amount_paise,
        channel=hop.channel,
    )
    return row, ScheduleTruth(row.id, MULE_FORWARDING_REASON, flow=hop.flow, session=hop.session)


def _transfer(  # the bank row says only what making the payment needs (#56)
    ids: IdStream, at: datetime, *, payer: UUID, payee: UUID, amount_paise: int, channel: str
) -> ScheduledTransfer:
    payload = {
        "payer_account_id": str(payer),
        "payee_account_id": str(payee),
        "amount_paise": amount_paise,
        "channel": channel,
    }
    return ScheduledTransfer(ids.next(), at, TRANSFER, payload)


@dataclass(frozen=True, slots=True)
class _RunContext:
    network: MuleNetwork
    instances: list[ScenarioInstance]
    plan: FraudPlan
    label_config: LabelConfig
    forwarder: Forwarder
    ring_instances: Mapping[UUID, UUID]


class _Bank:
    """The bank's state through the pass: balances, B1's view of payees and devices."""

    def __init__(
        self, planner: _Planner, network: MuleNetwork, extra: tuple[Account, ...] = ()
    ) -> None:
        pop = planner.pop
        self.policy = planner.cfg.legacy_policy
        self.history_start = pop.history_start
        self.balances = {
            a.id: a.balance_paise
            for a in (*pop.accounts, *network.accounts, *planner.payroll, *extra)
        }
        self.listed_at = {listing.account_id: listing.listed_at for listing in network.listings}
        home = planner.home_account
        self.saved_at = {
            (home[p.customer_id].id, p.account_id): p.added_at
            for p in (*pop.payees, *planner.new_payees)
        }
        self.first_seen = {
            d.id: d.first_seen_at for d in (*pop.devices, *network.devices, *planner.new_devices)
        }
        self.new_device = timedelta(days=planner.thresholds.new_device_days)
        self.paid: set[tuple[UUID, UUID]] = set()
        self.deposited: dict[UUID, int] = {}  # each investment scam's deposits made so far

    def _decide(self, intent: _Intent) -> PaymentDecision:
        if not intent.decided:
            return PaymentDecision.RELEASED
        pair = (intent.payer, intent.payee)
        payee_new = payee_is_new(
            self.policy, added_at=self.saved_at.get(pair), at=intent.at, ever_paid=pair in self.paid
        )
        device_is_new = (
            intent.device is not None
            and intent.at - self.first_seen[intent.device] < self.new_device
        )
        listed = self.listed_at.get(intent.payee)
        return decide_b1(
            self.policy,
            amount_paise=intent.amount_paise,
            channel=intent.channel,
            payee_listed=listed is not None and listed <= intent.at,
            payee_is_new=payee_new,
            device_is_new=device_is_new,
        )

    def process(self, planned: _Intent) -> tuple[_Intent, PaymentDecision] | None:
        """B1's decision on the payment as made, or None if it is never made. A released
        payment is executed at once."""
        amount = planned.settle(
            self.balances[planned.payer], self.deposited.get(planned.instance, 0)
        )
        if amount <= 0:
            return None  # nothing left to take
        intent = (
            replace(planned, amount_paise=amount) if amount != planned.amount_paise else planned
        )
        decision = self._decide(intent)
        if decision is PaymentDecision.RELEASED:
            if self.balances[intent.payer] < amount:
                return None  # the payer cannot fund it, so the payment is never made
            self.paid.add((intent.payer, intent.payee))
            if intent.scenario is ScenarioType.APP_INVESTMENT and intent.fee_share is None:
                self.deposited[intent.instance] = self.deposited.get(intent.instance, 0) + amount
            self.balances[intent.payer] -= amount
            self.balances[intent.payee] += amount
        return intent, decision


def _complaint(
    planner: _Planner, label: ScheduledLabel, intent: _Intent, victim: UUID, ids: IdStream
) -> Complaint:
    cfg = planner.cfg.fraud
    rng = keyed_rng(planner.seed, "complaint", str(intent.id))
    return Complaint(
        id=ids.next(),
        reported_at=label.due_at,
        victim_customer_id=victim,
        channel=_choice(rng, cfg.complaint_channel),
        category=cfg.complaint_category[intent.scenario.value],
        payment_ids=(intent.id,),
        counterparty_account_ids=(intent.payee,),
    )


def _labels(
    planner: _Planner, intent: _Intent, decision: PaymentDecision, config: LabelConfig
) -> list[ScheduledLabel]:
    spec = planner.cfg.profiles[planner.pop.profile]
    outcome = PaymentOutcome(str(intent.id), intent.scenario, decision, intent.at)
    return schedule_payment_labels(
        outcome, config, seed=spec.seed, maturity_days=spec.maturity_days
    )


class _Registry:
    """The sim registry's true listings: 40% of mule accounts, after their first fraud credit
    (docs/14 §4, §7). Whether an account is listed comes from its own stream."""

    def __init__(self, planner: _Planner, context: _RunContext, bank: _Bank) -> None:
        self.share = planner.cfg.forwarding.registry_listing_share
        self.seed = planner.seed
        self.label_config = context.label_config
        self.bank = bank
        self.credited: set[UUID] = set()
        self.listings: list[RegistryListing] = []
        self.labels: list[ScheduledLabel] = []

    def credit(self, account: UUID, at: datetime) -> None:
        if account in self.credited:
            return
        self.credited.add(account)
        if keyed_rng(self.seed, "mule_listing", str(account)).random() >= self.share:
            return
        label = schedule_registry_label(str(account), at, self.label_config, seed=self.seed)
        self.labels.append(label)
        self.listings.append(RegistryListing(account, label.due_at))
        self.bank.listed_at[account] = label.due_at


def _save_mule_payee(planner: _Planner, bank: _Bank, hop: Hop, network: MuleNetwork) -> None:
    """A home-bank mule saves its next-layer account before it first pays it."""
    if hop.session and (hop.payer, hop.payee) not in bank.saved_at:
        payee = planner.mule_payee(hop, network)
        bank.saved_at[hop.payer, hop.payee] = payee.added_at


@dataclass(slots=True)
class _Rows:
    """The rows the pass writes, payment by payment."""

    payments: list[Payment] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    telemetry: list[SessionTelemetry] = field(default_factory=list)
    truth: list[PaymentTruth] = field(default_factory=list)
    logins: list[LoginEvent] = field(default_factory=list)

    def add(self, intent: _Intent, decision: PaymentDecision, balances: Mapping[UUID, int]) -> None:
        executed = decision is PaymentDecision.RELEASED
        self.payments.append(
            Payment(
                id=intent.id,
                payer_account_id=intent.payer,
                payee_account_id=intent.payee,
                amount_paise=intent.amount_paise,
                channel=intent.channel,
                initiated_at=intent.at,
                device_id=intent.device,
                status=_STATUS[decision],
                origin=SIM_HISTORY,
                executed_at=intent.at if executed else None,
            )
        )
        if intent.login is not None:
            self.logins.append(intent.login)
        if executed:
            self.ledger.extend(
                LedgerEntry(account, intent.id, delta, balances[account], intent.at)
                for account, delta in (
                    (intent.payer, -intent.amount_paise),
                    (intent.payee, intent.amount_paise),
                )
            )
        if intent.telemetry is not None:
            self.telemetry.append(intent.telemetry)
        self.truth.append(
            PaymentTruth(
                payment_id=intent.id,
                scenario_instance_id=intent.instance,
                is_fraud=intent.is_fraud,
                hypothesis_key=ground_truth_key(intent.scenario),
                coached=intent.coached,
            )
        )


def _run(planner: _Planner, intents: list[_Intent], *, context: _RunContext) -> History:
    """One chronological pass: B1, the funds check, the ledger, truth, labels, complaints, and
    the onward transfers through the mule rings as their credits arrive."""
    population = planner.pop
    forwarder = context.forwarder
    cash_accounts = tuple(forwarder.cash_accounts.values())
    bank = _Bank(planner, context.network, cash_accounts)
    registry = _Registry(planner, context, bank)
    ring_of = {a: ring.id for a, (ring, _) in forwarder.layer_of.items()}
    complaint_ids = IdStream(planner.seed, "complaint")
    victim_of = {
        a.id: a.customer_id
        for a in (*population.accounts, *context.network.accounts)
        if a.customer_id
    }
    rows = _Rows()
    labels: list[ScheduledLabel] = []
    complaints: list[Complaint] = []
    schedule_ids = IdStream(planner.seed, "sim_schedule")
    in_flight: list[tuple[ScheduledTransfer, ScheduleTruth]] = []
    queue = [(i.at, str(i.id), i) for i in intents]
    heapq.heapify(queue)

    def schedule(hops: list[Hop]) -> None:
        for hop in hops:
            if hop.at >= population.history_end:
                in_flight.append(_in_flight(schedule_ids, hop))  # carried on after the history
                continue
            _save_mule_payee(planner, bank, hop, context.network)
            intent = planner.forward(
                hop, context.network, context.ring_instances[ring_of[hop.payer]]
            )
            heapq.heappush(queue, (intent.at, str(intent.id), intent))

    while queue:
        _, _, planned = heapq.heappop(queue)
        made = bank.process(planned)
        executed = made is not None and made[1] is PaymentDecision.RELEASED
        if planned.flow is not None:
            amount = made[0].amount_paise if made is not None and executed else 0
            schedule(forwarder.resolve(planned.flow, amount, planned.at))
        if made is None:
            continue
        intent, decision = made
        if executed and intent.is_fraud and forwarder.is_mule(intent.payee):
            registry.credit(intent.payee, intent.at)
            if intent.flow is None:  # a victim's payment starts a flow
                schedule(forwarder.arrive(intent.payee, intent.amount_paise, intent.at))
        rows.add(intent, decision, bank.balances)
        if intent.decided:
            scheduled = _labels(planner, intent, decision, context.label_config)
            labels.extend(scheduled)
            complaints.extend(
                _complaint(planner, label, intent, victim_of[intent.payer], complaint_ids)
                for label in scheduled
                if label.label_source is LabelSource.CUSTOMER_COMPLAINT
                and label.due_at < population.history_end
            )
    return History(
        payroll_accounts=planner.payroll,
        payments=tuple(rows.payments),
        ledger=tuple(rows.ledger),
        telemetry=tuple(rows.telemetry),
        scenario_instances=tuple(context.instances),
        payment_truth=tuple(rows.truth),
        labels=tuple(labels),
        balances=bank.balances,
        devices=tuple(planner.new_devices),
        payees=tuple(planner.new_payees),
        logins=tuple(rows.logins),
        crm_events=context.plan.crm_events,
        complaints=tuple(complaints),
        cash_out_accounts=cash_accounts,
        registry_listings=tuple(registry.listings),
        account_labels=tuple(registry.labels),
        schedule=tuple(row for row, _ in in_flight),
        schedule_truth=tuple(truth for _, truth in in_flight),
    )
