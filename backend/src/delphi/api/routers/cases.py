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
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.model import UNEXPLAINED
from delphi.kb.store import get_version
from delphi.navigator.api import (
    MAX_TRACE_BATCH,
    AlertAction,
    AlertStatus,
    CaseCursor,
    CaseProtocol,
    CaseRecord,
    CaseSort,
    CaseStatus,
    CaseType,
    ClientTrace,
    ConsoleView,
    Disposition,
    EvidenceRecord,
    TraceType,
    act_on_alert,
    case_profile,
    console,
    current_records,
    dispose,
    get_alert,
    get_case,
    list_cases,
    record_client_traces,
    request_evidence,
    request_simulated_outreach,
    sort_value,
    vulnerable_payer,
)
from delphi.navigator.api import (
    OUTREACH as OUTREACH_SOURCE,
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
    value = sort_value(sort, record)
    at = value.isoformat() if isinstance(value, datetime) else value
    raw = json.dumps({"s": sort.value, "at": at, "id": str(record.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, sort: CaseSort) -> CaseCursor:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if raw["s"] != sort.value:
            raise ValueError("the cursor belongs to another sort order")
        if sort is CaseSort.EXPECTED_LOSS:  # a number (#208)
            value = raw["at"]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("the cursor's expected loss is not a number")
            return CaseCursor(float(value), UUID(raw["id"]))
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
    """The case queue: soonest SLA first (``sort=sla_due_at``), the largest expected loss first
    (``expected_loss``, #208), or newest first (``opened_at``)."""
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


class EvidenceRecordView(BaseModel):
    """docs/09 §2 item 5: one fetched evidence record (an evidence card)."""

    id: UUID
    source_key: str
    outcome: str
    facts: dict[str, Any]
    record_refs: list[str]
    posterior_before: dict[str, float]
    posterior_after: dict[str, float]
    info_yield_bits: float
    rank_at_request: int | None
    net_value_at_request_paise: int | None
    fetch_ms: int
    requested_by: UUID | None
    requested_at: datetime
    narrative: str | None
    narrative_source: str | None

    @staticmethod
    def of(r: EvidenceRecord) -> EvidenceRecordView:
        def probs(values: Mapping[str, float]) -> dict[str, float]:
            return {k: round(v, PROBABILITY_DECIMALS) for k, v in values.items()}

        return EvidenceRecordView(
            id=r.id,
            source_key=r.source_key,
            outcome=r.outcome,
            facts=dict(r.raw),
            record_refs=list(r.record_refs),
            posterior_before=probs(r.posterior_before),
            posterior_after=probs(r.posterior_after),
            info_yield_bits=round(r.info_yield_bits, PROBABILITY_DECIMALS),
            rank_at_request=r.rank_at_request,
            net_value_at_request_paise=r.net_value_at_request_paise,
            fetch_ms=r.fetch_ms,
            requested_by=r.requested_by,
            requested_at=r.requested_at,
            narrative=r.narrative,
            narrative_source=r.narrative_source,
        )


class PoliceAlertView(BaseModel):
    """docs/09 §6: the coercion case's police alert draft and what a human did with it."""

    id: UUID
    status: AlertStatus
    payload: dict[str, Any]
    acted_by: UUID | None
    acted_at: datetime | None


class CaseDetail(CaseView):
    """One case with the console's decision support (docs/09 §2, docs/16)."""

    recommended_decision: str
    risk_by_decision: dict[str, int]
    next_best_evidence: list[EvidenceRowView]
    evidence_sufficient: bool
    what_would_change_my_mind: FlipView | None
    evidence_records: list[EvidenceRecordView]
    police_alert: PoliceAlertView | None


def _paise(value: float | None) -> int | None:
    return None if value is None else round(value)


def _detail(session: Session, record: CaseRecord, now: datetime) -> CaseDetail:
    names = _display_names(session, [record])[record.kb_version_id]
    base = CaseView.of(record, now, names)
    version = get_version(session, record.kb_version_id)
    if version is None:
        raise NotFoundError(f"No KB version {record.kb_version_id}.")
    records = current_records(session, record.id)
    view: ConsoleView = console(
        version.kb,
        case_profile(version.kb, record.type.value),
        posterior=record.posterior,
        amount_paise=record.amount_at_stake_paise,
        vulnerable=vulnerable_payer(session, record),
        protocol=record.protocol.value,
        observed=frozenset(r.source_key for r in records),
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
        evidence_records=[EvidenceRecordView.of(r) for r in records],
        police_alert=(
            PoliceAlertView(
                id=alert.id,
                status=alert.status,
                payload=dict(alert.payload),
                acted_by=alert.acted_by,
                acted_at=alert.acted_at,
            )
            if (alert := get_alert(session, record.id)) is not None
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


# A KB key (a decision, typology, source or outcome): bounded, so a request cannot carry an
# arbitrarily long string into the audit log and the problem detail (#127).
KbKey = Annotated[str, Field(min_length=1, max_length=64)]


class DispositionRequest(BaseModel):
    """docs/16: `{decision, typology?, confidence (1–5), note?, override_rationale?}`."""

    decision: KbKey
    typology: KbKey | None = None
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


class EvidenceRequest(BaseModel):
    """docs/16: `{source_key, refresh?}`; `CUSTOMER_OUTREACH_CALL` also takes the investigator's
    recorded `outcome` and a `note` (docs/09 §3)."""

    source_key: KbKey
    refresh: bool = False
    outcome: KbKey | None = None
    note: str | None = Field(default=None, max_length=2000)
    # docs/09 §3: in simulation/autoplay the worker places the outreach call (202).
    simulate: bool = False


@router.post("/cases/{case_id}/evidence-requests", response_model=CaseDetail)
def request_case_evidence(
    case_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.CASE_WRITE))],
    body: EvidenceRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """Fetch one evidence item (docs/09 §3): the adapter's facts, the outcome, and the case
    posterior updated. Idempotent per (case, source) unless `refresh`; 409 once disposed; 422
    for a source outside the case's profile or an outreach call without its recorded outcome."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        if body.simulate:
            if body.source_key != OUTREACH_SOURCE:
                raise DelphiError(
                    "Only CUSTOMER_OUTREACH_CALL can be simulated.",
                    code=ProblemCode.VALIDATION_FAILED,
                )
            request_simulated_outreach(
                session,
                platform.clock,
                case_id,
                requested_by=principal.user_id,
                simulator=platform.settings.simulation_enabled,
            )
            record = get_case(session, case_id)
            assert record is not None
            detail = _detail(session, record, platform.clock.now())
            return HTTPStatus.ACCEPTED, detail.model_dump(mode="json")
        request_evidence(
            session,
            platform.clock,
            case_id,
            body.source_key,
            requested_by=principal.user_id,
            refresh=body.refresh,
            recorded_outcome=body.outcome,
            note=body.note,
        )
        record = get_case(session, case_id)
        assert record is not None
        return HTTPStatus.OK, _detail(session, record, platform.clock.now()).model_dump(mode="json")

    reply = run_idempotent(request, raw, principal, key, run)
    if reply.status_code in {HTTPStatus.OK, HTTPStatus.ACCEPTED}:
        reply.headers["ETag"] = f'"{json.loads(bytes(reply.body))["version"]}"'
    return reply


@router.post("/cases/{case_id}/police-alert/{action}", response_model=CaseDetail)
def act_on_police_alert(
    case_id: UUID,
    action: AlertAction,
    *,
    principal: Annotated[Principal, Depends(require(Permission.CASE_WRITE))],
    request: Request,
    response: Response,
) -> CaseDetail:
    """Send (recorded as SENT_SIMULATED, with the case's evidence list) or discard the coercion
    case's police alert draft (docs/09 §6). Only a human call does this: nothing sends an alert
    automatically (FR-NAV-10). 404 for a case without one, 409 once it was acted on. The case
    comes back with its new version as its `ETag` (docs/16 §1)."""
    platform = _platform(request)
    with platform.sessions.begin() as session:
        record = get_case(session, case_id)
        if record is None:
            raise NotFoundError("No case with this id.")
        evidence = [
            {"source_key": r.source_key, "outcome": r.outcome, "at": r.requested_at.isoformat()}
            for r in current_records(session, case_id)
        ]
        act_on_alert(
            session,
            platform.clock,
            case_id,
            action,
            user_id=principal.user_id,
            evidence=evidence,
        )
        acted = get_case(session, case_id)  # the new version
        assert acted is not None
        response.headers["ETag"] = f'"{acted.version}"'  # a mutable aggregate carries its ETag
        return _detail(session, acted, platform.clock.now())


class TracePayload(BaseModel):
    """What a view event may carry: the evidence record it is about, for EVIDENCE_VIEWED and
    NARRATIVE_EXPANDED. Nothing else is stored from a browser."""

    model_config = {"extra": "forbid"}

    evidence_record_id: UUID | None = None


class TraceEventIn(BaseModel):
    model_config = {"extra": "forbid"}

    type: TraceType
    payload: TracePayload = Field(default_factory=TracePayload)
    at: AwareDatetime  # docs/16: times are UTC (ADR-0014); a zone-less time is a 422


class TraceBatch(BaseModel):
    model_config = {"extra": "forbid"}

    events: list[TraceEventIn] = Field(min_length=1, max_length=MAX_TRACE_BATCH)


class TraceBatchResult(BaseModel):
    recorded: int


@router.post("/cases/{case_id}/trace-events", response_model=TraceBatchResult)
def post_trace_events(
    case_id: UUID,
    body: TraceBatch,
    *,
    principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    request: Request,
) -> TraceBatchResult:
    """The console's view events, batched (docs/09 §5, FR-NAV-08): CASE_VIEWED,
    EVIDENCE_VIEWED, NARRATIVE_EXPANDED and WCMM_VIEWED. The server records the actions itself,
    so any other type is 422, as is an evidence-record event naming a record of another case;
    the whole batch is then refused. 404 for an unknown case."""
    platform = _platform(request)
    events = [
        ClientTrace(type=e.type, at=e.at, evidence_record_id=e.payload.evidence_record_id)
        for e in body.events
    ]
    with platform.sessions.begin() as session:
        recorded = record_client_traces(session, platform.clock, case_id, principal.user_id, events)
    return TraceBatchResult(recorded=recorded)
