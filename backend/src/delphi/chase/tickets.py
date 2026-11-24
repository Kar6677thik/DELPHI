"""CFCFRMS tickets, lien release and closing operations (docs/10 §5 steps 4, 6, 7; docs/05 §6;
docs/14 §9; #113).

``process_tickets`` settles the tickets still SENT_SIMULATED, oldest sent first, at most
``limit`` per call (bounded after #263; the next ``CHASE_RETRACE`` run carries on):
- **Acknowledged** once the simulated CFCFRMS latency has passed (``AckDelay``: docs/14 §9,
  ``delphi.sim.external.cfcfrms_ack_delay``, one stream per ticket; 10% never acknowledge).
  The external bank then honours the lien for min(the approved amount, the balance at that
  moment): PLACED, or FAILED_FUNDS_MOVED at 0.
- **Expired** once ``ticket_expiry_min`` has passed without an acknowledgement: the lien is
  FAILED_NO_RESPONSE. Expiry needs no simulator: with no dataset seeded (no ``AckDelay``),
  tickets still expire.

A ticket also settles after its operation was closed by hand: the external bank acts on the
request it was sent, so a lien it places stays under periodic review and is released with
``release_lien`` (docs/10 §5).

``close_due_operations`` closes an OPEN operation once its golden hour has ended and no ticket is
still SENT_SIMULATED (step 7). ``close_operation`` is the manual close, at any time, and
``release_lien`` releases a PLACED lien with a reason (PLACED → RELEASED).

All of them run in the caller's transaction. ``CHASE_RETRACE`` (docs/04 §5.4) calls the first
two; re-tracing open operations (step 5) joins it with the live mule movement (#114).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.chase.operations import (
    CandidateSource,
    TracedOnly,
    active_liens_paise,
    lock_lien_accounts,
    retrace_due,
)
from delphi.chase.tables import cfcfrms_tickets, chase_operations, liens
from delphi.kb.store import get_version
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.jobs import JobContext
from delphi.sim import api as bank

AckDelay = Callable[[str], timedelta | None]
"""The ticket's simulated acknowledgement delay (None: never acknowledged)."""

SENT = "SENT_SIMULATED"


def _scale(demo_time: bool, kb: Any) -> float:
    return float(kb.config.time.demo_time_scale) if demo_time else 1.0


def _balance(session: Session, account_id: UUID) -> int:
    a = bank.accounts.c
    value = session.execute(select(a.balance_paise).where(a.id == account_id)).scalar_one()
    return int(value)


def _lien_event(session: Session, lien_id: UUID, operation_id: UUID, status: str) -> None:
    publish(
        session,
        EventType.CHASE_LIEN_UPDATED,
        str(lien_id),
        {"operation_id": str(operation_id), "lien_id": str(lien_id), "status": status},
    )


def process_tickets(
    session: Session,
    clock: Clock,
    *,
    ack_delay: AckDelay | None,
    limit: int,
    demo_time: bool = False,
) -> Mapping[str, int]:
    """Acknowledge or expire the due tickets among the ``limit`` oldest SENT ones (``ack_delay``
    None: none is acknowledged, but they still expire). Returns the counts."""
    now = clock.now()
    t, lien = cfcfrms_tickets.c, liens.c
    rows = session.execute(
        select(
            t.id,
            t.lien_id,
            t.sent_at,
            lien.account_id,
            lien.amount_paise,
            lien.operation_id,
            chase_operations.c.kb_version_id,
        )
        .join(liens, lien.id == t.lien_id)
        .join(chase_operations, chase_operations.c.id == lien.operation_id)
        .where(t.status == SENT)
        .order_by(t.sent_at, t.id)
        .limit(limit)
        .with_for_update(of=cfcfrms_tickets, skip_locked=True)
    ).all()
    # FR-CHS-05 across operations: an acknowledged lien takes at most the balance less the
    # account's other active liens, under the same per-account lock as approval (#250 review).
    lock_lien_accounts(session, [r.account_id for r in rows])
    counts = {"acknowledged": 0, "expired": 0}
    kbs: dict[int, Any] = {}
    for row in rows:
        if row.kb_version_id not in kbs:
            version = get_version(session, row.kb_version_id)
            assert version is not None
            kbs[row.kb_version_id] = version.kb
        kb = kbs[row.kb_version_id]
        scale = _scale(demo_time, kb)
        delay = ack_delay(str(row.id)) if ack_delay is not None else None
        if delay is not None and row.sent_at + delay * scale <= now:
            others = active_liens_paise(session, [row.account_id], excluding=row.lien_id)
            headroom = _balance(session, row.account_id) - others.get(row.account_id, 0)
            amount = max(min(int(row.amount_paise), headroom), 0)
            placed = amount > 0
            status = "PLACED" if placed else "FAILED_FUNDS_MOVED"
            session.execute(
                update(cfcfrms_tickets)
                .where(t.id == row.id)
                .values(status="ACKNOWLEDGED", acknowledged_at=now)
            )
            session.execute(
                update(liens)
                .where(lien.id == row.lien_id)
                .values(
                    status=status,
                    amount_paise=amount,
                    placed_at=now if placed else None,
                    review_due_at=(
                        now + timedelta(days=kb.regulatory.chase.lien_review_interval_days)
                        if placed
                        else None
                    ),
                )
            )
            counts["acknowledged"] += 1
        elif now >= row.sent_at + timedelta(minutes=kb.config.chase.ticket_expiry_min * scale):
            status = "FAILED_NO_RESPONSE"
            session.execute(update(cfcfrms_tickets).where(t.id == row.id).values(status="EXPIRED"))
            session.execute(update(liens).where(lien.id == row.lien_id).values(status=status))
            counts["expired"] += 1
        else:
            continue
        record(
            session,
            clock,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="chase.ticket.settled",
            entity_type="lien",
            entity_id=str(row.lien_id),
            payload={"ticket_id": str(row.id), "lien_status": status},
        )
        _lien_event(session, row.lien_id, row.operation_id, status)
    return counts


def _close(session: Session, clock: Clock, op: Any, *, by: UUID | None, reason: str) -> None:
    session.execute(
        update(chase_operations)
        .where(chase_operations.c.id == op.id)
        .values(status="CLOSED", version=op.version + 1)
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER if by else ActorType.SYSTEM,
        actor_id=str(by) if by else None,
        action="chase.operation.closed",
        entity_type="chase_operation",
        entity_id=str(op.id),
        payload={"reason": reason},
    )
    publish(session, EventType.CHASE_UPDATED, str(op.id), {"operation_id": str(op.id)})


def close_due_operations(session: Session, clock: Clock) -> int:
    """Step 7: OPEN operations past their golden hour with no ticket still pending."""
    now = clock.now()
    c, t, lien = chase_operations.c, cfcfrms_tickets.c, liens.c
    pending = (
        exists()
        .where(lien.operation_id == c.id, t.lien_id == lien.id, t.status == SENT)
        .correlate(chase_operations)
    )
    due = session.execute(
        select(chase_operations)
        .where(c.status == "OPEN", c.golden_hour_ends_at <= now, ~pending)
        .with_for_update(of=chase_operations)
    ).all()
    for op in due:
        _close(session, clock, op, by=None, reason="GOLDEN_HOUR_ELAPSED")
    return len(due)


def close_operation(session: Session, clock: Clock, operation_id: UUID, *, by: UUID) -> None:
    """Manual close, allowed at any time (docs/10 §5 step 7)."""
    op = session.execute(
        select(chase_operations).where(chase_operations.c.id == operation_id).with_for_update()
    ).one_or_none()
    if op is None:
        raise NotFoundError("No chase operation with this id.")
    if op.status == "CLOSED":
        raise DelphiError(
            "The operation is already closed.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    _close(session, clock, op, by=by, reason="MANUAL")


def release_lien(
    session: Session, clock: Clock, operation_id: UUID, lien_id: UUID, *, by: UUID, reason: str
) -> None:
    """docs/05 §6: PLACED → RELEASED, with the reason."""
    if not reason.strip():
        raise DelphiError("A release needs a reason.", code=ProblemCode.VALIDATION_FAILED)
    row = session.execute(
        select(liens)
        .where(liens.c.id == lien_id, liens.c.operation_id == operation_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFoundError("No lien with this id in this operation.")
    if row.status != "PLACED":
        raise DelphiError(
            f"A {row.status} lien cannot be released.",
            code=ProblemCode.INVALID_STATE_TRANSITION,
        )
    session.execute(
        update(liens)
        .where(liens.c.id == lien_id)
        .values(status="RELEASED", released_at=clock.now(), release_reason=reason.strip())
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(by),
        action="chase.lien.released",
        entity_type="lien",
        entity_id=str(lien_id),
        payload={"operation_id": str(operation_id), "reason": reason.strip()},
    )
    _lien_event(session, lien_id, operation_id, "RELEASED")


@dataclass(frozen=True, slots=True)
class ChaseRetraceJob:
    """``CHASE_RETRACE`` (docs/04 §5.4): settle due tickets, close due operations, then
    re-trace the open ones whose interval has passed (#114)."""

    sessions: sessionmaker[Session]
    clock: Clock
    ack_delay: Callable[[Session], AckDelay | None]
    tickets_per_run: int
    demo_time: bool = False
    candidates: CandidateSource = field(default_factory=TracedOnly)

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        with self.sessions.begin() as session:
            counts: dict[str, Any] = dict(
                process_tickets(
                    session,
                    self.clock,
                    ack_delay=self.ack_delay(session),
                    limit=self.tickets_per_run,
                    demo_time=self.demo_time,
                )
            )
            counts["closed"] = close_due_operations(session, self.clock)
            counts["retraced"] = retrace_due(session, self.clock, self.candidates)
        return counts


def simulated_ack_delay(session: Session) -> AckDelay | None:
    """The simulator's CFCFRMS (docs/14 §9), seeded by the bank's dataset; None before a seed."""
    dataset = bank.current_dataset(session)
    if dataset is None:
        return None
    config = _generator_config()
    return lambda ticket_id: bank.cfcfrms_ack_delay(config, dataset.seed, ticket_id)


@cache
def _generator_config() -> bank.GeneratorConfig:
    """Parsed once per process, not every 20 s (the file is part of the release)."""
    return bank.load_generator_config()


__all__ = [
    "AckDelay",
    "ChaseRetraceJob",
    "close_due_operations",
    "close_operation",
    "process_tickets",
    "release_lien",
    "simulated_ack_delay",
]
