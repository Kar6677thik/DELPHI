"""The simulated investigation queue (docs/22 §3): the same capacity for every policy.

``config.queue.investigators`` investigators work ``shift_start_hour``–``shift_end_hour`` local
time, every day. Work pauses off shift and resumes at the next shift. Each takes the next case
in the policy's order: FIFO for B1/B2 (``fifo``), soonest SLA then the larger expected loss for
DELPHI (``by_sla_then_expected_loss``, docs/09 §1).

How a case is handled:
- **B1/B2 (``checklist``):** every evidence item of the case profile, then disposing. The
  disposition is the Bayes decision under KB v1 with all evidence (an idealised investigator).
- **DELPHI (``navigator``):** the planner's top-ranked evidence until it says STOP, then
  disposing. In the COERCION protocol the outreach call comes first, as the console pins it
  (docs/09 §2). The investigator takes the recommendation with ``accept_probability``, otherwise
  the other decision.

Evidence outcomes come from an ``EvidenceOracle``, the harness's view of the bank and the
response models (#123): the same outcome for a payment and source under every policy (common
random numbers).
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

from delphi.engine import (
    STOP,
    CompiledProfile,
    Phase,
    as_belief,
    decide,
    entropy_bits,
    loss_matrix,
    plan_next,
    update,
)
from delphi.engine.types import FloatArray
from delphi.evaluation.config import QueueConfig
from delphi.evaluation.outcomes import Decision, FinalStatus, LabelRecord, PaymentOutcome
from delphi.kb.model import KnowledgeBase
from delphi.sim.rng import keyed_rng

RELEASE = "RELEASE"  # CASE_PAYMENT_HOLD's decisions
CONFIRM_FRAUD = "CONFIRM_FRAUD"
OUTREACH = "CUSTOMER_OUTREACH_CALL"
# docs/09 §3: an outreach call that confirms coercion, or a denial, gives a label.
LABELLING_OUTREACH = frozenset({"CONFIRMS_COERCION", "DENIES_INITIATING"})
OUTREACH_LABEL = "OUTREACH_CONFIRMED"
DISPOSITION_LABEL = "INVESTIGATOR_DISPOSITION"
QUEUED_DECISIONS = frozenset({Decision.HOLD_FOR_REVIEW, Decision.PROTECTIVE_HOLD})

# (payment id, evidence source key) → the source's outcome for that payment
EvidenceOracle = Callable[[UUID, str], str]


@dataclass(frozen=True, slots=True)
class HeldPayment:
    """A payment in the queue, with the case posterior it opens with (the session's final
    posterior for DELPHI; the sensing prior for B1/B2)."""

    payment_id: UUID
    decision: Decision  # HOLD_FOR_REVIEW or PROTECTIVE_HOLD
    held_at: datetime
    amount_paise: int
    vulnerable: bool
    belief: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Investigation:
    disposition: str  # RELEASE or CONFIRM_FRAUD
    minutes: float  # evidence + disposing
    evidence_minutes: float
    entropy_reduced_bits: float
    outcomes: tuple[tuple[str, str], ...]  # (source key, outcome), in fetch order


@dataclass(frozen=True, slots=True)
class Resolution:
    started_at: datetime
    resolved_at: datetime | None  # None: still being worked when the run ends
    investigation: Investigation


Investigate = Callable[[HeldPayment], Investigation]
Order = Callable[[HeldPayment], tuple[object, ...]]


# --------------------------------------------------------------------------- working time


class Shifts:
    """Working hours: the next on-shift instant, and adding work time across shifts."""

    def __init__(self, config: QueueConfig) -> None:
        self._tz = timezone(timedelta(minutes=config.local_utc_offset_minutes))
        self._start = timedelta(hours=config.shift_start_hour)
        self._end = timedelta(hours=config.shift_end_hour)

    def _day(self, at: datetime) -> datetime:
        return at.astimezone(self._tz).replace(hour=0, minute=0, second=0, microsecond=0)

    def on_shift(self, at: datetime) -> datetime:
        day = self._day(at)
        if at < day + self._start:
            return day + self._start
        if at >= day + self._end:
            return day + timedelta(days=1) + self._start
        return at

    def add_work(self, at: datetime, minutes: float) -> datetime:
        left = timedelta(minutes=minutes)
        at = self.on_shift(at)
        while True:
            shift_end = self._day(at) + self._end
            if at + left <= shift_end:
                return at + left
            left -= shift_end - at
            at = self.on_shift(shift_end)


# --------------------------------------------------------------------------- queue orders


def fifo(item: HeldPayment) -> tuple[object, ...]:
    return (item.held_at, str(item.payment_id))


def by_sla_then_expected_loss(profile: CompiledProfile, kb: KnowledgeBase) -> Order:
    """DELPHI's order (docs/22 §3): ``sla_due_at``, then the larger expected loss (the case's
    Bayes risk at the hold, docs/09 §2), then FIFO."""
    sla = kb.regulatory.case_sla

    def key(item: HeldPayment) -> tuple[object, ...]:
        minutes = (
            sla.protective_sla_minutes
            if item.decision is Decision.PROTECTIVE_HOLD
            else sla.hold_review_sla_minutes
        )
        losses = loss_matrix(profile, item.amount_paise, item.vulnerable)
        _, risk = decide(_belief(profile, item.belief), losses)
        return (item.held_at + timedelta(minutes=minutes), -risk, *fifo(item))

    return key


# --------------------------------------------------------------------------- handling


def _belief(profile: CompiledProfile, belief: Mapping[str, float]) -> FloatArray:
    return as_belief([belief.get(h, 0.0) for h in profile.hypotheses])


@dataclass
class _Case:
    profile: CompiledProfile
    kb: KnowledgeBase
    oracle: EvidenceOracle
    item: HeldPayment

    def __post_init__(self) -> None:
        self.belief = _belief(self.profile, self.item.belief)
        self.start_entropy = entropy_bits(self.belief)
        self.minutes = 0.0
        self.outcomes: list[tuple[str, str]] = []

    def fetch(self, key: str) -> None:
        src = self.profile.sources[key]
        outcome = self.oracle(self.item.payment_id, key)
        self.belief = update(self.belief, src.likelihood[src.outcomes.index(outcome)])
        self.minutes += self.kb.sources[key].expected_minutes or 0.0
        self.outcomes.append((key, outcome))

    def recommended(self) -> str:
        losses = loss_matrix(self.profile, self.item.amount_paise, self.item.vulnerable)
        return self.profile.decisions[decide(self.belief, losses)[0]]

    def close(self, disposition: str, dispose_minutes: float) -> Investigation:
        return Investigation(
            disposition=disposition,
            minutes=self.minutes + dispose_minutes,
            evidence_minutes=self.minutes,
            entropy_reduced_bits=self.start_entropy - entropy_bits(self.belief),
            outcomes=tuple(self.outcomes),
        )


def evidence_keys(profile: CompiledProfile) -> tuple[str, ...]:
    """The profile's evidence items: B1/B2's fixed checklist."""
    return tuple(
        k
        for k, s in profile.sources.items()
        if s.kind == "EVIDENCE" and s.enabled and (not s.contexts or profile.context in s.contexts)
    )


def checklist(
    profile: CompiledProfile, kb: KnowledgeBase, oracle: EvidenceOracle, config: QueueConfig
) -> Investigate:
    """B1/B2: every evidence item, then the Bayes decision with all of it."""
    keys = evidence_keys(profile)

    def investigate(item: HeldPayment) -> Investigation:
        case = _Case(profile, kb, oracle, item)
        for key in keys:
            case.fetch(key)
        return case.close(case.recommended(), config.dispose_minutes)

    return investigate


def navigator(
    profile: CompiledProfile,
    kb: KnowledgeBase,
    oracle: EvidenceOracle,
    config: QueueConfig,
    *,
    seed: int,
) -> Investigate:
    """DELPHI: the planner's next evidence until STOP; the investigator agrees with the
    recommendation with ``accept_probability``, drawn from the payment's own stream."""

    def investigate(item: HeldPayment) -> Investigation:
        case = _Case(profile, kb, oracle, item)
        if item.decision is Decision.PROTECTIVE_HOLD and OUTREACH in profile.sources:
            case.fetch(OUTREACH)  # the COERCION protocol pins the call first (docs/09 §2)
        while True:
            plan = plan_next(
                profile,
                case.belief,
                amount_paise=item.amount_paise,
                vulnerable=item.vulnerable,
                phase=Phase.NAVIGATOR,
                observed=frozenset(k for k, _ in case.outcomes),
            )
            if plan.action == STOP:
                break
            case.fetch(plan.action)
        recommended = case.recommended()
        rng = keyed_rng(seed, "eval", str(item.payment_id), "investigator")
        if rng.random() >= config.accept_probability:
            recommended = next(d for d in profile.decisions if d != recommended)
        return case.close(recommended, config.dispose_minutes)

    return investigate


# --------------------------------------------------------------------------- the queue


def run_queue(
    items: Iterable[HeldPayment],
    investigate: Investigate,
    order: Order,
    config: QueueConfig,
    *,
    until: datetime,
) -> dict[UUID, Resolution]:
    """Work the queue until ``until``: each free investigator takes the first waiting case in
    ``order``. A case started but not finished by ``until`` has no ``resolved_at``; a case
    never started has no resolution."""
    shifts = Shifts(config)
    arrivals = sorted(items, key=lambda i: (i.held_at, str(i.payment_id)))
    waiting: list[tuple[tuple[object, ...], int, HeldPayment]] = []
    start = arrivals[0].held_at if arrivals else until
    free = [(start, n) for n in range(config.investigators)] if arrivals else []
    heapq.heapify(free)
    out: dict[UUID, Resolution] = {}
    next_arrival = 0
    while free:
        at, who = heapq.heappop(free)
        at = shifts.on_shift(at)
        while next_arrival < len(arrivals) and arrivals[next_arrival].held_at <= at:
            item = arrivals[next_arrival]
            heapq.heappush(waiting, (order(item), next_arrival, item))
            next_arrival += 1
        if not waiting:
            if next_arrival == len(arrivals):
                continue  # nothing left: this investigator is done
            heapq.heappush(free, (arrivals[next_arrival].held_at, who))
            continue
        if at >= until:
            break
        _, _, item = heapq.heappop(waiting)
        done = investigate(item)
        finished = shifts.add_work(at, done.minutes)
        out[item.payment_id] = Resolution(at, finished if finished <= until else None, done)
        heapq.heappush(free, (finished, who))
    return out


def settle(
    outcomes: Sequence[PaymentOutcome], resolutions: Mapping[UUID, Resolution]
) -> tuple[list[PaymentOutcome], list[LabelRecord]]:
    """Apply the queue to the policy's outcomes: a released hold executes, a confirmed fraud
    is declined, an unresolved hold stays HELD. Each disposition gives an
    INVESTIGATOR_DISPOSITION label, and a labelling outreach call an OUTREACH_CONFIRMED label,
    both known when the case is disposed."""
    settled: list[PaymentOutcome] = []
    labels: list[LabelRecord] = []
    for o in outcomes:
        r = resolutions.get(o.payment_id)
        if o.decision not in QUEUED_DECISIONS:
            settled.append(o)
            continue
        if r is None or r.resolved_at is None:
            settled.append(replace(o, held_at=o.decided_at, final_status=FinalStatus.HELD))
            continue
        done = r.investigation
        released = done.disposition == RELEASE
        settled.append(
            replace(
                o,
                held_at=o.decided_at,
                resolved_at=r.resolved_at,
                final_status=FinalStatus.EXECUTED if released else FinalStatus.DECLINED,
                evidence_minutes=done.evidence_minutes,
                entropy_reduced_bits=done.entropy_reduced_bits,
            )
        )
        labels.append(LabelRecord(o.payment_id, DISPOSITION_LABEL, r.resolved_at))
        if any(k == OUTREACH and v in LABELLING_OUTREACH for k, v in done.outcomes):
            labels.append(LabelRecord(o.payment_id, OUTREACH_LABEL, r.resolved_at))
    return settled, labels
