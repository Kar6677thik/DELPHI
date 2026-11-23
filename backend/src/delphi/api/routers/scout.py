"""The Scout's unexplained queue (docs/16 "Scout", docs/11 §1, #117).

`GET /scout/queue` lists the items still in the queue; `POST /scout/queue/{item_id}/dismiss` is
an FDE engineer removing one, with a reason (audited).
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.store import active_version
from delphi.platform.errors import DelphiError, ProblemCode
from delphi.platform.wiring import Platform
from delphi.scout.api import QueueItem, dismiss, queue_items

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


@router.get("/scout/queue")
def read_queue(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> QueueView:
    with _platform(request).sessions() as session:
        return QueueView(items=[_item(i) for i in queue_items(session, _kb(session))])


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
    """docs/11 §1: 404 for an id not in the queue (already dismissed, or assigned)."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        item = dismiss(
            session, platform.clock, _kb(session), item_id, reason=body.reason, by=principal.user_id
        )
        return HTTPStatus.OK, _item(item).model_dump(mode="json")

    return run_idempotent(request, raw, principal, key, run)
