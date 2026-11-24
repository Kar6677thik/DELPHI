"""Replaying the evaluation stream under one policy (docs/22 §1–2, #123 plan §1–6).

Each policy gets its own copy of the bank: balances from the end of the history, the payer–payee
pairs already paid, and what the bank knows about payees, devices and registry listings. Its
decisions change that copy (a released payment executes when the payer can fund it), so B1, B2
and DELPHI each meet the stream as their own decisions left the bank.

The replay is a discrete-event loop over the stream's payments and the window's scheduled
transfers (salaries, mule forwarding), in time order. Nothing sleeps. The ``VirtualClock`` only
keeps the replay's own time: a policy reads the time from ``view.initiated_at``, not the clock.

A policy sees a ``PolicyView``, built from bank-visible records only (no field comes from
``sim_truth``). The truth needed to score the run (the scenario, the OTP stream) is joined in
only when the ``PaymentOutcome`` is written.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from delphi.evaluation.outcomes import Decision, FinalStatus, PaymentOutcome
from delphi.evaluation.stream import (
    PaymentTruth,
    StreamItem,
    eval_stream,
    otp_would_pass,
    stream_truth,
)
from delphi.platform.clock import VirtualClock
from delphi.sim.dataset import Dataset
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.labels import PaymentDecision
from delphi.sim.legacy_policy import decide_b1, payee_is_new
from delphi.sim.response_models import ResponseModels
from delphi.sim.telemetry import SessionTelemetry

EXECUTED = "EXECUTED"
_TRANSFER, _PAYMENT = 0, 1  # same instant: money arriving first, as the bank books it


@dataclass(frozen=True, slots=True)
class PolicyView:
    """What a policy may know about a payment at initiation (bank-visible only)."""

    payment_id: UUID
    customer_id: UUID
    payer_account_id: UUID
    payee_account_id: UUID
    amount_paise: int
    channel: str
    initiated_at: datetime
    device_id: UUID | None
    vulnerable: bool
    telemetry: SessionTelemetry | None
    payee_listed: bool  # a registry listing published by now
    payee_added_at: datetime | None  # when the customer saved the payee (None: never)
    payee_ever_paid: bool  # an executed payment to this payee before now
    device_first_seen_at: datetime | None
    payer_balance_paise: int


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    decided_at: datetime


class Policy(Protocol):
    name: str

    def decide(self, view: PolicyView) -> PolicyDecision: ...


@dataclass
class BankState:
    """One policy's copy of the bank (#123 plan §1 step 4)."""

    balances: dict[UUID, int]
    paid: set[tuple[UUID, UUID]]
    customer_of: Mapping[UUID, UUID]
    vulnerable: Mapping[UUID, bool]
    saved: Mapping[tuple[UUID, UUID], tuple[datetime, ...]]
    first_seen: Mapping[UUID, datetime]
    listed: Mapping[UUID, datetime]

    @staticmethod
    def at_history_end(dataset: Dataset) -> BankState:
        pop, history = dataset.population, dataset.history
        saved: dict[tuple[UUID, UUID], list[datetime]] = {}
        for p in (*pop.payees, *history.payees):
            saved.setdefault((p.customer_id, p.account_id), []).append(p.added_at)
        return BankState(
            balances=dict(history.balances),
            paid={
                (p.payer_account_id, p.payee_account_id)
                for p in history.payments
                if p.status == EXECUTED
            },
            customer_of={a.id: a.customer_id for a in pop.accounts if a.customer_id is not None},
            vulnerable={c.id: c.is_vulnerable for c in pop.customers},
            saved={k: tuple(sorted(v)) for k, v in saved.items()},
            first_seen={d.id: d.first_seen_at for d in (*pop.devices, *history.devices)},
            listed={r.account_id: r.listed_at for r in history.registry_listings},
        )

    def view(self, item: StreamItem) -> PolicyView:
        p = item.payment
        at = p.initiated_at
        customer = self.customer_of[p.payer_account_id]
        added = [t for t in self.saved.get((customer, p.payee_account_id), ()) if t <= at]
        listed_at = self.listed.get(p.payee_account_id)
        first_seen = self.first_seen.get(p.device_id) if p.device_id is not None else None
        return PolicyView(
            payment_id=p.id,
            customer_id=customer,
            payer_account_id=p.payer_account_id,
            payee_account_id=p.payee_account_id,
            amount_paise=p.amount_paise,
            channel=p.channel,
            initiated_at=at,
            device_id=p.device_id,
            vulnerable=self.vulnerable[customer],
            telemetry=item.telemetry,
            payee_listed=listed_at is not None and listed_at <= at,
            payee_added_at=added[0] if added else None,
            payee_ever_paid=(p.payer_account_id, p.payee_account_id) in self.paid,
            device_first_seen_at=first_seen
            if first_seen is not None and first_seen <= at
            else None,
            payer_balance_paise=self.balances.get(p.payer_account_id, 0),
        )

    def transfer(self, payer: UUID, payee: UUID, amount: int) -> bool:
        """Execute when the payer can fund it, as the bank does (docs/05 §3)."""
        if self.balances.get(payer, 0) < amount:
            return False
        self.balances[payer] = self.balances.get(payer, 0) - amount
        self.balances[payee] = self.balances.get(payee, 0) + amount
        self.paid.add((payer, payee))
        return True


@dataclass(frozen=True, slots=True)
class B1:
    """docs/22 §2, the legacy rules, on the view; as the history's B1 and the monitor's."""

    config: GeneratorConfig
    new_device_days: int
    name: str = "B1"

    def decide(self, view: PolicyView) -> PolicyDecision:
        legacy = self.config.legacy_policy
        first_seen = view.device_first_seen_at
        device_is_new = (
            view.device_id is not None
            and first_seen is not None
            and view.initiated_at - first_seen < timedelta(days=self.new_device_days)
        )
        verdict = decide_b1(
            legacy,
            amount_paise=view.amount_paise,
            channel=view.channel,
            payee_listed=view.payee_listed,
            payee_is_new=payee_is_new(
                legacy,
                added_at=view.payee_added_at,
                at=view.initiated_at,
                ever_paid=view.payee_ever_paid,
            ),
            device_is_new=device_is_new,
        )
        return PolicyDecision(B1_DECISIONS[verdict], view.initiated_at)


B1_DECISIONS = {
    PaymentDecision.RELEASED: Decision.RELEASE,
    PaymentDecision.HELD: Decision.HOLD_FOR_REVIEW,
    PaymentDecision.DECLINED: Decision.DECLINE,
}

_HOLDS = frozenset({Decision.HOLD_FOR_REVIEW, Decision.PROTECTIVE_HOLD})


@dataclass(order=True)
class _Event:
    at: datetime
    kind: int
    seq: int
    item: StreamItem | None = field(compare=False, default=None)
    transfer: tuple[UUID, UUID, int] | None = field(compare=False, default=None)


def _events(dataset: Dataset, stream: Iterable[StreamItem]) -> list[_Event]:
    events = [_Event(i.payment.initiated_at, _PAYMENT, n, item=i) for n, i in enumerate(stream)]
    for n, s in enumerate(dataset.history.schedule):
        payload = s.payload
        transfer = (
            UUID(str(payload["payer_account_id"])),
            UUID(str(payload["payee_account_id"])),
            int(payload["amount_paise"]),
        )
        events.append(_Event(s.due_at, _TRANSFER, n, transfer=transfer))
    heapq.heapify(events)
    return events


def replay(
    dataset: Dataset, policy: Policy, *, seed: int, models: ResponseModels
) -> tuple[PaymentOutcome, ...]:
    """Run ``policy`` over the evaluation stream; one ``PaymentOutcome`` per stream payment.
    A hold stays HELD here: the investigation queue (#124) settles it."""
    stream = eval_stream(dataset)
    truth = stream_truth(dataset)
    bank = BankState.at_history_end(dataset)
    clock = VirtualClock(dataset.population.history_end)
    events = _events(dataset, stream)
    outcomes: list[PaymentOutcome] = []
    while events:
        event = heapq.heappop(events)
        if event.at > clock.now():
            clock.advance_to(event.at)
        if event.transfer is not None:
            bank.transfer(*event.transfer)
            continue
        assert event.item is not None
        view = bank.view(event.item)
        decided = policy.decide(view)
        outcomes.append(_outcome(bank, view, decided, truth, seed=seed, models=models))
    return tuple(outcomes)


def _outcome(
    bank: BankState,
    view: PolicyView,
    decided: PolicyDecision,
    truth: Mapping[UUID, PaymentTruth],
    *,
    seed: int,
    models: ResponseModels,
) -> PaymentOutcome:
    t = truth[view.payment_id]
    decision = decided.decision
    if decision is Decision.RELEASE:
        # A release the payer cannot fund moves no money; it is still the policy letting the
        # payment through, so it is not "stopped" (docs/22 §4).
        bank.transfer(view.payer_account_id, view.payee_account_id, view.amount_paise)
        status = FinalStatus.EXECUTED
    elif decision in _HOLDS:
        status = FinalStatus.HELD
    else:
        status = FinalStatus.DECLINED
    return PaymentOutcome(
        payment_id=view.payment_id,
        customer_id=view.customer_id,
        vulnerable=view.vulnerable,
        amount_paise=view.amount_paise,
        scenario=t.scenario,
        otp_would_pass=otp_would_pass(seed, models, view.payment_id, t),
        decision=decision,
        decided_at=decided.decided_at,
        final_status=status,
        held_at=decided.decided_at if status is FinalStatus.HELD else None,
    )
