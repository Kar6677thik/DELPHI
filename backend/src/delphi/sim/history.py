"""The history period's genuine payments, decided by the legacy policy B1 (docs/14 §4, #17).

Each customer plans its GENUINE_ROUTINE payments from its own stream,
``keyed_rng(seed, "routine", i)``: a daily rate λ ~ Gamma(2, 0.3), Poisson counts per day,
Zipf-weighted payees, log-normal amounts per payee type at a habitual local hour, plus a monthly
salary credit. One chronological pass then applies B1, keeps balances non-negative (a payment
the payer cannot fund is not made), writes the double-entry ledger (docs/05 §1) and schedules
each customer payment's labels (docs/14 §7): a held or declined history payment is never
investigated, so it never gets a label.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import numpy as np

from delphi.kb.model import ClassifierThresholds
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.ids import IdStream
from delphi.sim.labels import (
    LabelConfig,
    PaymentDecision,
    PaymentOutcome,
    ScheduledLabel,
    schedule_payment_labels,
)
from delphi.sim.legacy_policy import decide_b1
from delphi.sim.population import ACTIVE, FULL_KYC, Account, Population
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
class ScenarioInstance:
    id: UUID
    scenario_type: str
    hypothesis_key: str
    params: Mapping[str, Any]
    started_at: datetime


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
        targets: TelemetryTargets,
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
        self.devices: dict[UUID, list[UUID]] = {}
        for device in population.devices:
            self.devices.setdefault(device.customer_id, []).append(device.id)
        payroll_ids = IdStream(self.seed, "payroll_account")
        banks = config.population.external_banks
        self.payroll = tuple(
            Account(
                id=payroll_ids.next(),
                customer_id=None,
                bank_code=bank,
                is_merchant=False,
                merchant_category=None,
                opened_at=population.history_start - timedelta(days=3650),
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
        habit = routine.habitual_hour.min + rng.random() * (
            routine.habitual_hour.max - routine.habitual_hour.min
        )
        payees = self.payees.get(customer.id, [])
        weights = np.asarray(
            [1 / (rank + 1) ** routine.zipf_exponent for rank in range(len(payees))]
        )
        cdf = list(np.cumsum(weights) / weights.sum()) if payees else []
        devices = self.devices[customer.id]
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
                channels = routine.channel[payee_type]
                names = list(channels)
                channel_cdf = list(np.cumsum([channels[n] for n in names]))
                channel = names[min(bisect.bisect_right(channel_cdf, rng.random()), len(names) - 1)]
                hour = (habit + routine.hour_sd * rng.standard_normal()) % _HOURS_PER_DAY
                at = day + timedelta(seconds=int(hour * _SECONDS_PER_HOUR))
                payment_id = self.payment_ids.next()
                signals = draw_signals(rng, self.targets)
                intents.append(
                    _Intent(
                        id=payment_id,
                        payer=account.id,
                        payee=payee,
                        amount_paise=_lognormal_paise(rng, amount.median * scale, amount.sigma),
                        channel=channel,
                        at=at,
                        device=devices[int(rng.integers(len(devices)))],
                        instance=instance.id,
                        decided=True,
                        telemetry=render_telemetry(
                            rng, payment_id, signals, self.cfg.telemetry, self.thresholds
                        ),
                    )
                )


def generate_history(
    config: GeneratorConfig,
    population: Population,
    *,
    targets: TelemetryTargets,
    thresholds: ClassifierThresholds,
    label_config: LabelConfig,
) -> History:
    """GENUINE_ROUTINE payments and salaries over the history, decided by B1.

    ``targets`` are the GENUINE_ROUTINE telemetry targets (docs/14 §5.1) and ``thresholds`` the
    KB's classifier thresholds (docs/08 §5)."""
    planner = _Planner(config, population, targets, thresholds)
    intents: list[_Intent] = []
    instances: list[ScenarioInstance] = []
    for index in range(len(population.customers)):
        planner.customer(index, intents, instances)
    intents = [i for i in intents if population.history_start <= i.at < population.history_end]
    intents.sort(key=lambda i: (i.at, str(i.id)))

    spec = config.profiles[population.profile]
    balances = {a.id: a.balance_paise for a in (*population.accounts, *planner.payroll)}
    payments: list[Payment] = []
    ledger: list[LedgerEntry] = []
    telemetry: list[SessionTelemetry] = []
    truth: list[PaymentTruth] = []
    labels: list[ScheduledLabel] = []
    for intent in intents:
        # Established payees and known devices: B1 has nothing to hold in routine payments,
        # but every customer payment still goes through it.
        decision = (
            decide_b1(
                config.legacy_policy,
                amount_paise=intent.amount_paise,
                channel=intent.channel,
                payee_listed=False,
                payee_is_new=False,
                device_is_new=False,
            )
            if intent.decided
            else PaymentDecision.RELEASED
        )
        if decision is PaymentDecision.RELEASED and balances[intent.payer] < intent.amount_paise:
            continue  # the payer cannot fund it, so the payment is never made
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
        if executed:
            balances[intent.payer] -= intent.amount_paise
            balances[intent.payee] += intent.amount_paise
            ledger.append(
                LedgerEntry(
                    intent.payer, intent.id, -intent.amount_paise, balances[intent.payer], intent.at
                )
            )
            ledger.append(
                LedgerEntry(
                    intent.payee, intent.id, intent.amount_paise, balances[intent.payee], intent.at
                )
            )
        if intent.telemetry is not None:
            telemetry.append(intent.telemetry)
        truth.append(
            PaymentTruth(
                payment_id=intent.id,
                scenario_instance_id=intent.instance,
                is_fraud=False,
                hypothesis_key=ground_truth_key(ScenarioType.GENUINE_ROUTINE),
                coached=False,
            )
        )
        if intent.decided:
            outcome = PaymentOutcome(
                str(intent.id), ScenarioType.GENUINE_ROUTINE, decision, intent.at
            )
            labels.extend(
                schedule_payment_labels(
                    outcome, label_config, seed=spec.seed, maturity_days=spec.maturity_days
                )
            )
    return History(
        payroll_accounts=planner.payroll,
        payments=tuple(payments),
        ledger=tuple(ledger),
        telemetry=tuple(telemetry),
        scenario_instances=tuple(instances),
        payment_truth=tuple(truth),
        labels=tuple(labels),
        balances=balances,
    )
