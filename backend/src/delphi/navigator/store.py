"""Cases: opening, reading and listing them (docs/05 §4, docs/09 §1, docs/16, #97).

M4's stub opens a PAYMENT_HOLD case when Guardian holds a payment: protocol COERCION for
PROTECTIVE_HOLD, STANDARD for HOLD_FOR_REVIEW; the initial posterior is the session's final
posterior, and the SLA is the pinned KB's (`regulatory.case_sla`, docs/07). Guardian calls
``open_case`` with plain values, so the Navigator imports nothing from Guardian (no cycle);
linking the session back to the case (`guardian_sessions.case_id`) is Guardian's (#94). Evidence,
dispositions and the console come with #103.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Float, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from delphi.kb.store import get_version
from delphi.navigator.console import case_profile, recommend
from delphi.navigator.police_alert import draft_alert
from delphi.navigator.tables import cases
from delphi.navigator.traces import TraceType, record_trace
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.sim import api as bank


class CaseType(StrEnum):
    PAYMENT_HOLD = "PAYMENT_HOLD"
    ACCOUNT_REVIEW = "ACCOUNT_REVIEW"


class CaseProtocol(StrEnum):
    STANDARD = "STANDARD"
    COERCION = "COERCION"


class CaseStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DISPOSED = "DISPOSED"


class CaseSort(StrEnum):
    """The case queue's orders (docs/16): soonest SLA first, the largest expected loss first, or
    newest first."""

    SLA_DUE_AT = "sla_due_at"
    EXPECTED_LOSS = "expected_loss"
    OPENED_AT = "opened_at"


# docs/09 §1: the Guardian decisions that open a case, and the protocol each one gets.
HOLD_DECISIONS: Mapping[str, CaseProtocol] = {
    "HOLD_FOR_REVIEW": CaseProtocol.STANDARD,
    "PROTECTIVE_HOLD": CaseProtocol.COERCION,
}


class CaseExistsError(DelphiError):
    """The payment already has an open case (docs/05 §4)."""

    code = ProblemCode.INVALID_STATE_TRANSITION


@dataclass(frozen=True, slots=True)
class CaseRecord:
    id: UUID
    type: CaseType
    protocol: CaseProtocol
    status: CaseStatus
    subject_payment_id: UUID | None
    subject_account_id: UUID | None
    guardian_session_id: UUID | None
    kb_version_id: int
    prior_components: Mapping[str, Any]
    posterior: Mapping[str, float]
    recommended_decision: str | None
    risk_by_decision: Mapping[str, Any] | None
    leading_hypothesis: str
    campaign_id: UUID | None
    amount_at_stake_paise: int
    opened_at: datetime
    sla_due_at: datetime
    disposition: str | None
    disposed_at: datetime | None
    dd_pack_complete: bool
    unexplained: bool
    version: int
    # the disposition's details (docs/05 §4; #103), None until DISPOSED
    disposition_typology: str | None = None
    disposition_confidence: int | None = None
    is_override: bool = False
    override_rationale: str | None = None
    disposed_by: UUID | None = None

    @staticmethod
    def of(row: Any) -> CaseRecord:
        return CaseRecord(
            id=row.id,
            type=CaseType(row.type),
            protocol=CaseProtocol(row.protocol),
            status=CaseStatus(row.status),
            subject_payment_id=row.subject_payment_id,
            subject_account_id=row.subject_account_id,
            guardian_session_id=row.guardian_session_id,
            kb_version_id=row.kb_version_id,
            prior_components=row.prior_components,
            posterior=row.posterior,
            recommended_decision=row.recommended_decision,
            risk_by_decision=row.risk_by_decision,
            leading_hypothesis=row.leading_hypothesis,
            campaign_id=row.campaign_id,
            amount_at_stake_paise=row.amount_at_stake_paise,
            opened_at=row.opened_at,
            sla_due_at=row.sla_due_at,
            disposition=row.disposition,
            disposed_at=row.disposed_at,
            dd_pack_complete=row.dd_pack_complete,
            unexplained=row.unexplained,
            version=row.version,
            disposition_typology=row.disposition_typology,
            disposition_confidence=row.disposition_confidence,
            is_override=row.is_override,
            override_rationale=row.override_rationale,
            disposed_by=row.disposed_by,
        )

    @property
    def expected_loss_paise(self) -> float | None:
        """The recommended decision's risk (#208); None for a case stored without one."""
        if self.recommended_decision is None or not self.risk_by_decision:
            return None
        risk = self.risk_by_decision.get(self.recommended_decision)
        return None if risk is None else float(risk)


def _leading(posterior: Mapping[str, float], order: Sequence[str]) -> str:
    """The most probable hypothesis; ties go to the KB's hypothesis order."""
    rank = {k: i for i, k in enumerate(order)}
    return min(posterior, key=lambda k: (-posterior[k], rank.get(k, len(rank)), k))


def open_case(
    session: Session,
    clock: Clock,
    *,
    decision: str,
    payment_id: UUID,
    guardian_session_id: UUID,
    kb_version_id: int,
    posterior: Mapping[str, float],
    prior_components: Mapping[str, Any],
    amount_at_stake_paise: int,
    unexplained: bool,
    demo_time: bool = False,
) -> CaseRecord:
    """A PAYMENT_HOLD case for a Guardian hold (docs/09 §1), in the caller's transaction.

    ``decision`` must be HOLD_FOR_REVIEW (protocol STANDARD) or PROTECTIVE_HOLD (COERCION). The
    SLA comes from the KB version the session was pinned to, scaled by that version's
    ``demo_time_scale`` in DEMO time (docs/07). Opening writes the audit entry and publishes
    ``case.opened`` (docs/16 §3, docs/17 §4) with the case.

    Idempotent for a retried Guardian transaction: if the payment already has an open case from
    the same session, that case is returned and nothing is written again. An open case from
    another session is refused (``CaseExistsError``).
    """
    protocol = HOLD_DECISIONS.get(decision)
    if protocol is None:
        raise DelphiError(
            f"A {decision} decision opens no case (docs/09 §1).",
            code=ProblemCode.VALIDATION_FAILED,
        )
    if not posterior:
        raise DelphiError(
            "A case needs the session's posterior.", code=ProblemCode.VALIDATION_FAILED
        )
    existing = open_case_for_payment(session, payment_id)
    if existing is not None:
        if existing.guardian_session_id == guardian_session_id:
            return existing
        raise CaseExistsError(f"Payment {payment_id} already has an open case.")
    version = get_version(session, kb_version_id)
    if version is None:
        raise DelphiError(f"No KB version {kb_version_id}.", code=ProblemCode.VALIDATION_FAILED)
    sla = version.kb.regulatory.case_sla
    minutes = (
        sla.protective_sla_minutes
        if protocol is CaseProtocol.COERCION
        else sla.hold_review_sla_minutes
    )
    scale = version.kb.config.time.demo_time_scale if demo_time else 1.0
    now = clock.now()
    case_id = uuid7(unix_ms=int(now.timestamp() * 1000))
    sla_due_at = now + timedelta(minutes=minutes * scale)
    leading = _leading(posterior, version.kb.hypothesis_order)
    advice = recommend(  # #208: stored while open, so the queue can show and sort by it
        case_profile(version.kb, CaseType.PAYMENT_HOLD.value),
        posterior,
        amount_paise=amount_at_stake_paise,
        vulnerable=payer_is_vulnerable(session, payment_id),
    )
    try:
        with session.begin_nested():
            session.execute(
                cases.insert().values(
                    id=case_id,
                    type=CaseType.PAYMENT_HOLD.value,
                    protocol=protocol.value,
                    status=CaseStatus.OPEN.value,
                    subject_payment_id=payment_id,
                    guardian_session_id=guardian_session_id,
                    kb_version_id=kb_version_id,
                    prior_components=dict(prior_components),
                    posterior=dict(posterior),
                    leading_hypothesis=leading,
                    recommended_decision=advice.decision,
                    risk_by_decision=dict(advice.risk_by_decision),
                    amount_at_stake_paise=amount_at_stake_paise,
                    opened_at=now,
                    sla_due_at=sla_due_at,
                    is_override=False,
                    dd_pack_complete=False,
                    unexplained=unexplained,
                    version=1,
                )
            )
    except IntegrityError as exc:  # a concurrent open won the race
        if open_case_for_payment(session, payment_id) is not None:
            raise CaseExistsError(f"Payment {payment_id} already has an open case.") from None
        raise exc
    payload = {
        "type": CaseType.PAYMENT_HOLD.value,
        "protocol": protocol.value,
        "decision": decision,
        "payment_id": str(payment_id),
        "guardian_session_id": str(guardian_session_id),
        "leading_hypothesis": leading,
        "sla_due_at": sla_due_at.isoformat(),
    }
    record(
        session,
        clock,
        actor_type=ActorType.ENGINE,
        actor_id=None,
        action="case.opened",
        entity_type="case",
        entity_id=str(case_id),
        payload=payload,
    )
    record_trace(  # docs/09 §5: the engine opened it, so no user
        session,
        case_id=case_id,
        user_id=None,
        type=TraceType.CASE_OPENED,
        at=now,
        payload={"protocol": protocol.value, "decision": decision},
    )
    if protocol is CaseProtocol.COERCION:  # docs/09 §6 item 2: drafted, never sent by itself
        draft_alert(session, clock, case_id=case_id, payment_id=payment_id)
    publish(session, EventType.CASE_OPENED, str(case_id), payload)
    opened = get_case(session, case_id)
    assert opened is not None
    return opened


def get_case(conn: Connection | Session, case_id: UUID) -> CaseRecord | None:
    row = conn.execute(select(cases).where(cases.c.id == case_id)).one_or_none()
    return CaseRecord.of(row) if row is not None else None


def open_case_for_payment(conn: Connection | Session, payment_id: UUID) -> CaseRecord | None:
    row = conn.execute(
        select(cases).where(
            cases.c.subject_payment_id == payment_id,
            cases.c.status != CaseStatus.DISPOSED.value,
        )
    ).one_or_none()
    return CaseRecord.of(row) if row is not None else None


@dataclass(frozen=True, slots=True)
class CaseCursor:
    """Where a page ended: the sort value (a time, or an expected loss) and the id of its last
    case."""

    at: datetime | float
    id: UUID


# The expected-loss sort key: the recommended decision's risk from `risk_by_decision` (#208).
# A case stored before #208 has none, so it sorts last (risks are never negative).
_NO_EXPECTED_LOSS = -1.0
EXPECTED_LOSS = func.coalesce(
    cast(cases.c.risk_by_decision.op("->>")(cases.c.recommended_decision), Float),
    _NO_EXPECTED_LOSS,
)


def sort_value(sort: CaseSort, record: CaseRecord) -> datetime | float:
    """The record's value for a sort order, as ``list_cases`` orders by it (for cursors)."""
    if sort is CaseSort.SLA_DUE_AT:
        return record.sla_due_at
    if sort is CaseSort.OPENED_AT:
        return record.opened_at
    loss = record.expected_loss_paise
    return _NO_EXPECTED_LOSS if loss is None else loss


def payer_is_vulnerable(session: Session, payment_id: UUID | None) -> bool:
    """The loss weighting of doc 06 §4: whether the payment's payer is a vulnerable customer."""
    if payment_id is None:
        return False
    payment = bank.get_payment(session, payment_id)
    if payment is None or payment.payer_customer_id is None:
        return False
    return bool(
        session.execute(
            select(bank.customers.c.is_vulnerable).where(
                bank.customers.c.id == payment.payer_customer_id
            )
        ).scalar_one_or_none()
    )


def list_cases(
    conn: Connection | Session,
    *,
    limit: int,
    sort: CaseSort = CaseSort.SLA_DUE_AT,
    after: CaseCursor | None = None,
    status: CaseStatus | None = None,
    case_type: CaseType | None = None,
    protocol: CaseProtocol | None = None,
) -> list[CaseRecord]:
    """The case queue, keyset-paged: soonest SLA first, the largest expected loss first, or
    newest first (docs/16 §1)."""
    key: Any
    if sort is CaseSort.SLA_DUE_AT:
        key, ascending = cases.c.sla_due_at, True
    elif sort is CaseSort.EXPECTED_LOSS:
        key, ascending = EXPECTED_LOSS, False
    else:
        key, ascending = cases.c.opened_at, False
    query = select(cases)
    if status is not None:
        query = query.where(cases.c.status == status.value)
    if case_type is not None:
        query = query.where(cases.c.type == case_type.value)
    if protocol is not None:
        query = query.where(cases.c.protocol == protocol.value)
    if after is not None:
        if ascending:
            query = query.where(or_(key > after.at, and_(key == after.at, cases.c.id > after.id)))
        else:
            query = query.where(or_(key < after.at, and_(key == after.at, cases.c.id < after.id)))
    order = (key.asc(), cases.c.id.asc()) if ascending else (key.desc(), cases.c.id.desc())
    return [CaseRecord.of(r) for r in conn.execute(query.order_by(*order).limit(limit))]
