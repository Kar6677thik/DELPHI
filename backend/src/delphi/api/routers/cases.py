"""Cases (docs/16 "Cases"): the case queue (#97), one case with its console (#103), and the
disposition (#103).

`GET /cases/{id}` adds the console's decision support (docs/09 §2): the recommendation with
`risk_by_decision`, next best evidence, the evidence-sufficient flag and WCMM. Evidence records
and the timeline come with #102. Cases are a mutable aggregate, so a case carries
`ETag: "<version>"`, and the disposition needs a matching `If-Match` (docs/16 §1).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
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
    ConsoleView,
    Disposition,
    case_profile,
    console,
    dispose,
    get_case,
    list_cases,
    vulnerable_payer,
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
    # the disposition's details (docs/05 §4), null until DISPOSED
    disposition_typology: str | None = None
    disposition_confidence: int | None = None
    is_override: bool = False
    override_rationale: str | None = None
    disposed_by: UUID | None = None
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
            disposition_typology=record.disposition_typology,
            disposition_confidence=record.disposition_confidence,
            is_override=record.is_override,
            override_rationale=record.override_rationale,
            disposed_by=record.disposed_by,
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


class EvidenceRowView(BaseModel):
    """docs/09 §2 item 2: one evidence item, eligible ones ranked by net value."""

    source_key: str
    eligible: bool
    ineligible_reason: str | None
    net_value_paise: int | None
    cost_paise: int
    expected_minutes: float
    settle_pct: float | None
    pinned_by_protocol: bool


class FlipView(BaseModel):
    """docs/06 §12: the most likely single evidence outcome that would change the decision."""

    source_key: str
    outcome: str
    probability: float
    new_decision: str
    risk_change_paise: int


class CaseDetail(CaseView):
    """One case with the console's decision support (docs/09 §2, docs/16)."""

    recommended_decision: str
    risk_by_decision: dict[str, int]
    next_best_evidence: list[EvidenceRowView]
    evidence_sufficient: bool
    what_would_change_my_mind: FlipView | None


def _paise(value: float | None) -> int | None:
    return None if value is None else round(value)


def _detail(session: Session, record: CaseRecord, now: datetime) -> CaseDetail:
    names = _display_names(session, [record])[record.kb_version_id]
    base = CaseView.of(record, now, names)
    version = get_version(session, record.kb_version_id)
    if version is None:
        raise NotFoundError(f"No KB version {record.kb_version_id}.")
    view: ConsoleView = console(
        version.kb,
        case_profile(version.kb, record.type.value),
        posterior=record.posterior,
        amount_paise=record.amount_at_stake_paise,
        vulnerable=vulnerable_payer(session, record),
        protocol=record.protocol.value,
        observed=frozenset(),  # evidence records arrive with #102 (docs/09 §3)
    )
    # A DISPOSED case is immutable (docs/05 §4): it shows the recommendation and risks stored
    # at disposition, and offers no more evidence.
    disposed = record.status is CaseStatus.DISPOSED
    flip = None if disposed else view.what_would_change_my_mind
    return CaseDetail(
        **base.model_dump(exclude={"recommended_decision", "risk_by_decision"}),
        recommended_decision=(
            record.recommended_decision
            if disposed and record.recommended_decision
            else view.recommended_decision
        ),
        risk_by_decision={
            d: round(r)
            for d, r in (
                record.risk_by_decision
                if disposed and record.risk_by_decision
                else view.risk_by_decision
            ).items()
        },
        next_best_evidence=[
            EvidenceRowView(
                source_key=r.source_key,
                eligible=r.eligible,
                ineligible_reason=r.ineligible_reason,
                net_value_paise=_paise(r.net_value_paise),
                cost_paise=round(r.cost_paise),
                expected_minutes=r.expected_minutes,
                settle_pct=None if r.settle_pct is None else round(r.settle_pct, 1),
                pinned_by_protocol=r.pinned_by_protocol,
            )
            for r in view.next_best_evidence
            if not disposed  # a decided case asks for no more evidence
        ],
        evidence_sufficient=disposed or view.evidence_sufficient,
        what_would_change_my_mind=(
            FlipView(
                source_key=flip.source_key,
                outcome=flip.outcome,
                probability=round(flip.probability, PROBABILITY_DECIMALS),
                new_decision=flip.new_decision,
                risk_change_paise=round(flip.risk_change_paise),
            )
            if flip is not None
            else None
        ),
    )


@router.get("/cases/{case_id}")
def read_case(
    case_id: UUID,
    request: Request,
    response: Response,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> CaseDetail:
    platform = _platform(request)
    with platform.sessions() as session:
        record = get_case(session, case_id)
        if record is None:
            raise NotFoundError("No case with this id.")
        detail = _detail(session, record, platform.clock.now())
    response.headers["ETag"] = f'"{record.version}"'
    return detail


class DispositionRequest(BaseModel):
    """docs/16: `{decision, typology?, confidence (1–5), note?, override_rationale?}`."""

    decision: str
    typology: str | None = None
    confidence: int = Field(ge=1, le=5)
    note: str | None = Field(default=None, max_length=2000)
    override_rationale: str | None = Field(default=None, max_length=2000)


def _if_match(raw: str | None) -> int:
    if raw is None:
        raise DelphiError(
            "A disposition needs an If-Match header with the case's ETag.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    value = raw.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise DelphiError(
            "If-Match must be the ETag the server returned.", code=ProblemCode.VALIDATION_FAILED
        )
    return int(value)


@router.post("/cases/{case_id}/disposition", response_model=CaseDetail)
def dispose_case(
    case_id: UUID,
    *,
    # first: the permission is checked before the body and the headers are validated
    principal: Annotated[Principal, Depends(require(Permission.CASE_WRITE))],
    body: DispositionRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    """Decide the case (docs/05 §4, docs/09 §2): 412 `VERSION_CONFLICT` on a stale `If-Match`,
    409 once disposed, 422 for a decision outside the profile, a missing typology for
    CONFIRM_FRAUD, or an override without a rationale of 20+ characters."""
    platform = _platform(request)
    expected = _if_match(if_match)

    def run(session: Session) -> tuple[int, Any]:
        record = dispose(
            session,
            platform.clock,
            case_id,
            Disposition(
                decision=body.decision,
                confidence=body.confidence,
                typology=body.typology,
                note=body.note,
                override_rationale=body.override_rationale,
            ),
            expected_version=expected,
            user_id=principal.user_id,
        )
        return HTTPStatus.OK, _detail(session, record, platform.clock.now()).model_dump(mode="json")

    reply = run_idempotent(request, raw, principal, key, run)
    if reply.status_code == HTTPStatus.OK:  # docs/16 §1: a mutable aggregate carries its ETag
        reply.headers["ETag"] = f'"{json.loads(bytes(reply.body))["version"]}"'
    return reply
