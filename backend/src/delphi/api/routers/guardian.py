"""Guardian sessions, read side (docs/16 "Guardian", docs/08 §6, #98).

`GET /guardian/sessions` lists sessions newest first, keyset-paged, with the docs/16 filters;
`GET /guardian/sessions/{id}` returns the full `GuardianSessionView` with its probe runs and the
B1/B2 verdicts (FR-GDN-12), and `ETag: "<version>"`. `GET /guardian/sessions/{id}/dd-pack`
returns the session's due-diligence pack as JSON or a printable page (docs/09 §7, FR-GDN-15).
Live updates come over `/events/stream?topics=guardian` (docs/16 §3).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.guardian.api import (
    SessionCursor,
    SessionFilter,
    SessionMode,
    SessionRow,
    SessionStatus,
    build_dd_pack,
    get_session_row,
    list_sessions,
    probe_run_rows,
    render_dd_pack_html,
)
from delphi.kb.store import active_version
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["guardian"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1
PROBABILITY_DECIMALS = 6  # docs/16 §1


GUARDIAN_PROFILE = "GUARDIAN_PAYMENT"  # docs/07: the decision profile Guardian sessions use


class DdPackFormat(StrEnum):
    JSON = "json"
    HTML = "html"


class PaymentSummary(BaseModel):
    id: UUID
    payer_account_id: UUID
    payee_account_id: UUID
    amount_paise: int
    channel: str
    initiated_at: datetime
    device_id: UUID | None
    status: str


class PriorView(BaseModel):
    probs: dict[str, float] | None
    p_model: dict[str, float] | None
    anomaly_percentile: float | None
    open_mass: float | None
    top_features: list[dict[str, Any]]


class Baselines(BaseModel):
    """What B1 and B2 (docs/22 §2) would have done with this payment (docs/08 §6). B2's
    ``OTP_STEP_UP`` releases only if the OTP passes."""

    B1: str
    B2: str


class ProbeRunView(BaseModel):
    id: UUID
    seq: int
    probe_key: str
    mode: str
    status: str
    selected_by: str | None
    propensity: float | None
    outcome: str | None
    posterior_before: dict[str, float] | None
    posterior_after: dict[str, float] | None
    info_yield_bits: float | None
    voi_myopic_paise: int | None
    net_value_paise: int | None
    cost_paise: int | None
    candidates: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime | None


class GuardianSessionSummary(BaseModel):
    """A row of the monitor's list (docs/08 §6)."""

    id: UUID
    payment: PaymentSummary
    status: str
    decision: str | None
    terminal_reason: str | None
    kb_version_id: int
    leading_hypothesis: str | None
    posterior: dict[str, float] | None
    unexplained: bool
    baselines: Baselines | None
    started_at: datetime
    decided_at: datetime | None
    case_id: UUID | None
    dd_pack_complete: bool
    version: int


class GuardianSessionView(GuardianSessionSummary):
    """docs/16 `GuardianSessionView`."""

    prior: PriorView
    risk_by_decision: dict[str, int] | None
    probe_runs: list[ProbeRunView]


class GuardianSessionPage(BaseModel):
    items: list[GuardianSessionSummary]
    next_cursor: str | None


def _probs(values: Any) -> dict[str, float] | None:
    if values is None:
        return None
    return {k: round(float(v), PROBABILITY_DECIMALS) for k, v in values.items()}


def _summary(row: SessionRow) -> dict[str, Any]:
    posterior = _probs(row.posterior)
    p = row.payment
    return {
        "id": row.id,
        "payment": PaymentSummary(
            id=p.id,
            payer_account_id=p.payer_account_id,
            payee_account_id=p.payee_account_id,
            amount_paise=p.amount_paise,
            channel=p.channel,
            initiated_at=p.initiated_at,
            device_id=p.device_id,
            status=p.status,
        ),
        "status": row.status,
        "decision": row.decision,
        "terminal_reason": row.terminal_reason,
        "kb_version_id": row.kb_version_id,
        "leading_hypothesis": (
            max(sorted(posterior), key=lambda h: posterior[h]) if posterior else None
        ),
        "posterior": posterior,
        "unexplained": row.unexplained,
        "baselines": Baselines(**row.baselines) if row.baselines else None,
        "started_at": row.started_at,
        "decided_at": row.decided_at,
        "case_id": row.case_id,
        "dd_pack_complete": row.dd_pack_complete,
        "version": row.version,
    }


def _probe_run(r: Any) -> ProbeRunView:
    return ProbeRunView(
        id=r.id,
        seq=r.seq,
        probe_key=r.probe_key,
        mode=r.mode,
        status=r.status,
        selected_by=r.selected_by,
        propensity=r.propensity,
        outcome=r.outcome,
        posterior_before=_probs(r.posterior_before),
        posterior_after=_probs(r.posterior_after),
        info_yield_bits=r.info_yield_bits,
        voi_myopic_paise=r.voi_myopic_paise,
        net_value_paise=r.net_value_paise,
        cost_paise=r.cost_paise,
        candidates=list(r.candidates or []),
        started_at=r.started_at,
        completed_at=r.completed_at,
    )


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _encode_cursor(row: SessionRow) -> str:
    raw = json.dumps({"at": row.started_at.isoformat(), "id": str(row.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> SessionCursor:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not (isinstance(raw, dict) and isinstance(raw.get("at"), str)):
            raise ValueError("the cursor is not an object with a time")
        if not isinstance(raw.get("id"), str):
            raise ValueError("the cursor has no id")
        at = datetime.fromisoformat(raw["at"])
        if at.tzinfo is None:
            raise ValueError("the cursor's time has no zone")
        return SessionCursor(at, UUID(raw["id"]))
    except (binascii.Error, ValueError, KeyError, TypeError, AttributeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


def _check_decision(session: Any, decision: str | None) -> None:
    """Decisions come from the KB's decision profile (docs/07), not a fixed list."""
    if decision is None:
        return
    active = active_version(session)
    allowed = (
        active.kb.decision_profiles[GUARDIAN_PROFILE].decisions
        if active is not None and GUARDIAN_PROFILE in active.kb.decision_profiles
        else ()
    )
    if decision not in allowed:
        raise DelphiError(
            f"`decision` must be one of {list(allowed)}.", code=ProblemCode.VALIDATION_FAILED
        )


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise DelphiError(
            f"`{name}` must be an RFC 3339 time with a zone.", code=ProblemCode.VALIDATION_FAILED
        )
    return value


@router.get("/guardian/sessions")
def read_sessions(
    *,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    status: Annotated[SessionStatus | None, Query()] = None,
    decision: Annotated[
        str | None, Query(description="A decision of the ACTIVE KB's profile")
    ] = None,
    unexplained: Annotated[bool | None, Query()] = None,
    mode: Annotated[SessionMode | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> GuardianSessionPage:
    """The monitor's list: newest first. ``from`` is inclusive and ``to`` exclusive, on the
    session's start."""
    with _platform(request).sessions() as session:
        _check_decision(session, decision)
    where = SessionFilter(
        status=status.value if status else None,
        decision=decision,
        unexplained=unexplained,
        mode=mode.value if mode else None,
        started_from=_aware(from_, "from"),
        started_to=_aware(to, "to"),
    )
    after = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        rows = list_sessions(session, limit=limit + 1, after=after, where=where)
    page, more = rows[:limit], len(rows) > limit
    return GuardianSessionPage(
        items=[GuardianSessionSummary(**_summary(r)) for r in page],
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


@router.get("/guardian/sessions/{session_id}")
def read_session(
    session_id: UUID,
    request: Request,
    response: Response,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> GuardianSessionView:
    with _platform(request).sessions() as session:
        row = get_session_row(session, session_id)
        if row is None:
            raise NotFoundError("No Guardian session with this id.")
        runs = probe_run_rows(session, session_id)
    response.headers["ETag"] = f'"{row.version}"'
    return GuardianSessionView(
        **_summary(row),
        prior=PriorView(
            probs=_probs(row.prior),
            p_model=_probs(row.p_model),
            anomaly_percentile=row.anomaly_percentile,
            open_mass=row.open_mass,
            top_features=[dict(f) for f in row.top_features],
        ),
        risk_by_decision=(
            {d: round(v) for d, v in row.risk_by_decision.items()} if row.risk_by_decision else None
        ),
        probe_runs=[_probe_run(r) for r in runs],
    )


@router.get(
    "/guardian/sessions/{session_id}/dd-pack",
    responses={200: {"content": {"text/html": {}}}},
)
def read_dd_pack(
    session_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
    format: Annotated[DdPackFormat, Query()] = DdPackFormat.JSON,
) -> Any:
    """The session's due-diligence pack (docs/09 §7): JSON, or a printable HTML page."""
    with _platform(request).sessions() as session:
        pack = build_dd_pack(session, session_id)
    if format is DdPackFormat.HTML:
        return HTMLResponse(render_dd_pack_html(pack))
    return pack.as_json()
