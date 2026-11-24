"""Chase operations and liens (docs/10 §1, §5; docs/05 §6; #113).

``create_operation`` traces a reported fraud (docs/10 §3) and stores every account still holding
taint as a freeze candidate, scored (docs/10 §4, #112). Candidates come through
``CandidateSource``, so the scoring stays the owner's (#112) and a test can feed its own.

``approve_liens`` is the only way a lien comes to exist (FR-CHS-04): an investigator approves
selected candidates. For each one the amount is recomputed **at approval**, as the lower of the
traced taint and the account's balance now (FR-CHS-05), with the recorded reasons and the
payments along the taint's path as evidence (docs/10 §5 step 2).
- An internal account (`DLPH`) is liened at once: PLACED, or FAILED_FUNDS_MOVED when nothing
  is left (docs/10 §5 step 3).
- An external account gets a CFCFRMS ticket, DRAFTED then SENT_SIMULATED, and its lien waits
  as TICKET_SENT (step 4). The worker acknowledges or expires tickets (#113 PR 2).

Liens are recorded here; holding the liened money back from the account's later debits is the
live mule movement's (#114).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Text, cast, func, literal, select, text, update
from sqlalchemy.orm import Session

from delphi.chase.ledger import CASH_OUT_CHANNEL, load_trace_inputs, visible_until
from delphi.chase.tables import cfcfrms_tickets, chase_candidates, chase_operations, liens
from delphi.chase.tracer import TraceResult, trace
from delphi.kb.model import KnowledgeBase
from delphi.kb.store import active_version, get_version
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.locks import LIEN_ACCOUNT_LOCK_CLASS
from delphi.sim import api as bank

PAISE_PER_INR = 100
EXECUTED = "EXECUTED"


@dataclass(frozen=True, slots=True)
class TracedCandidate:
    """An account still holding taint, and its score once scored (None: not scored)."""

    account_id: UUID
    hop: int
    taint_paise: int
    balance_paise: int
    p_present: float | None = None
    p_mule: float | None = None
    erv_paise: int | None = None
    wrongful_cost_paise: int | None = None
    net_paise: int | None = None
    rank: int | None = None
    recommended: bool | None = None


class CandidateSource(Protocol):
    """Turns a trace into freeze candidates (docs/10 §4). #112's scoring plugs in here."""

    def candidates(
        self, session: Session, kb: KnowledgeBase, traced: TraceResult, now: datetime
    ) -> list[TracedCandidate]: ...


class TracedOnly:
    """Candidates as the tracer gives them, unscored (docs/10 §4's floor applies)."""

    def candidates(
        self, session: Session, kb: KnowledgeBase, traced: TraceResult, now: datetime
    ) -> list[TracedCandidate]:
        floor = round(kb.config.chase.min_trace_inr * PAISE_PER_INR)
        held = {UUID(a): t for a, t in traced.taint_now.items() if t >= floor}
        balances = _balances(session, held)
        return [
            TracedCandidate(a, traced.depth[str(a)], t, balances.get(a, 0))
            for a, t in sorted(held.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ]


# Until the trace loader is bounded (#266), the request's transaction is limited the way the
# recurring jobs are (#263): a slow or huge trace fails the request instead of the database.
TRACE_STATEMENT_TIMEOUT = "30s"
TRACE_WORK_MEM = "16MB"


def bound_trace(session: Session) -> None:
    session.execute(text(f"SET LOCAL statement_timeout = '{TRACE_STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL work_mem = '{TRACE_WORK_MEM}'"))


ACTIVE_LIEN_STATUSES = ("PLACED", "TICKET_SENT")  # money a lien holds, or is about to


def lock_lien_accounts(session: Session, accounts: Sequence[UUID]) -> None:
    """Serialise lien setting per account (FR-CHS-05), in a fixed order so two transactions
    locking overlapping accounts cannot deadlock. Held until the transaction ends."""
    for account in sorted(set(accounts), key=str):
        session.execute(
            select(
                func.pg_advisory_xact_lock(
                    LIEN_ACCOUNT_LOCK_CLASS, func.hashtext(cast(literal(account), Text))
                )
            )
        )


def active_liens_paise(
    session: Session, accounts: Sequence[UUID], *, excluding: UUID | None = None
) -> dict[UUID, int]:
    """Each account's money already under an active lien (PLACED or TICKET_SENT), any
    operation's; ``excluding`` leaves one lien out (the one being settled)."""
    if not accounts:
        return {}
    n = liens.c
    query = (
        select(n.account_id, func.sum(n.amount_paise))
        .where(n.account_id.in_(list(accounts)), n.status.in_(ACTIVE_LIEN_STATUSES))
        .group_by(n.account_id)
    )
    if excluding is not None:
        query = query.where(n.id != excluding)
    return {a: int(total) for a, total in session.execute(query).tuples()}


def _balances(session: Session, accounts: Mapping[UUID, Any]) -> dict[UUID, int]:
    if not accounts:
        return {}
    a = bank.accounts.c
    rows = session.execute(select(a.id, a.balance_paise).where(a.id.in_(list(accounts))))
    return {r.id: int(r.balance_paise) for r in rows}


def _sources(
    session: Session, complaint_id: UUID | None, payment_ids: Sequence[UUID]
) -> tuple[list[UUID], datetime | None]:
    if complaint_id is None:
        return list(payment_ids), None
    c = bank.complaints.c
    row = session.execute(
        select(c.payment_ids, c.reported_at).where(c.id == complaint_id)
    ).one_or_none()
    if row is None:
        raise NotFoundError(f"No complaint {complaint_id}.")
    return list(row.payment_ids), row.reported_at


EVAL_PROFILE: bank.DatasetProfile = "eval"  # checked against the profiles by mypy


def visibility_lag(session: Session, kb: KnowledgeBase) -> float:
    """docs/10 §2: the minutes Chase lags the payment network, by the seeded dataset's profile:
    ``eval``'s lag on an `eval` dataset, ``demo``'s otherwise (`demo` and `test`)."""
    dataset = bank.current_dataset(session)
    lags = kb.config.chase.visibility_lag_min
    return lags.eval if dataset is not None and dataset.profile == EVAL_PROFILE else lags.demo


def create_operation(
    session: Session,
    clock: Clock,
    *,
    complaint_id: UUID | None,
    payment_ids: Sequence[UUID],
    created_by: UUID | None,
    source: CandidateSource,
    demo_time: bool = False,
) -> UUID:
    """docs/10 §1: an operation from a complaint (its payments) or from payment ids, traced and
    with its candidates stored. Only executed payments carry money; none is a 422."""
    if (complaint_id is None) == (not payment_ids):
        raise DelphiError(
            "Give a complaint_id or payment_ids, not both.", code=ProblemCode.VALIDATION_FAILED
        )
    version = active_version(session)
    if version is None:
        raise DelphiError("No ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
    kb = version.kb
    now = clock.now()
    ids, complained_at = _sources(session, complaint_id, payment_ids)
    bound_trace(session)
    sources, moves = load_trace_inputs(
        session.connection(), ids, until=visible_until(now, visibility_lag(session, kb))
    )
    if not sources:
        raise DelphiError(
            "None of these payments was executed: there is no money to chase.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    traced = trace(
        sources,
        moves,
        min_trace_paise=round(kb.config.chase.min_trace_inr * PAISE_PER_INR),
        cash_out_channel=CASH_OUT_CHANNEL,
    )
    scale = kb.config.time.demo_time_scale if demo_time else 1.0
    reported_at = complained_at or now
    op_id = uuid7(unix_ms=int(now.timestamp() * 1000))
    session.execute(
        chase_operations.insert().values(
            id=op_id,
            complaint_id=complaint_id,
            source_payment_ids=[UUID(s.payment_id) for s in sources],
            status="OPEN",
            reported_at=reported_at,
            reported_loss_paise=traced.initial_paise,
            kb_version_id=version.id,
            golden_hour_ends_at=reported_at
            + timedelta(minutes=kb.regulatory.chase.golden_hour_minutes * scale),
            last_traced_at=now,
            metrics={},
            created_by=created_by,
            version=1,
        )
    )
    for n, c in enumerate(source.candidates(session, kb, traced, now)):
        session.execute(
            chase_candidates.insert().values(
                id=uuid7(unix_ms=int(now.timestamp() * 1000) + n + 1),
                operation_id=op_id,
                account_id=c.account_id,
                hop=c.hop,
                taint_paise=c.taint_paise,
                balance_paise=c.balance_paise,
                p_present=c.p_present,
                p_mule=c.p_mule,
                erv_paise=c.erv_paise,
                wrongful_cost_paise=c.wrongful_cost_paise,
                net_paise=c.net_paise,
                rank=c.rank,
                recommended=c.recommended,
                traced_at=now,
            )
        )
    record(
        session,
        clock,
        actor_type=ActorType.USER if created_by else ActorType.SYSTEM,
        actor_id=str(created_by) if created_by else None,
        action="chase.operation.created",
        entity_type="chase_operation",
        entity_id=str(op_id),
        payload={"complaint_id": str(complaint_id) if complaint_id else None},
    )
    publish(session, EventType.CHASE_UPDATED, str(op_id), {"operation_id": str(op_id)})
    return op_id


def lien_amount(taint_paise: int, balance_paise: int) -> int:
    """FR-CHS-05 / docs/05 §6: at most the traced taint and at most the balance now; never the
    whole balance unless all of it is taint."""
    return max(min(taint_paise, balance_paise), 0)


def _path(traced_flows: Sequence[Any], account: str) -> list[str]:
    """The payments that carried taint into ``account`` and the accounts before it."""
    into: dict[str, list[Any]] = {}
    for f in traced_flows:
        if f.to_account_id is not None:
            into.setdefault(f.to_account_id, []).append(f)
    seen: set[str] = set()
    refs: list[str] = []
    stack = [account]
    while stack:
        current = stack.pop()
        for f in into.get(current, []):
            if f.payment_id not in seen:
                seen.add(f.payment_id)
                refs.append(f.payment_id)
                stack.append(f.from_account_id)
    return refs


def approve_liens(
    session: Session,
    clock: Clock,
    operation_id: UUID,
    candidate_ids: Sequence[UUID],
    *,
    approved_by: UUID,
) -> list[UUID]:
    """docs/10 §5 steps 1–4 for the candidates an investigator approved. Returns the liens."""
    op = session.execute(
        select(chase_operations).where(chase_operations.c.id == operation_id).with_for_update()
    ).one_or_none()
    if op is None:
        raise NotFoundError("No chase operation with this id.")
    if op.status != "OPEN":
        raise DelphiError(
            "A closed operation takes no liens.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    version = get_version(session, op.kb_version_id)
    assert version is not None
    kb = version.kb
    if not candidate_ids or len(candidate_ids) > kb.config.chase.max_liens_per_round:
        raise DelphiError(
            f"Approve 1 to {kb.config.chase.max_liens_per_round} candidates at a time.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    k = chase_candidates.c
    rows = session.execute(
        select(chase_candidates).where(k.operation_id == operation_id, k.id.in_(candidate_ids))
    ).all()
    if len(rows) != len(set(candidate_ids)):
        raise NotFoundError("A candidate is not in this operation.")
    now = clock.now()
    # The taint's path for the evidence refs: re-traced as of now (docs/10 §5 step 2).
    bound_trace(session)
    sources, moves = load_trace_inputs(
        session.connection(),
        list(op.source_payment_ids),
        until=visible_until(now, visibility_lag(session, kb)),
    )
    traced = trace(
        sources,
        moves,
        min_trace_paise=round(kb.config.chase.min_trace_inr * PAISE_PER_INR),
        cash_out_channel=CASH_OUT_CHANNEL,
    )
    accounts = [r.account_id for r in rows]
    # FR-CHS-05 across operations: a lien is capped by the balance less what other liens on
    # the account already hold, under a per-account lock (#250 review).
    lock_lien_accounts(session, accounts)
    held = active_liens_paise(session, accounts)
    balances = {
        a: max(b - held.get(a, 0), 0)
        for a, b in _balances(session, {a: None for a in accounts}).items()
    }
    a = bank.accounts.c
    banks = dict(
        session.execute(
            select(a.id, a.bank_code).where(a.id.in_([r.account_id for r in rows]))
        ).all()
    )
    created: list[UUID] = []
    for n, row in enumerate(sorted(rows, key=lambda r: str(r.id))):
        if session.execute(select(liens.c.id).where(liens.c.candidate_id == row.id)).first():
            raise DelphiError(
                "A candidate already has a lien.", code=ProblemCode.INVALID_STATE_TRANSITION
            )
        account = str(row.account_id)
        taint_now = traced.taint_now.get(account, 0)
        amount = lien_amount(taint_now, balances.get(row.account_id, 0))
        balances[row.account_id] = balances.get(row.account_id, 0) - amount
        refs = _path(traced.flows, account) or [str(p) for p in op.source_payment_ids]
        internal = banks[row.account_id] == bank.HOME_BANK
        p_mule = f"{row.p_mule:.2f}" if row.p_mule is not None else "not scored"
        reasons = (
            f"Traced ₹{taint_now / PAISE_PER_INR:,.2f} of reported fraud "
            f"{op.complaint_id or 'payments ' + ', '.join(map(str, op.source_payment_ids))} "
            f"reached this account via {row.hop} hop(s) ({' → '.join(reversed(refs))}). "
            f"The mule score is {p_mule}. A lien is placed on the traced amount only, per "
            f"DELPHI chase operation {operation_id}."
        )
        if internal:
            status = "PLACED" if amount > 0 else "FAILED_FUNDS_MOVED"
        else:
            status = "TICKET_SENT" if amount > 0 else "FAILED_FUNDS_MOVED"
        placed = status == "PLACED"
        lien_id = uuid7(unix_ms=int(now.timestamp() * 1000) + n)
        session.execute(
            liens.insert().values(
                id=lien_id,
                operation_id=operation_id,
                candidate_id=row.id,
                account_id=row.account_id,
                bank_code=banks[row.account_id],
                amount_paise=amount,
                status=status,
                recorded_reasons=reasons,
                evidence_refs=refs,
                approved_by=approved_by,
                approved_at=now,
                placed_at=now if placed else None,
                review_due_at=(
                    now + timedelta(days=kb.regulatory.chase.lien_review_interval_days)
                    if placed
                    else None
                ),
            )
        )
        if status == "TICKET_SENT":
            session.execute(
                cfcfrms_tickets.insert().values(
                    id=uuid7(unix_ms=int(now.timestamp() * 1000) + n),
                    lien_id=lien_id,
                    target_bank=banks[row.account_id],
                    status="SENT_SIMULATED",
                    payload={
                        "complaint_id": str(op.complaint_id) if op.complaint_id else None,
                        "account_id": account,
                        "bank": banks[row.account_id],
                        "amount_paise": amount,
                        "reasons": reasons,
                    },
                    sent_at=now,
                )
            )
        record(
            session,
            clock,
            actor_type=ActorType.USER,
            actor_id=str(approved_by),
            action="chase.lien.approved",
            entity_type="lien",
            entity_id=str(lien_id),
            payload={"operation_id": str(operation_id), "status": status, "amount_paise": amount},
        )
        publish(
            session,
            EventType.CHASE_LIEN_UPDATED,
            str(lien_id),
            {"operation_id": str(operation_id), "lien_id": str(lien_id), "status": status},
        )
        created.append(lien_id)
    session.execute(
        update(chase_operations)
        .where(chase_operations.c.id == operation_id)
        .values(version=op.version + 1)
    )
    return created
