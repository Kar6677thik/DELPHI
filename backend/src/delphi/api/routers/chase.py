"""Chase operations and liens (docs/16 "Chase", docs/10 §5, #113).

`POST /chase/operations` traces a complaint's (or the given payments') money and stores the
freeze candidates. `POST /chase/operations/{id}/liens` is the investigator's approval: the only
way a lien is placed or a CFCFRMS ticket sent (FR-CHS-04). `GET /chase/operations/{id}` returns
the operation with its candidates, liens and tickets.
"""

from __future__ import annotations

import json
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.chase.api import (
    CandidateSource,
    TracedOnly,
    approve_liens,
    cfcfrms_tickets,
    chase_candidates,
    chase_operations,
    close_operation,
    create_operation,
    liens,
    release_lien,
)
from delphi.platform.errors import NotFoundError
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["chase"])


class StartChase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complaint_id: UUID | None = None
    payment_ids: list[UUID] = Field(default_factory=list, max_length=50)


class ApproveLiens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[UUID] = Field(min_length=1)


class CandidateView(BaseModel):
    id: UUID
    account_id: UUID
    hop: int
    taint_paise: int
    balance_paise: int
    p_present: float | None
    p_mule: float | None
    erv_paise: int | None
    wrongful_cost_paise: int | None
    net_paise: int | None
    rank: int | None
    recommended: bool | None


class LienView(BaseModel):
    id: UUID
    candidate_id: UUID
    account_id: UUID
    bank_code: str
    amount_paise: int
    status: str
    recorded_reasons: str
    evidence_refs: list[str]
    approved_by: UUID
    approved_at: datetime
    placed_at: datetime | None
    review_due_at: datetime | None


class TicketView(BaseModel):
    id: UUID
    lien_id: UUID
    target_bank: str
    status: str
    sent_at: datetime | None
    acknowledged_at: datetime | None


class ChaseView(BaseModel):
    """docs/16 `ChaseView` (the graph and metrics come with #114's Chase screen work)."""

    id: UUID
    complaint_id: UUID | None
    source_payment_ids: list[UUID]
    status: str
    reported_at: datetime
    reported_loss_paise: int
    golden_hour_ends_at: datetime
    last_traced_at: datetime
    candidates: list[CandidateView]
    liens: list[LienView]
    tickets: list[TicketView]
    version: int


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _source(request: Request) -> CandidateSource:
    source: CandidateSource | None = getattr(request.app.state, "chase_candidates", None)
    return source if source is not None else TracedOnly()


def _view(session: Session, operation_id: UUID) -> ChaseView:
    op = session.execute(
        select(chase_operations).where(chase_operations.c.id == operation_id)
    ).one_or_none()
    if op is None:
        raise NotFoundError("No chase operation with this id.")
    cands = session.execute(
        select(chase_candidates)
        .where(chase_candidates.c.operation_id == operation_id)
        .order_by(chase_candidates.c.rank.nulls_last(), chase_candidates.c.taint_paise.desc())
    ).all()
    lien_rows = session.execute(
        select(liens).where(liens.c.operation_id == operation_id).order_by(liens.c.approved_at)
    ).all()
    tickets = session.execute(
        select(cfcfrms_tickets).where(cfcfrms_tickets.c.lien_id.in_([r.id for r in lien_rows]))
    ).all()
    return ChaseView(
        id=op.id,
        complaint_id=op.complaint_id,
        source_payment_ids=list(op.source_payment_ids),
        status=op.status,
        reported_at=op.reported_at,
        reported_loss_paise=op.reported_loss_paise,
        golden_hour_ends_at=op.golden_hour_ends_at,
        last_traced_at=op.last_traced_at,
        candidates=[CandidateView.model_validate(c, from_attributes=True) for c in cands],
        liens=[LienView.model_validate(r, from_attributes=True) for r in lien_rows],
        tickets=[TicketView.model_validate(t, from_attributes=True) for t in tickets],
        version=op.version,
    )


@router.post("/chase/operations", status_code=201, response_model=ChaseView)
def start_chase(
    *,
    principal: Annotated[Principal, Depends(require(Permission.CHASE_WRITE))],
    body: StartChase,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """docs/10 §1: trace a complaint's (or the payments') money; 422 if nothing executed."""
    platform = _platform(request)
    source = _source(request)

    def run(session: Session) -> tuple[int, Any]:
        op_id = create_operation(
            session,
            platform.clock,
            complaint_id=body.complaint_id,
            payment_ids=body.payment_ids,
            created_by=principal.user_id,
            source=source,
        )
        return HTTPStatus.CREATED, _view(session, op_id).model_dump(mode="json")

    reply = run_idempotent(request, raw, principal, key, run)
    if reply.status_code == HTTPStatus.CREATED:
        reply.headers["ETag"] = f'"{json.loads(bytes(reply.body))["version"]}"'
    return reply


@router.get("/chase/operations/{operation_id}")
def read_chase(
    operation_id: UUID,
    request: Request,
    response: Response,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> ChaseView:
    with _platform(request).sessions() as session:
        view = _view(session, operation_id)
    response.headers["ETag"] = f'"{view.version}"'
    return view


@router.post("/chase/operations/{operation_id}/liens", response_model=ChaseView)
def approve(
    operation_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.CHASE_WRITE))],
    body: ApproveLiens,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """FR-CHS-04: the investigator approves candidates; each lien is the taint still traced to
    the account, at most its balance now (FR-CHS-05). 409 for a closed operation or a
    candidate already liened; 422 for more than `max_liens_per_round`."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        approve_liens(
            session,
            platform.clock,
            operation_id,
            body.candidate_ids,
            approved_by=principal.user_id,
        )
        return HTTPStatus.OK, _view(session, operation_id).model_dump(mode="json")

    return run_idempotent(request, raw, principal, key, run)


class ReleaseLien(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


@router.post("/chase/operations/{operation_id}/liens/{lien_id}/release", response_model=ChaseView)
def release(
    operation_id: UUID,
    lien_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.CHASE_WRITE))],
    body: ReleaseLien,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """docs/05 §6: a PLACED lien is released, with the reason; 409 for any other status."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        release_lien(
            session, platform.clock, operation_id, lien_id, by=principal.user_id, reason=body.reason
        )
        return HTTPStatus.OK, _view(session, operation_id).model_dump(mode="json")

    return run_idempotent(request, raw, principal, key, run)


@router.post("/chase/operations/{operation_id}/close", response_model=ChaseView)
def close(
    operation_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.CHASE_WRITE))],
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """docs/10 §5 step 7: a manual close, at any time; 409 once closed."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        close_operation(session, platform.clock, operation_id, by=principal.user_id)
        return HTTPStatus.OK, _view(session, operation_id).model_dump(mode="json")

    return run_idempotent(request, raw, principal, key, run)
