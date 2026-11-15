"""Cases, read-only (docs/16 "Cases", #97): the case queue and one case.

The M4 stub serves what a case holds when it opens. The case console's fields (next best
evidence, WCMM, evidence records, timeline) come with #103. Cases are a mutable aggregate, so a
case carries `ETag: "<version>"` (docs/16 §1).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.model import UNEXPLAINED
from delphi.kb.store import get_version
from delphi.navigator.api import (
    CaseCursor,
    CaseProtocol,
    CaseRecord,
    CaseSort,
    CaseStatus,
    CaseType,
    get_case,
    list_cases,
)
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["cases"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1
PROBABILITY_DECIMALS = 6  # docs/16 §1
UNEXPLAINED_NAME = "Unexplained"  # the implicit open hypothesis has no KB file (docs/06 §2)


class CaseView(BaseModel):
    id: UUID
    type: CaseType
    protocol: CaseProtocol
    status: CaseStatus
    subject_payment_id: UUID | None
    subject_account_id: UUID | None
    guardian_session_id: UUID | None
    kb_version_id: int
    leading_hypothesis: str
    # The hypothesis's display name in the case's pinned KB (docs/07), for the queue and console.
    leading_hypothesis_name: str
    posterior: dict[str, float]
    prior_components: dict[str, Any]
    recommended_decision: str | None
    risk_by_decision: dict[str, Any] | None
    amount_at_stake_paise: int
    opened_at: datetime
    sla_due_at: datetime
    sla_breached: bool
    unexplained: bool
    dd_pack_complete: bool
    disposition: str | None
    disposed_at: datetime | None
    version: int

    @staticmethod
    def of(record: CaseRecord, now: datetime, names: Mapping[str, str]) -> CaseView:
        open_ = record.status is not CaseStatus.DISPOSED
        return CaseView(
            id=record.id,
            type=record.type,
            protocol=record.protocol,
            status=record.status,
            subject_payment_id=record.subject_payment_id,
            subject_account_id=record.subject_account_id,
            guardian_session_id=record.guardian_session_id,
            kb_version_id=record.kb_version_id,
            leading_hypothesis=record.leading_hypothesis,
            leading_hypothesis_name=names.get(record.leading_hypothesis, record.leading_hypothesis),
            posterior={k: round(v, PROBABILITY_DECIMALS) for k, v in record.posterior.items()},
            prior_components=dict(record.prior_components),
            recommended_decision=record.recommended_decision,
            risk_by_decision=dict(record.risk_by_decision) if record.risk_by_decision else None,
            amount_at_stake_paise=record.amount_at_stake_paise,
            opened_at=record.opened_at,
            sla_due_at=record.sla_due_at,
            sla_breached=open_ and record.sla_due_at <= now,
            unexplained=record.unexplained,
            dd_pack_complete=record.dd_pack_complete,
            disposition=record.disposition,
            disposed_at=record.disposed_at,
            version=record.version,
        )


class CasePage(BaseModel):
    items: list[CaseView]
    next_cursor: str | None


def _display_names(session: Session, records: Sequence[CaseRecord]) -> dict[int, dict[str, str]]:
    """Each pinned KB version's hypothesis display names (UNEXPLAINED is implicit, docs/06)."""
    names: dict[int, dict[str, str]] = {}
    for version_id in {r.kb_version_id for r in records}:
        version = get_version(session, version_id)
        known = version.kb.hypotheses if version is not None else {}
        names[version_id] = {
            UNEXPLAINED: UNEXPLAINED_NAME,
            **{k: h.display_name for k, h in known.items()},
        }
    return names


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _encode_cursor(sort: CaseSort, record: CaseRecord) -> str:
    at = record.sla_due_at if sort is CaseSort.SLA_DUE_AT else record.opened_at
    raw = json.dumps({"s": sort.value, "at": at.isoformat(), "id": str(record.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, sort: CaseSort) -> CaseCursor:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if raw["s"] != sort.value:
            raise ValueError("the cursor belongs to another sort order")
        at = datetime.fromisoformat(raw["at"])
        if at.tzinfo is None:
            raise ValueError("the cursor's time has no zone")
        return CaseCursor(at, UUID(raw["id"]))
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


@router.get("/cases")
def read_cases(
    *,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    status: Annotated[CaseStatus | None, Query()] = None,
    type: Annotated[CaseType | None, Query()] = None,
    protocol: Annotated[CaseProtocol | None, Query()] = None,
    sort: Annotated[CaseSort, Query()] = CaseSort.SLA_DUE_AT,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> CasePage:
    """The case queue: soonest SLA first (``sort=sla_due_at``), or newest first."""
    after = _decode_cursor(cursor, sort) if cursor else None
    platform = _platform(request)
    with platform.sessions() as session:
        records = list_cases(
            session,
            limit=limit + 1,
            sort=sort,
            after=after,
            status=status,
            case_type=type,
            protocol=protocol,
        )
        page, more = records[:limit], len(records) > limit
        names = _display_names(session, page)
    now = platform.clock.now()
    return CasePage(
        items=[CaseView.of(r, now, names[r.kb_version_id]) for r in page],
        next_cursor=_encode_cursor(sort, page[-1]) if more and page else None,
    )


@router.get("/cases/{case_id}")
def read_case(
    case_id: UUID,
    request: Request,
    response: Response,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> CaseView:
    platform = _platform(request)
    with platform.sessions() as session:
        record = get_case(session, case_id)
        if record is None:
            raise NotFoundError("No case with this id.")
        names = _display_names(session, [record])
    response.headers["ETag"] = f'"{record.version}"'
    return CaseView.of(record, platform.clock.now(), names[record.kb_version_id])
