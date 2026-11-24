"""The unexplained queue (docs/11 §1, FR-SCT-01, #117).

**Entry.** A decided Guardian session whose terminal posterior was flagged unexplained
(P(UNEXPLAINED) ≥ `unexplained_routing_threshold`, the session's `unexplained` flag), and a
disposed ACCOUNT_REVIEW case whose posterior at disposition is at least that threshold. A
PAYMENT_HOLD case is not a second item: its Guardian session already is.

**Exit.** An item leaves the queue when an FDE engineer dismisses it (with a reason, audited), or
when it belongs to a cluster whose typology draft was PROMOTED (it is assigned to that
typology). Cluster members are stored as ``{"subject_type", "subject_id"}`` refs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Text, cast, exists, func, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from delphi.guardian.api import unexplained_sessions_query
from delphi.kb.model import KnowledgeBase
from delphi.navigator.api import unexplained_reviews_query
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.scout.tables import scout_clusters, scout_queue_dismissals, typology_drafts

PROMOTED = "PROMOTED"


class SubjectType(StrEnum):
    GUARDIAN_SESSION = "GUARDIAN_SESSION"
    CASE = "CASE"


@dataclass(frozen=True, slots=True)
class QueueItem:
    subject_type: SubjectType
    subject_id: UUID
    entered_at: datetime
    p_unexplained: float
    payment_id: UUID | None = None
    account_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class QueueCursor:
    """The last item of a page (keyset: ``entered_at``, then ``subject_id``)."""

    entered_at: datetime
    subject_id: UUID


def _queue(kb: KnowledgeBase, *, since: datetime | None) -> Select[Any]:
    """The queue as one SQL query: both sources, less the dismissed items and the members of
    promoted clusters. Every caller bounds it (a page, a count, one item)."""
    threshold = kb.config.engine.unexplained_routing_threshold
    src = union_all(
        unexplained_sessions_query(since),
        unexplained_reviews_query(threshold=threshold, since=since),
    ).subquery("queue")
    d, c, t = scout_queue_dismissals.c, scout_clusters.c, typology_drafts.c
    dismissed = exists().where(
        d.subject_type == src.c.subject_type, d.subject_id == src.c.subject_id
    )
    ref = func.jsonb_build_array(
        func.jsonb_build_object(
            "subject_type", src.c.subject_type, "subject_id", cast(src.c.subject_id, Text)
        )
    )
    assigned = exists().where(
        t.cluster_id == c.id, t.status == PROMOTED, c.member_refs.contains(ref)
    )
    return select(src).where(~dismissed, ~assigned)


def _item(row: Any) -> QueueItem:
    return QueueItem(
        SubjectType(row.subject_type),
        row.subject_id,
        row.entered_at,
        float(row.p_unexplained),
        payment_id=row.payment_id,
        account_id=row.account_id,
    )


def queue_items(
    session: Session,
    kb: KnowledgeBase,
    *,
    limit: int,
    since: datetime | None = None,
    after: QueueCursor | None = None,
    newest_first: bool = False,
) -> list[QueueItem]:
    """At most ``limit`` queue items, oldest first (``newest_first``: the newest, newest
    first), after ``after``; ``since`` keeps the items that entered at or after it."""
    query = _queue(kb, since=since).subquery("q")
    rows = select(query)
    if after is not None:
        rows = rows.where(
            tuple_(query.c.entered_at, query.c.subject_id)
            > tuple_(after.entered_at, after.subject_id)
        )
    order = (query.c.entered_at, query.c.subject_id)
    rows = rows.order_by(*(c.desc() for c in order) if newest_first else order).limit(limit)
    return [_item(r) for r in session.execute(rows)]


def count_queue_items(session: Session, kb: KnowledgeBase, *, since: datetime | None) -> int:
    query = _queue(kb, since=since).subquery("q")
    return int(session.execute(select(func.count()).select_from(query)).scalar_one())


def queue_item(session: Session, kb: KnowledgeBase, item_id: UUID) -> QueueItem | None:
    query = _queue(kb, since=None).subquery("q")
    row = session.execute(select(query).where(query.c.subject_id == item_id).limit(1)).first()
    return _item(row) if row is not None else None


def dismiss(
    session: Session,
    clock: Clock,
    kb: KnowledgeBase,
    item_id: UUID,
    *,
    reason: str,
    by: UUID,
) -> QueueItem:
    """docs/11 §1: an FDE engineer removes an item from the queue, with a reason (audited). An
    id not in the queue is a 404; losing a race with a concurrent dismissal of the same item is
    a 409."""
    if not reason.strip():
        raise DelphiError("A dismissal needs a reason.", code=ProblemCode.VALIDATION_FAILED)
    item = queue_item(session, kb, item_id)
    if item is None:
        raise NotFoundError("No item with this id in the unexplained queue.")
    now = clock.now()
    inserted = session.execute(
        insert(scout_queue_dismissals)
        .values(
            subject_type=item.subject_type.value,
            subject_id=item.subject_id,
            reason=reason.strip(),
            dismissed_by=by,
            dismissed_at=now,
        )
        .on_conflict_do_nothing()
        .returning(scout_queue_dismissals.c.subject_id)
    ).first()
    if inserted is None:
        raise DelphiError(
            "This item was dismissed meanwhile.", code=ProblemCode.INVALID_STATE_TRANSITION
        )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(by),
        action="scout.queue.dismissed",
        entity_type=item.subject_type.value.lower(),
        entity_id=str(item.subject_id),
        payload={"reason": reason.strip()},
    )
    return item
