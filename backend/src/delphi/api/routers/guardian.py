"""Guardian sessions (docs/16 "Guardian", docs/08 §4, §6, #98).

Write side, for the mock phone: `POST /guardian/sessions` creates the payment and starts its
LIVE session, `.../probe-responses` answers the probe on screen, `.../cancel` cancels. They run
on the api's runner slot (`app.state.guardian_runner`); without it they answer 503
NOT_INITIALISED. The create and the answer take an `Idempotency-Key` (docs/16 §1).

Read side:

`GET /guardian/sessions` lists sessions newest first, keyset-paged, with the docs/16 filters;
`GET /guardian/sessions/{id}` returns the full `GuardianSessionView` with its probe runs and the
B1/B2 verdicts (FR-GDN-12), and `ETag: "<version>"`. `GET /guardian/sessions/{id}/dd-pack`
returns the session's due-diligence pack as JSON or a printable page (docs/09 §7, FR-GDN-15).
Live updates come over `/events/stream?topics=guardian` (docs/16 §3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent_async
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.engine import Ineligible
from delphi.guardian.api import (
    ClientSignals,
    LiveGuardian,
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
from delphi.sim.api import (
    PayChannel,
    PaymentRequest,
    PhoneTelemetry,
    initiate_payment,
    refuse_during_reset,
)

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


class ProbeCandidateView(BaseModel):
    """One row of a planning step's candidates table (docs/08 §6), as the Guardian stores it in
    `probe_runs.candidates` (`guardian.service._candidates`). Money is integer paise."""

    source_key: str
    eligible: bool
    ineligible_reason: Ineligible | None = None
    cost_paise: int | None = None
    voi_myopic_paise: int | None = None
    net_value_paise: int | None = None
    settle_pct: float | None = None  # "expected to settle X% of the remaining uncertainty"
    selected: bool


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
    candidates: list[ProbeCandidateView]
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
        candidates=[ProbeCandidateView.model_validate(c) for c in r.candidates or []],
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
    view = _view(_platform(request), session_id)
    response.headers["ETag"] = f'"{view.version}"'
    return view


def _view(platform: Platform, session_id: UUID) -> GuardianSessionView:
    with platform.sessions() as session:
        row = get_session_row(session, session_id)
        if row is None:
            raise NotFoundError("No Guardian session with this id.")
        runs = probe_run_rows(session, session_id)
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


# ---------------------------------------------------------------------- the write side (phone)

MAX_AMOUNT_PAISE = 10_000_000 * 100  # docs/19 §4: an amount is at most ₹10 crore
MAX_TEXT = 64  # docs/19 §4: bounded strings (keys, VPAs)
MAX_SESSION_S = 24 * 60 * 60.0  # an app session's durations are under a day
MAX_CLIENT_MS = 2**53 - 1  # a browser's Date.now() fits a JS safe integer
MAX_CLIENT_COUNT = 10_000  # far beyond what one probe screen can count

_Strict = ConfigDict(extra="forbid")
Count = Annotated[int, Field(ge=0, le=MAX_CLIENT_COUNT)]
Seconds = Annotated[float, Field(ge=0, le=MAX_SESSION_S, allow_inf_nan=False)]


class TelemetryIn(BaseModel):
    """The app session's signals, as the mobile SDK reports them (docs/08 §5)."""

    model_config = _Strict
    typing_cadence_z: Annotated[float, Field(allow_inf_nan=False)]
    paste_on_account_field: bool
    max_field_dwell_s: Seconds
    remote_access_active: bool
    screen_share_active: bool
    on_call: bool
    call_counterparty_known: bool | None = None
    session_duration_s: Seconds


class NewPayeeIn(BaseModel):
    """A payee the customer typed in: the bank resolves the VPA to its account."""

    model_config = _Strict
    vpa: str = Field(min_length=1, max_length=MAX_TEXT)


STORY_LOCAL_TIME = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # the migration 0019 check


class PaymentIn(BaseModel):
    model_config = _Strict
    payer_account_id: UUID
    payee_account_id: UUID | None = None
    new_payee: NewPayeeIn | None = None
    amount_paise: Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT_PAISE)]
    channel: PayChannel
    device_id: UUID
    telemetry: TelemetryIn | None = None
    # A demo story's scripted IST "HH:MM" (docs/14 §10): the DEMO_STORY job's result carries it.
    story_local_time: Annotated[str, Field(pattern=STORY_LOCAL_TIME)] | None = None

    @model_validator(mode="after")
    def _one_payee(self) -> PaymentIn:
        if (self.payee_account_id is None) == (self.new_payee is None):
            raise ValueError("give exactly one of payee_account_id and new_payee")
        return self


class StartSessionRequest(BaseModel):
    """docs/16: `POST /guardian/sessions`."""

    model_config = _Strict
    payment: PaymentIn


class OptionAnswer(BaseModel):
    model_config = _Strict
    option_key: str = Field(min_length=1, max_length=MAX_TEXT)


class OtpAnswer(BaseModel):
    model_config = _Strict
    otp: str = Field(min_length=1, max_length=MAX_TEXT)


class BiometricAnswer(BaseModel):
    model_config = _Strict
    biometric: Literal["MATCH", "NO_MATCH", "CANCEL"]


class CoolingOffIn(BaseModel):
    model_config = _Strict
    cancelled: bool
    other_channel_attempts: Count = 0
    limit_increase_requests: Count = 0
    send_now_presses: Count = 0


class CoolingOffAnswer(BaseModel):
    model_config = _Strict
    cooling_off: CoolingOffIn


class ClientSignalsIn(BaseModel):
    model_config = _Strict
    shown_at_ms: Annotated[int, Field(ge=0, le=MAX_CLIENT_MS)]
    answered_at_ms: Annotated[int, Field(ge=0, le=MAX_CLIENT_MS)]
    visibility_hidden_count: Count = 0
    paste_count: Count = 0


class ProbeResponseRequest(BaseModel):
    """docs/08 §4: one answer to the probe on the phone's screen."""

    model_config = _Strict
    probe_run_id: UUID
    answer: OptionAnswer | OtpAnswer | BiometricAnswer | CoolingOffAnswer
    client_signals: ClientSignalsIn


class ProbeResponseView(GuardianSessionView):
    """The session after the answer. ``attempts_remaining`` is set only for a wrong OTP with
    attempts left: the run still waits (docs/08 §4 OTP protocol step 3)."""

    attempts_remaining: int | None = None


def _live(request: Request) -> LiveGuardian:
    live: LiveGuardian | None = request.app.state.guardian_runner
    if live is None:
        raise DelphiError(
            "The Guardian is not running in this process yet.", code=ProblemCode.NOT_INITIALISED
        )
    return live


def _initiate(platform: Platform, payment: PaymentIn) -> UUID:
    """The payment, PENDING_GUARDIAN (docs/05 §2). Refused before anything is written when no
    KB is ACTIVE, so no payment is left without its session, and while a demo reset is queued
    or running (409)."""
    t = payment.telemetry
    with platform.sessions.begin() as session:
        if active_version(session) is None:
            raise DelphiError("No ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
        refuse_during_reset(session)  # a payment now would race the reset's deletes
        return initiate_payment(
            session,
            PaymentRequest(
                payer_account_id=payment.payer_account_id,
                amount_paise=payment.amount_paise,
                channel=payment.channel,
                device_id=payment.device_id,
                payee_account_id=payment.payee_account_id,
                payee_vpa=payment.new_payee.vpa if payment.new_payee else None,
                telemetry=PhoneTelemetry(**t.model_dump()) if t else None,
                story_local_time=payment.story_local_time,
            ),
            platform.clock.now(),
        )


def _with_etag(reply: JSONResponse) -> JSONResponse:
    if reply.status_code in {HTTPStatus.OK, HTTPStatus.CREATED}:  # docs/16 §1
        reply.headers["ETag"] = f'"{json.loads(bytes(reply.body))["version"]}"'
    return reply


@router.post(
    "/guardian/sessions",
    status_code=HTTPStatus.CREATED,
    response_model=GuardianSessionView,
)
async def start_session(
    *,
    principal: Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))],
    body: StartSessionRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """The customer pressed Pay on the mock phone (docs/08 §1): the payment is created
    PENDING_GUARDIAN and its LIVE session starts. The session comes back as it stands after
    steps 1–3 (decided already on the fast path); the rest reaches the phone over
    `/phone/stream` and the monitor over `/events/stream`. 503 NOT_INITIALISED while the
    Guardian does not run in this process, or no KB is ACTIVE."""
    live = _live(request)
    platform = _platform(request)

    async def run() -> tuple[int, Any]:
        payment_id = await asyncio.to_thread(_initiate, platform, body.payment)
        record = await live.start(payment_id)
        view = await asyncio.to_thread(_view, platform, record.id)
        return HTTPStatus.CREATED, view.model_dump(mode="json")

    return _with_etag(await run_idempotent_async(request, raw, principal, key, run))


@router.post(
    "/guardian/sessions/{session_id}/probe-responses",
    response_model=ProbeResponseView,
)
async def respond_to_probe(
    session_id: UUID,
    *,
    principal: Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))],
    body: ProbeResponseRequest,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """The phone's answer (docs/08 §4). 404 for a run that is not this session's, 409
    PROBE_NOT_AWAITING for one that no longer waits, 409 INVALID_STATE_TRANSITION past its
    deadline or for a session the simulation drives, 400 INVALID_CLIENT_SIGNALS for
    implausible timings, 422 for an answer the screen could not give."""
    live = _live(request)
    platform = _platform(request)
    s = body.client_signals
    signals = ClientSignals(
        s.shown_at_ms, s.answered_at_ms, s.visibility_hidden_count, s.paste_count
    )

    async def run() -> tuple[int, Any]:
        answer = body.answer.model_dump(mode="json")
        reply = await live.respond(session_id, body.probe_run_id, answer, signals)
        view = await asyncio.to_thread(_view, platform, session_id)
        out = ProbeResponseView(**view.model_dump(), attempts_remaining=reply.attempts_remaining)
        return HTTPStatus.OK, out.model_dump(mode="json")

    return _with_etag(await run_idempotent_async(request, raw, principal, key, run))


@router.post("/guardian/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: UUID,
    *,
    _principal: Annotated[Principal, Depends(require(Permission.DEMO_DRIVE))],
    request: Request,
    response: Response,
) -> GuardianSessionView:
    """The customer cancelled the payment (FR-GDN-07): the session ends CANCELLED_BY_CUSTOMER
    and the payment is CANCELLED. 409 for a finished session or one the simulation drives."""
    live = _live(request)
    await live.cancel(session_id)
    view = await asyncio.to_thread(_view, _platform(request), session_id)
    response.headers["ETag"] = f'"{view.version}"'
    return view
