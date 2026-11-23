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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.guardian.api import unexplained_sessions
from delphi.kb.model import KnowledgeBase
from delphi.navigator.api import unexplained_reviews
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


def _dismissed(session: Session) -> set[tuple[str, UUID]]:
    d = scout_queue_dismissals.c
    return {
        (r.subject_type, r.subject_id)
        for r in session.execute(select(d.subject_type, d.subject_id))
    }


def _assigned(session: Session) -> set[tuple[str, UUID]]:
    """Members of clusters whose draft was promoted to a typology."""
    c, t = scout_clusters.c, typology_drafts.c
    rows = session.execute(
        select(c.member_refs)
        .join(typology_drafts, t.cluster_id == c.id)
        .where(t.status == PROMOTED)
    )
    return {(ref["subject_type"], UUID(ref["subject_id"])) for (refs,) in rows for ref in refs}


def queue_items(
    session: Session, kb: KnowledgeBase, *, since: datetime | None = None
) -> list[QueueItem]:
    """The queue now, oldest first; ``since`` keeps the items that entered at or after it."""
    gone = _dismissed(session) | _assigned(session)
    items = [
        QueueItem(
            SubjectType.GUARDIAN_SESSION,
            s.id,
            s.decided_at,
            s.p_unexplained,
            payment_id=s.payment_id,
        )
        for s in unexplained_sessions(session, since=since)
    ]
    threshold = kb.config.engine.unexplained_routing_threshold
    items += [
        QueueItem(SubjectType.CASE, r.id, r.disposed_at, r.p_unexplained, account_id=r.account_id)
        for r in unexplained_reviews(session, threshold=threshold, since=since)
    ]
    items = [i for i in items if (i.subject_type.value, i.subject_id) not in gone]
    return sorted(items, key=lambda i: (i.entered_at, str(i.subject_id)))


def dismiss(
    session: Session,
    clock: Clock,
    kb: KnowledgeBase,
    item_id: UUID,
    *,
    reason: str,
    by: UUID,
) -> QueueItem:
    """docs/11 §1: an FDE engineer removes an item from the queue, with a reason (audited)."""
    if not reason.strip():
        raise DelphiError("A dismissal needs a reason.", code=ProblemCode.VALIDATION_FAILED)
    item = next((i for i in queue_items(session, kb) if i.subject_id == item_id), None)
    if item is None:
        raise NotFoundError("No item with this id in the unexplained queue.")
    now = clock.now()
    session.execute(
        scout_queue_dismissals.insert().values(
            subject_type=item.subject_type.value,
            subject_id=item.subject_id,
            reason=reason.strip(),
            dismissed_by=by,
            dismissed_at=now,
        )
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
