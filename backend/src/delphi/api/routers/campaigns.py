"""Campaigns (docs/16 "Cases", docs/09 §8, docs/05 §5, #107).

`GET /campaigns` lists the live campaigns (not merged, with cases), highest `rank_score` first,
with docs/16 keyset paging. `GET /campaigns/{id}` is one campaign with its cases and the
infrastructure entities they share (docs/09 §8). A campaign that merged is still found, with
`merged_into` naming the one that took its cases. Campaigns are derived (`CAMPAIGN_RECOMPUTE`),
so there is nothing to write here.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.navigator.api import (
    CampaignCase,
    campaign_cases,
    get_campaign,
    list_campaigns,
    member_cases,
)
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["campaigns"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1


class CampaignView(BaseModel):
    id: UUID
    leading_hypothesis: str
    case_count: int
    f3_stage_max: str | None
    expected_loss_paise: int
    vulnerable_victims: int
    rank_score: float
    merged_into: UUID | None
    created_at: datetime
    updated_at: datetime


class CampaignPage(BaseModel):
    items: list[CampaignView]
    next_cursor: str | None


class CampaignCaseView(BaseModel):
    id: UUID
    type: str
    status: str
    leading_hypothesis: str
    opened_at: datetime
    amount_at_stake_paise: int


class SharedEntity(BaseModel):
    kind: str  # "device", "account" or "ip24"
    value: str
    case_count: int


class CampaignDetail(CampaignView):
    cases: list[CampaignCaseView]
    shared_entities: list[SharedEntity]


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _encode_cursor(view: CampaignView) -> str:
    raw = json.dumps({"r": view.rank_score, "id": str(view.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, UUID]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        rank = raw["r"]
        if isinstance(rank, bool) or not isinstance(rank, int | float):
            raise ValueError("the cursor's rank is not a number")
        return float(rank), UUID(raw["id"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


@router.get("/campaigns")
def read_campaigns(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> CampaignPage:
    after = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        rows = list_campaigns(session, limit=limit + 1, after=after)
    views = [CampaignView.model_validate(r, from_attributes=True) for r in rows[:limit]]
    more = len(rows) > limit
    return CampaignPage(items=views, next_cursor=_encode_cursor(views[-1]) if more else None)


def _shared(members: list[CampaignCase]) -> list[SharedEntity]:
    """Entities at least two of the campaign's cases have (docs/09 §8), most shared first."""
    counts = Counter(e for m in members for e in m.facts.entities)
    shared = [(e, n) for e, n in counts.items() if n > 1]
    shared.sort(key=lambda en: (-en[1], en[0]))
    return [
        SharedEntity(kind=e.split(":", 1)[0], value=e.split(":", 1)[1], case_count=n)
        for e, n in shared
    ]


@router.get("/campaigns/{campaign_id}")
def read_campaign(
    campaign_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> CampaignDetail:
    with _platform(request).sessions() as session:
        row = get_campaign(session, campaign_id)
        if row is None:
            raise NotFoundError("No campaign with this id.")
        members = campaign_cases(session, campaign_id)
        case_rows = member_cases(session, campaign_id)
    view = CampaignView.model_validate(row, from_attributes=True)
    return CampaignDetail(
        **view.model_dump(),
        cases=[CampaignCaseView.model_validate(r, from_attributes=True) for r in case_rows],
        shared_entities=_shared(members),
    )
