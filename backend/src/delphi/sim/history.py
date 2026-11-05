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
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import numpy as np

from delphi.kb.model import ClassifierThresholds
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
)
from delphi.sim.legacy_policy import decide_b1, payee_is_new
from delphi.sim.logins import LoginEvent, login_before
from delphi.sim.mules import MuleNetwork
from delphi.sim.population import ACTIVE, FULL_KYC, Account, Device, Payee, Population, _choice
from delphi.sim.records import Complaint, CrmEvent, ScenarioInstance
from delphi.sim.response_models import TelemetryTargets, draw_signals
from delphi.sim.rng import keyed_rng
from delphi.sim.scenarios import ScenarioType, ground_truth_key
from delphi.sim.telemetry import SessionTelemetry, render_telemetry

IST = timezone(timedelta(hours=5, minutes=30))  # app chrome and habits are local (ADR-0014)
SIM_HISTORY = "SIM_HISTORY"
SALARY_CHANNEL = "NEFT"
_STATUS = {
    PaymentDecision.RELEASED: "EXECUTED",  # in the simulation RELEASED → EXECUTED is immediate
    PaymentDecision.HELD: "HELD",
    PaymentDecision.DECLINED: "DECLINED",
}
_PAISE_PER_INR = 100
_HOURS_PER_DAY = 24
_SECONDS_PER_HOUR = 3600


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

    def settle(self, balance: int) -> int:
        """The amount actually sent, given the payer's balance at the time."""
        if self.balance_share is not None:
            return int(balance * self.balance_share) // _PAISE_PER_INR * _PAISE_PER_INR
        if self.max_balance_share is not None:
            cap = int(balance * self.max_balance_share) // _PAISE_PER_INR * _PAISE_PER_INR
            return min(self.amount_paise, cap)
        return self.amount_paise


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
        self.phones: dict[UUID, list[Device]] = {}  # each customer's genuine new phones
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
        """Local (IST) midnights of every history day."""
        first = self.pop.history_start.astimezone(IST).replace(hour=0, minute=0, second=0)
        days = []
        day = first
        while day < self.pop.history_end:
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
        customer_index: int,
        payer: UUID,
        payee: UUID,
        amount_paise: int,
        channel: str,
        at: datetime,
        device: Device,
        instance: UUID,
        city: str | None = None,
    ) -> _Intent:
        customer = self.pop.customers[customer_index]
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
        start, end = self.pop.history_start, self.pop.history_end
        if not start <= at < end:
            return  # outside the history: no payment, so no payee or phone either
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
        intents.append(
            self._session(
                rng,
                ScenarioType.GENUINE_UNUSUAL,
                customer_index=index,
                payer=account.id,
                payee=payee,
                amount_paise=_lognormal_paise(
                    rng, cfg.amount_inr.median * scale, cfg.amount_inr.sigma
                ),
                channel=cfg.channel,
                at=at,
                device=device,
                instance=instance.id,
            )
        )

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
                customer_index=session.customer_index,
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
        unusual_rate = rate * unusual.rate_per_10k / unusual.routine_rate_per_10k
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
            if at >= self.pop.history_end:
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
                        customer_index=index,
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
    genuine = sorted(
        (i for i in genuine if inside[0] <= i.at < inside[1]), key=lambda i: (i.at, str(i.id))
    )
    # The fraud rates are per 10,000 payments made: a dry run of the genuine payments counts
    # the sessions that happen (some are never made, for want of funds).
    dry = _Bank(planner, network)
    made = sum(dry.process(i) is not None and i.decided for i in genuine)
    plan = plan_fraud(
        config, population, network, genuine_sessions=made, coached_share=coached_share
    )
    instances.extend(plan.instances)
    planner.new_devices.extend(plan.devices)
    planner.new_payees.extend(plan.payees)
    fraud = [i for i in planner.fraud(plan) if inside[0] <= i.at < inside[1]]
    intents = sorted((*genuine, *fraud), key=lambda i: (i.at, str(i.id)))
    return _run(planner, intents, context=_RunContext(network, instances, plan, label_config))


@dataclass(frozen=True, slots=True)
class _RunContext:
    network: MuleNetwork
    instances: list[ScenarioInstance]
    plan: FraudPlan
    label_config: LabelConfig


class _Bank:
    """The bank's state through the pass: balances, B1's view of payees and devices."""

    def __init__(self, planner: _Planner, network: MuleNetwork) -> None:
        pop = planner.pop
        self.policy = planner.cfg.legacy_policy
        self.history_start = pop.history_start
        self.balances = {
            a.id: a.balance_paise for a in (*pop.accounts, *network.accounts, *planner.payroll)
        }
        self.listed_at = {listing.account_id: listing.listed_at for listing in network.listings}
        home = planner.home_account
        self.saved_at = {
            (home[p.customer_id].id, p.account_id): p.added_at
            for p in (*pop.payees, *planner.new_payees)
        }
        self.first_seen = {d.id: d.first_seen_at for d in (*pop.devices, *planner.new_devices)}
        self.new_device = timedelta(days=planner.thresholds.new_device_days)
        self.paid: set[tuple[UUID, UUID]] = set()

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
        amount = planned.settle(self.balances[planned.payer])
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


def _run(planner: _Planner, intents: list[_Intent], *, context: _RunContext) -> History:
    """One chronological pass: B1, the funds check, the ledger, truth, labels, complaints."""
    population = planner.pop
    spec = planner.cfg.profiles[population.profile]
    bank = _Bank(planner, context.network)
    complaint_ids = IdStream(planner.seed, "complaint")
    victim_of = {a.id: a.customer_id for a in population.accounts if a.customer_id}
    payments: list[Payment] = []
    ledger: list[LedgerEntry] = []
    telemetry: list[SessionTelemetry] = []
    truth: list[PaymentTruth] = []
    labels: list[ScheduledLabel] = []
    logins: list[LoginEvent] = []
    complaints: list[Complaint] = []
    for planned in intents:
        made = bank.process(planned)
        if made is None:
            continue
        intent, decision = made
        executed = decision is PaymentDecision.RELEASED
        payments.append(
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
            logins.append(intent.login)
        if executed:
            for account, delta in (
                (intent.payer, -intent.amount_paise),
                (intent.payee, intent.amount_paise),
            ):
                ledger.append(
                    LedgerEntry(account, intent.id, delta, bank.balances[account], intent.at)
                )
        if intent.telemetry is not None:
            telemetry.append(intent.telemetry)
        truth.append(
            PaymentTruth(
                payment_id=intent.id,
                scenario_instance_id=intent.instance,
                is_fraud=intent.is_fraud,
                hypothesis_key=ground_truth_key(intent.scenario),
                coached=intent.coached,
            )
        )
        if intent.decided:
            outcome = PaymentOutcome(str(intent.id), intent.scenario, decision, intent.at)
            scheduled = schedule_payment_labels(
                outcome, context.label_config, seed=spec.seed, maturity_days=spec.maturity_days
            )
            labels.extend(scheduled)
            complaints.extend(
                _complaint(planner, label, intent, victim_of[intent.payer], complaint_ids)
                for label in scheduled
                if label.label_source is LabelSource.CUSTOMER_COMPLAINT
                and label.due_at < population.history_end
            )
    return History(
        payroll_accounts=planner.payroll,
        payments=tuple(payments),
        ledger=tuple(ledger),
        telemetry=tuple(telemetry),
        scenario_instances=tuple(context.instances),
        payment_truth=tuple(truth),
        labels=tuple(labels),
        balances=bank.balances,
        devices=tuple(planner.new_devices),
        payees=tuple(planner.new_payees),
        logins=tuple(logins),
        crm_events=context.plan.crm_events,
        complaints=tuple(complaints),
    )
