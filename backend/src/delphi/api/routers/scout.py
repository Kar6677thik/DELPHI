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

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, true, tuple_
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.problems import problem
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.store import active_version
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.wiring import Platform
from delphi.scout.api import (
    DraftInvalidError,
    DraftPatch,
    DraftStatus,
    QueueCursor,
    QueueItem,
    dismiss,
    edit_draft,
    get_draft,
    list_drafts,
    queue_items,
    scout_clusters,
    stored_spec,
    typology_drafts,
    validate_draft,
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
    """The clusters of every run, newest first, each with its latest typology draft; docs/16
    keyset paging (``limit`` ≤ 200, ``cursor``), on the ``(created_at, id)`` index."""
    c, t = scout_clusters.c, typology_drafts.c
    draft = (  # a cluster's latest draft, so a cluster with several drafts appears once
        select(t.id.label("draft_id"), t.status, t.spec)
        .where(t.cluster_id == c.id)
        .order_by(t.updated_at.desc(), t.id.desc())
        .limit(1)
        .lateral("draft")
    )
    query = (
        select(scout_clusters, draft.c.draft_id, draft.c.status, draft.c.spec)
        .outerjoin(draft, true())
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


# --------------------------------------------------------------------------- typology drafts


class DraftIssue(BaseModel):
    path: str
    code: str
    message: str


class DraftView(BaseModel):
    id: UUID
    cluster_id: UUID | None
    status: str
    revision: int
    spec: dict[str, Any]
    editors: list[UUID]
    last_edited_by: UUID | None
    backtest_report: dict[str, Any] | None
    backtested_revision: int | None
    created_at: datetime
    updated_at: datetime
    version: int
    # The current spec checked against ACTIVE + the draft (docs/11 §3): what still stops it
    # being backtested. A CANDIDATE lacks at least a key and a colour.
    issues: list[DraftIssue]


class DraftPage(BaseModel):
    items: list[DraftView]
    next_cursor: str | None


def _draft_view(row: Any, kb: Any) -> DraftView:
    issues = validate_draft(kb, stored_spec(row))
    return DraftView(
        id=row.id,
        cluster_id=row.cluster_id,
        status=row.status,
        revision=row.revision,
        spec=row.spec,
        editors=list(row.editors),
        last_edited_by=row.last_edited_by,
        backtest_report=row.backtest_report,
        backtested_revision=row.backtested_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        issues=[DraftIssue(path=i.path, code=i.code, message=i.message) for i in issues],
    )


def _with_etag(view: DraftView) -> JSONResponse:
    return JSONResponse(view.model_dump(mode="json"), headers={"ETag": f'"{view.version}"'})


def _if_match(raw: str | None) -> int:
    if raw is None:
        raise DelphiError(
            "An edit needs an If-Match header with the draft's ETag.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    value = raw.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise DelphiError(
            "If-Match must be the ETag the server returned.", code=ProblemCode.VALIDATION_FAILED
        )
    return int(value)


@router.get("/typology-drafts")
def read_drafts(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    status: Annotated[DraftStatus, Query()] = DraftStatus.CANDIDATE,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> DraftPage:
    """The drafts of one status (CANDIDATE unless asked, the owner's decision on #255), most
    recently edited first; keyset paging (docs/16 §1)."""
    after = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        kb = _kb(session)
        rows = list_drafts(
            session,
            limit=limit + 1,
            status=status,
            after=(after.entered_at, after.subject_id) if after else None,
        )
        views = [_draft_view(r, kb) for r in rows[:limit]]
    more = len(rows) > limit
    return DraftPage(
        items=views,
        next_cursor=_encode_keyset(views[-1].updated_at, views[-1].id) if more else None,
    )


@router.get("/typology-drafts/{draft_id}", response_model=DraftView)
def read_draft(
    draft_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> JSONResponse:
    with _platform(request).sessions() as session:
        return _with_etag(_draft_view(get_draft(session, draft_id), _kb(session)))


@router.patch("/typology-drafts/{draft_id}", response_model=DraftView)
def patch_draft(
    draft_id: UUID,
    body: DraftPatch,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.FDE))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    """docs/11 §3: a partial edit, validated as ACTIVE + the draft; the next revision, back to
    DRAFT. 412 on a stale If-Match; 422 with `errors` when it breaks KB rules (nothing saved);
    409 for a draft in shadow, promoted or rejected."""
    expected = _if_match(if_match)
    platform = _platform(request)
    try:
        with platform.sessions.begin() as session:
            kb = _kb(session)
            row = edit_draft(
                session,
                platform.clock,
                kb,
                draft_id,
                body,
                expected_version=expected,
                by=principal.user_id,
            )
            view = _draft_view(row, kb)
    except DraftInvalidError as err:
        return problem(
            request,
            ProblemCode.VALIDATION_FAILED,
            err.detail,
            errors=[{"path": i.path, "code": i.code, "message": i.message} for i in err.issues],
        )
    return _with_etag(view)
