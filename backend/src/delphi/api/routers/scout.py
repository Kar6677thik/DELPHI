"""The Scout's unexplained queue (docs/16 "Scout", docs/11 §1, #117).

`GET /scout/queue` lists the items still in the queue, oldest first, with docs/16 keyset paging
(`limit` ≤ 200, `cursor`, `{items, next_cursor}`); `POST /scout/queue/{item_id}/dismiss` is an
FDE engineer removing one, with a reason (audited).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.store import active_version
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.wiring import Platform
from delphi.scout.api import (
    QueueCursor,
    QueueItem,
    dismiss,
    queue_items,
    scout_clusters,
    typology_drafts,
)

router = APIRouter(prefix=API_PREFIX, tags=["scout"])


class QueueItemView(BaseModel):
    id: UUID
    subject_type: str
    entered_at: datetime
    p_unexplained: float
    payment_id: UUID | None
    account_id: UUID | None


class QueueView(BaseModel):
    items: list[QueueItemView]
    next_cursor: str | None


DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1


class Dismissal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _kb(session: Session) -> Any:
    version = active_version(session)
    if version is None:
        raise DelphiError("No ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
    return version.kb


def _item(i: QueueItem) -> QueueItemView:
    return QueueItemView(
        id=i.subject_id,
        subject_type=i.subject_type.value,
        entered_at=i.entered_at,
        p_unexplained=round(i.p_unexplained, 6),
        payment_id=i.payment_id,
        account_id=i.account_id,
    )


def _encode_keyset(at: datetime, item_id: UUID) -> str:
    raw = json.dumps({"at": at.isoformat(), "id": str(item_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _encode_cursor(item: QueueItem) -> str:
    return _encode_keyset(item.entered_at, item.subject_id)


def _decode_cursor(cursor: str) -> QueueCursor:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        at = datetime.fromisoformat(raw["at"])
        if at.tzinfo is None:
            raise ValueError("the cursor's time has no zone")
        return QueueCursor(at, UUID(raw["id"]))
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


@router.get("/scout/queue")
def read_queue(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> QueueView:
    after = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        items = queue_items(session, _kb(session), limit=limit + 1, after=after)
    page, more = items[:limit], len(items) > limit
    return QueueView(
        items=[_item(i) for i in page],
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


@router.post("/scout/queue/{item_id}/dismiss", response_model=QueueItemView)
def dismiss_item(
    item_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.FDE))],
    body: Dismissal,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """docs/11 §1: 404 for an id not in the queue (already dismissed, or assigned); 409 when a
    concurrent dismissal of the same item won."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        item = dismiss(
            session, platform.clock, _kb(session), item_id, reason=body.reason, by=principal.user_id
        )
        return HTTPStatus.OK, _item(item).model_dump(mode="json")

    return run_idempotent(request, raw, principal, key, run)


class RunRequested(BaseModel):
    job_id: UUID
    created: bool


class ClusterView(BaseModel):
    id: UUID
    run_id: UUID
    size: int
    member_refs: list[dict[str, str]]
    distinguishing_features: dict[str, Any]
    outcome_profile: dict[str, Any]
    created_at: datetime
    draft_id: UUID | None
    draft_status: str | None
    suggested_name: str | None


@router.post("/scout/runs", status_code=202, response_model=RunRequested)
def request_run(
    *,
    principal: Annotated[Principal, Depends(require(Permission.FDE))],
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """docs/11 §2: a manual Scout run, as a `RUN_SCOUT` job (docs/16 "→ job")."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        job = enqueue(
            session,
            platform.clock,
            JobType.RUN_SCOUT,
            idempotency_key=f"scout-manual:{key}",
            params={"trigger": "manual"},
            created_by=principal.user_id,
        )
        return HTTPStatus.ACCEPTED, {"job_id": str(job.job_id), "created": job.created}

    return run_idempotent(request, raw, principal, key, run)


class ClusterPage(BaseModel):
    items: list[ClusterView]
    next_cursor: str | None


@router.get("/scout/clusters")
def read_clusters(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ClusterPage:
    """The clusters of every run, newest first, each with its typology draft; docs/16 keyset
    paging (``limit`` ≤ 200, ``cursor``)."""
    c, t = scout_clusters.c, typology_drafts.c
    query = (
        select(scout_clusters, t.id.label("draft_id"), t.status, t.spec)
        .outerjoin(typology_drafts, t.cluster_id == c.id)
        .order_by(c.created_at.desc(), c.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        after = _decode_cursor(cursor)
        query = query.where(tuple_(c.created_at, c.id) < tuple_(after.entered_at, after.subject_id))
    with _platform(request).sessions() as session:
        rows = session.execute(query).all()
    page, more = rows[:limit], len(rows) > limit
    return ClusterPage(
        items=[
            ClusterView(
                id=r.id,
                run_id=r.run_id,
                size=r.size,
                member_refs=r.member_refs,
                distinguishing_features=r.distinguishing_features,
                outcome_profile=r.outcome_profile,
                created_at=r.created_at,
                draft_id=r.draft_id,
                draft_status=r.status,
                suggested_name=(r.spec or {}).get("name"),
            )
            for r in page
        ],
        next_cursor=_encode_keyset(page[-1].created_at, page[-1].id) if more and page else None,
    )
