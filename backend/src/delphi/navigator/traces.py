"""Investigator trace events (docs/09 §5, FR-NAV-08, #108).

Every interaction with a case appends one ``trace_events`` row. The server writes the events
that are actions, in the action's own transaction: ``CASE_OPENED`` (``open_case``),
``EVIDENCE_REQUESTED`` (``request_evidence``: the source with the rank, net value and settle %
the console showed for it), ``DISPOSITION`` (``dispose``) and ``DD_PACK_VIEWED``. The console sends
the view events (``CLIENT_TYPES``) in batches through ``POST /cases/{id}/trace-events``; a client
can never write an action event, so the analytics' actions are the server's record.

The Learning page's trace analytics read these rows (docs/12 §6); nothing here aggregates them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.navigator.tables import cases, evidence_records, trace_events
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode


class TraceType(StrEnum):
    CASE_OPENED = "CASE_OPENED"
    CASE_VIEWED = "CASE_VIEWED"
    EVIDENCE_REQUESTED = "EVIDENCE_REQUESTED"
    EVIDENCE_VIEWED = "EVIDENCE_VIEWED"
    NARRATIVE_EXPANDED = "NARRATIVE_EXPANDED"
    WCMM_VIEWED = "WCMM_VIEWED"
    DISPOSITION = "DISPOSITION"
    DD_PACK_VIEWED = "DD_PACK_VIEWED"


# What the console may send: views only. The actions are written by the server.
CLIENT_TYPES = frozenset(
    {
        TraceType.CASE_VIEWED,
        TraceType.EVIDENCE_VIEWED,
        TraceType.NARRATIVE_EXPANDED,
        TraceType.WCMM_VIEWED,
    }
)
# The client events about one evidence record name it; the others carry nothing.
RECORD_TYPES = frozenset({TraceType.EVIDENCE_VIEWED, TraceType.NARRATIVE_EXPANDED})


@dataclass(frozen=True, slots=True)
class ClientTrace:
    """One view event from the console: its type, when the browser saw it, and the evidence
    record it is about (``RECORD_TYPES`` only)."""

    type: TraceType
    at: datetime
    evidence_record_id: UUID | None = None


def record_trace(
    session: Session,
    *,
    case_id: UUID,
    user_id: UUID | None,
    type: TraceType,
    at: datetime,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Append one trace event in the caller's transaction."""
    session.execute(
        trace_events.insert().values(
            case_id=case_id,
            user_id=user_id,
            type=type.value,
            payload=dict(payload or {}),
            at=at,
        )
    )


def _invalid(detail: str) -> DelphiError:
    return DelphiError(detail, code=ProblemCode.VALIDATION_FAILED)


def record_client_traces(
    session: Session,
    clock: Clock,
    case_id: UUID,
    user_id: UUID,
    events: Sequence[ClientTrace],
) -> int:
    """Store a batch of the console's view events (docs/09 §5). Every event is checked before
    any is written: only ``CLIENT_TYPES``; an evidence-record event names a record of this case,
    the others name none. A browser's clock may be off, so times are kept between the case's
    opening and now. Returns how many were stored."""
    opened = session.execute(
        select(cases.c.opened_at).where(cases.c.id == case_id)
    ).scalar_one_or_none()
    if opened is None:
        raise NotFoundError("No case with this id.")
    named = {e.evidence_record_id for e in events if e.evidence_record_id is not None}
    known = (
        set(
            session.execute(
                select(evidence_records.c.id).where(
                    evidence_records.c.case_id == case_id, evidence_records.c.id.in_(named)
                )
            ).scalars()
        )
        if named
        else set()
    )
    for e in events:
        if e.type not in CLIENT_TYPES:
            raise _invalid(f"{e.type} is recorded by the server, not sent by the console.")
        if e.type in RECORD_TYPES and e.evidence_record_id not in known:
            raise _invalid(f"{e.type} names an evidence record of this case.")
        if e.type not in RECORD_TYPES and e.evidence_record_id is not None:
            raise _invalid(f"{e.type} names no evidence record.")
    now = clock.now()
    for e in events:
        record_trace(
            session,
            case_id=case_id,
            user_id=user_id,
            type=e.type,
            at=max(opened, min(e.at, now)),
            payload=(
                {"evidence_record_id": str(e.evidence_record_id)}
                if e.evidence_record_id is not None
                else {}
            ),
        )
    return len(events)


def list_traces(session: Session, case_id: UUID) -> list[Mapping[str, Any]]:
    """A case's trace events, oldest first (for the DD pack and the tests)."""
    t = trace_events.c
    return [
        dict(r)
        for r in session.execute(
            select(t.type, t.user_id, t.payload, t.at)
            .where(t.case_id == case_id)
            .order_by(t.at, t.id)
        ).mappings()
    ]
