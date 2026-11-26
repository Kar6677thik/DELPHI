"""The question bank's in-app route (docs/16 "KB", docs/07 §9, FR-KB-05, #110): draft variants
with the LLM, review them, and bundle the approved ones into a CANDIDATE KB version."""

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
from sqlalchemy.orm import Session

from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.kb.model import Language
from delphi.kb.question_variants import (
    LANGUAGES,
    Decision,
    VariantRecord,
    VariantStatus,
    approve_batch,
    canonical_variant,
    get_variant,
    list_variants,
    review,
)
from delphi.kb.store import active_version
from delphi.llm.schemas import VARIANT_MAX_CHARS
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES

router = APIRouter(prefix=API_PREFIX, tags=["kb"])

Viewer = Annotated[Principal, Depends(require(Permission.VIEW))]
Fde = Annotated[Principal, Depends(require(Permission.FDE))]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class OptionView(BaseModel):
    key: str
    text: str


class OriginalView(BaseModel):
    """The ACTIVE KB's canonical English variant, shown beside a draft (docs/18 §2)."""

    variant_id: str
    text: str
    options: list[OptionView]


class VariantView(BaseModel):
    id: UUID
    probe_key: str
    language: Language
    text: str
    options: list[OptionView]
    status: VariantStatus
    generated_by: str
    llm_call_id: UUID | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    included_in_kb_version: int | None
    created_at: datetime
    original: OriginalView | None = Field(
        description="the ACTIVE KB's English variant 1 of the probe; null if it has none"
    )

    @staticmethod
    def of(record: VariantRecord, original: OriginalView | None) -> VariantView:
        return VariantView(
            id=record.id,
            probe_key=record.probe_key,
            language=record.language,
            text=record.text,
            options=[OptionView(**o) for o in record.options],
            status=record.status,
            generated_by=record.generated_by.value,
            llm_call_id=record.llm_call_id,
            reviewed_by=record.reviewed_by,
            reviewed_at=record.reviewed_at,
            included_in_kb_version=record.included_in_kb_version,
            created_at=record.created_at,
            original=original,
        )


class VariantPage(BaseModel):
    items: list[VariantView]
    next_cursor: str | None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe_key: str = Field(min_length=1, max_length=64)
    languages: list[Language] = Field(min_length=1, max_length=len(LANGUAGES))


class GenerateQueued(BaseModel):
    job_id: UUID
    created: bool


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    edited_text: str | None = Field(
        default=None,
        max_length=VARIANT_MAX_CHARS,
        description="APPROVE only: the question text as approved, replacing the draft's",
    )


class BatchView(BaseModel):
    kb_version_id: int
    variant_ids: list[UUID]


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _originals(session: Session, probe_keys: set[str]) -> dict[str, OriginalView]:
    active = active_version(session)
    if active is None:
        return {}
    found: dict[str, OriginalView] = {}
    for key in probe_keys:
        try:
            v = canonical_variant(active.kb, key)
        except NotFoundError:
            continue
        found[key] = OriginalView(
            variant_id=v.variant_id,
            text=v.text,
            options=[OptionView(key=o.key, text=o.text) for o in v.options],
        )
    return found


def _encode_cursor(record: VariantRecord) -> str:
    raw = json.dumps({"at": record.created_at.isoformat(), "id": str(record.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        at = datetime.fromisoformat(raw["at"])
        if at.tzinfo is None:
            raise ValueError("the cursor's time has no zone")
        return at, UUID(raw["id"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


@router.get("/question-bank/variants")
def read_variants(
    *,
    request: Request,
    _principal: Viewer,
    status: Annotated[VariantStatus | None, Query()] = None,
    probe_key: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> VariantPage:
    """Drafted variants, oldest first, each with the ACTIVE KB's English original (docs/18 §2).
    ``status=PENDING_REVIEW`` is the review queue."""
    after = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        records = list_variants(
            session, status=status, probe_key=probe_key, limit=limit + 1, after=after
        )
        page, more = records[:limit], len(records) > limit
        originals = _originals(session, {r.probe_key for r in page})
    return VariantPage(
        items=[VariantView.of(r, originals.get(r.probe_key)) for r in page],
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


@router.post("/question-bank/generate", status_code=202, response_model=GenerateQueued)
def generate_variants(
    body: GenerateRequest,
    *,
    principal: Fde,
    raw: RawBody,
    key: IdempotencyKey,
    request: Request,
) -> JSONResponse:
    """Queue an ``LLM_QUESTION_VARIANTS`` job: one draft per language for the probe (docs/15
    §1). 404 when the ACTIVE KB's question bank has no such probe."""
    platform = _platform(request)
    languages = list(dict.fromkeys(body.languages))

    def run(session: Session) -> tuple[int, Any]:
        active = active_version(session)
        if active is None:
            raise DelphiError("There is no ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
        canonical_variant(active.kb, body.probe_key)
        job = enqueue(
            session,
            platform.clock,
            JobType.LLM_QUESTION_VARIANTS,
            idempotency_key=f"LLM_QUESTION_VARIANTS:api:{key}",
            params={"probe_key": body.probe_key, "languages": languages},
            created_by=principal.user_id,
        )
        return HTTPStatus.ACCEPTED, {"job_id": str(job.job_id), "created": job.created}

    return run_idempotent(request, raw, principal, key, run)


@router.post("/question-bank/variants/{variant_id}/review")
def review_variant(
    variant_id: UUID, body: ReviewRequest, *, principal: Fde, request: Request
) -> VariantView:
    """APPROVE (optionally with ``edited_text``) or REJECT a PENDING_REVIEW variant, audited.
    404 for an unknown variant, 409 once it was reviewed, 422 for an edit on a rejection or an
    edit with a link or a phone number (docs/15 §5)."""
    platform = _platform(request)
    with platform.sessions.begin() as session:
        reviewed = review(
            session,
            platform.clock,
            variant_id,
            body.decision,
            reviewer=principal.user_id,
            edited_text=body.edited_text,
        )
        originals = _originals(session, {reviewed.probe_key})
    return VariantView.of(reviewed, originals.get(reviewed.probe_key))


@router.post("/question-bank/approve-batch", status_code=201, response_model=BatchView)
def approve_variants(
    *, principal: Fde, raw: RawBody, key: IdempotencyKey, request: Request
) -> JSONResponse:
    """docs/07 §9 "Approve batch": every APPROVED variant not yet included goes into one new
    CANDIDATE KB version (parent = ACTIVE), for the KB lifecycle to promote (docs/07 §1). 409
    when none is waiting; 422 ``KB_VALIDATION_FAILED`` when the result breaks a KB rule."""
    platform = _platform(request)

    def run(session: Session) -> tuple[int, Any]:
        batch = approve_batch(
            session, platform.clock, user=principal.user_id, classifiers=CLASSIFIER_NAMES
        )
        return HTTPStatus.CREATED, {
            "kb_version_id": batch.kb_version_id,
            "variant_ids": [str(i) for i in batch.variant_ids],
        }

    return run_idempotent(request, raw, principal, key, run)


@router.get("/question-bank/variants/{variant_id}")
def read_variant(variant_id: UUID, *, request: Request, _principal: Viewer) -> VariantView:
    with _platform(request).sessions() as session:
        record = get_variant(session, variant_id)
        if record is None:
            raise NotFoundError(f"No question variant {variant_id}.")
        originals = _originals(session, {record.probe_key})
    return VariantView.of(record, originals.get(record.probe_key))
