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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from delphi.engine.types import CompiledProfile
from delphi.evaluation.outcomes import (
    Decision,
    FinalStatus,
    PaymentOutcome,
    ProbeMode,
    ProbeRecord,
)
from delphi.evaluation.stream import (
    OTP_PASSED,
    OTP_STEP_UP,
    PaymentTruth,
    StreamItem,
    customer_rng,
    eval_stream,
    otp_would_pass,
    stream_truth,
)
from delphi.guardian.api import PriorInputs, session_prior
from delphi.platform.clock import VirtualClock
from delphi.sim.dataset import Dataset
from delphi.sim.generator_config import GeneratorConfig
from delphi.sim.labels import PaymentDecision
from delphi.sim.legacy_policy import Baseline, decide_b1, decide_b2, payee_is_new
from delphi.sim.response_models import ResponseModels
from delphi.sim.telemetry import SessionTelemetry

EXECUTED = "EXECUTED"
GENUINE = "GENUINE"
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
    # For the passive probes (docs/08 §5), all bank-visible:
    language: str = "en"
    consent_call_status: bool = False
    payee_is_merchant: bool = False
    payee_phone_risk: str | None = None  # the FRI category of the payee account's phone
    payee_earlier_payments: int = 0  # released or executed payments into the payee before now
    payee_earlier_customers: int = 0
    payee_first_payment_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    decided_at: datetime
    probes: tuple[ProbeRecord, ...] = ()
    argmax_hypothesis: str | None = None


Ask = Callable[[str], str]
"""Ask the customer a probe; returns the outcome they give (their own stream, #123 plan §3)."""


class Policy(Protocol):
    name: str

    def decide(self, view: PolicyView, ask: Ask) -> PolicyDecision: ...


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
    customers: Mapping[UUID, Any] = field(default_factory=dict)
    merchants: frozenset[UUID] = frozenset()
    phone_risk: Mapping[UUID, str] = field(default_factory=dict)  # account → FRI category
    # payee account → (initiated_at, payer customer) of its released or executed payments
    paid_into: dict[UUID, list[tuple[datetime, UUID]]] = field(default_factory=dict)

    @staticmethod
    def at_history_end(dataset: Dataset) -> BankState:
        pop, history = dataset.population, dataset.history
        customer_of = {a.id: a.customer_id for a in pop.accounts if a.customer_id is not None}
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
            customer_of=customer_of,
            vulnerable={c.id: c.is_vulnerable for c in pop.customers},
            saved={k: tuple(sorted(v)) for k, v in saved.items()},
            first_seen={d.id: d.first_seen_at for d in (*pop.devices, *history.devices)},
            listed={r.account_id: r.listed_at for r in history.registry_listings},
            customers={c.id: c for c in pop.customers},
            merchants=frozenset(a.id for a in pop.accounts if a.is_merchant),
            phone_risk=_phone_risk(dataset),
            paid_into=_paid_into(dataset, customer_of),
        )

    def view(self, item: StreamItem) -> PolicyView:
        p = item.payment
        at = p.initiated_at
        customer = self.customer_of[p.payer_account_id]
        added = [t for t in self.saved.get((customer, p.payee_account_id), ()) if t <= at]
        listed_at = self.listed.get(p.payee_account_id)
        earlier = [e for e in self.paid_into.get(p.payee_account_id, ()) if e[0] < at]
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
            language=getattr(self.customers.get(customer), "language", "en"),
            consent_call_status=bool(
                getattr(self.customers.get(customer), "consent_call_status", False)
            ),
            payee_is_merchant=p.payee_account_id in self.merchants,
            payee_phone_risk=self.phone_risk.get(p.payee_account_id),
            payee_earlier_payments=len(earlier),
            payee_earlier_customers=len({c for _, c in earlier}),
            payee_first_payment_at=min((t for t, _ in earlier), default=None),
        )

    def transfer(self, payer: UUID, payee: UUID, amount: int) -> bool:
        """Execute when the payer can fund it, as the bank does (docs/05 §3)."""
        if self.balances.get(payer, 0) < amount:
            return False
        self.balances[payer] = self.balances.get(payer, 0) - amount
        self.balances[payee] = self.balances.get(payee, 0) + amount
        self.paid.add((payer, payee))
        return True

    def record_payment(self, view: PolicyView) -> None:
        """A released stream payment counts for the payee's later BENEFICIARY_INTEL."""
        self.paid_into.setdefault(view.payee_account_id, []).append(
            (view.initiated_at, view.customer_id)
        )


COUNTED = frozenset({"RELEASED", "EXECUTED"})  # as the BENEFICIARY_INTEL adapter counts


def _paid_into(
    dataset: Dataset, customer_of: Mapping[UUID, UUID]
) -> dict[UUID, list[tuple[datetime, UUID]]]:
    out: dict[UUID, list[tuple[datetime, UUID]]] = {}
    for p in dataset.history.payments:
        customer = customer_of.get(p.payer_account_id)
        if p.status in COUNTED and customer is not None:
            out.setdefault(p.payee_account_id, []).append((p.initiated_at, customer))
    return out


def _phone_risk(dataset: Dataset) -> dict[UUID, str]:
    risk = {r.phone_number: r.category for r in dataset.phone_risk}
    return {
        a.id: risk[a.phone_number]
        for a in dataset.population.accounts
        if a.phone_number is not None and a.phone_number in risk
    }


@dataclass(frozen=True, slots=True)
class B1:
    """docs/22 §2, the legacy rules, on the view; as the history's B1 and the monitor's."""

    config: GeneratorConfig
    new_device_days: int
    name: str = "B1"

    def decide(self, view: PolicyView, ask: Ask) -> PolicyDecision:
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


@dataclass(frozen=True, slots=True)
class B2:
    """docs/22 §2, score plus fixed friction: r = 1 − p0(GENUINE) from the same sensing prior
    the Guardian builds (``session_prior``). r < 0.20 releases; 0.20 ≤ r < 0.60 asks for an OTP
    and releases if it passes, else holds; r ≥ 0.60 holds. Thresholds: ``score_policy``."""

    config: GeneratorConfig
    profile: CompiledProfile
    sensing: Mapping[UUID, PriorInputs]
    name: str = "B2"

    def decide(self, view: PolicyView, ask: Ask) -> PolicyDecision:
        prior = session_prior(self.profile, self.sensing[view.payment_id])
        p_genuine = float(prior.prior[self.profile.hypotheses.index(GENUINE)])
        verdict = decide_b2(self.config.score_policy, p_genuine=p_genuine)
        if verdict is Baseline.RELEASE:
            return PolicyDecision(Decision.RELEASE, view.initiated_at)
        if verdict is Baseline.HOLD_FOR_REVIEW:
            return PolicyDecision(Decision.HOLD_FOR_REVIEW, view.initiated_at)
        outcome = ask(OTP_STEP_UP)
        otp = ProbeRecord(
            OTP_STEP_UP,
            ProbeMode.ACTIVE,
            friction_paise=round(self.profile.sources[OTP_STEP_UP].cost_paise),
            info_yield_bits=None,
        )
        decision = Decision.RELEASE if outcome == OTP_PASSED else Decision.HOLD_FOR_REVIEW
        return PolicyDecision(decision, view.initiated_at, (otp,))


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
        t = truth[view.payment_id]

        def ask(probe: str, payment_id: UUID = view.payment_id, t: PaymentTruth = t) -> str:
            rng = customer_rng(seed, payment_id, probe)
            # t.sim_day: a coaching adaptation (docs/14 §5.3) applies on the session's day.
            return models.sample_answer(
                rng, t.scenario, probe, coached=t.coached, sim_day=t.sim_day
            )

        decided = policy.decide(view, ask)
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
        bank.record_payment(view)
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
        probes=decided.probes,
        argmax_hypothesis=decided.argmax_hypothesis,
    )
