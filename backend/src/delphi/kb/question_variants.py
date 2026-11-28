"""Question variants drafted in the app and their review (docs/07 §9, docs/15, FR-KB-05, #110).

The in-app route for new wordings and translations of a question probe:

1. ``LLM_QUESTION_VARIANTS`` (``QuestionVariantsJob``) asks the model, through ``LlmService``,
   for one variant per language wanted, from the ACTIVE KB's canonical English variant (the
   lowest ``variant_id``). Each variant that passes docs/15 §5 is stored as PENDING_REVIEW. With
   no model (the kill switch, the budget, or every output failing validation), no variant is
   drafted: ``QUESTION_VARIANTS`` has no template.
2. An FDE reviews each one (``review``): APPROVE, optionally with an edited question text, or
   REJECT. Only PENDING_REVIEW variants can be reviewed, and every review is audited.
3. "Approve batch" (``approve_batch``) adds every APPROVED variant not yet included to the
   ACTIVE KB's question bank, as APPROVED, and stores the result as a CANDIDATE KB version
   (parent = ACTIVE), validated as any KB version is. Promoting it is the KB lifecycle's
   (docs/07 §1, #119).

Nothing in ``question_variants`` is ever served: the Guardian serves only the question bank of
the KB version a session is pinned to, so a variant reaches a customer only once a reviewed KB
version holding it is ACTIVE (FR-KB-05, docs/15 §1).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast, get_args
from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import (
    KnowledgeBase,
    Language,
    QuestionBankEntry,
    QuestionOption,
    QuestionVariant,
)
from delphi.kb.store import KbStatus, active_version, insert_version
from delphi.kb.validate import validate_kb
from delphi.llm.port import Purpose
from delphi.llm.schemas import VARIANT_MAX_CHARS, QuestionVariants
from delphi.llm.service import LlmService
from delphi.llm.validators import variant_text_issues
from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.errors import (
    DelphiError,
    InvalidStateTransitionError,
    NotFoundError,
    ProblemCode,
)
from delphi.platform.ids import uuid7
from delphi.platform.jobs import JobContext, JobRegistry, JobType
from delphi.platform.logging import get_logger

log = get_logger(__name__)

metadata = MetaData(schema="delphi")

question_variants = Table(
    "question_variants",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("probe_key", Text, nullable=False),
    Column("language", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("options", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("generated_by", Text, nullable=False),
    Column("llm_call_id", PG_UUID(as_uuid=True)),
    Column("job_id", PG_UUID(as_uuid=True)),
    Column("reviewed_by", PG_UUID(as_uuid=True)),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("included_in_kb_version", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

LANGUAGES: tuple[Language, ...] = get_args(Language)
CANONICAL_LANGUAGE: Language = "en"
APPROVED: Literal["APPROVED"] = "APPROVED"
_NUMBERED = re.compile(r"^(?P<prefix>.+)-(?P<lang>[a-z]{2})-(?P<n>\d+)$")


class VariantStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class GeneratedBy(StrEnum):
    HUMAN = "HUMAN"
    LLM = "LLM"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class VariantRecord:
    id: UUID
    probe_key: str
    language: Language
    text: str
    options: list[dict[str, str]]
    status: VariantStatus
    generated_by: GeneratedBy
    llm_call_id: UUID | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    included_in_kb_version: int | None
    created_at: datetime

    @staticmethod
    def of(row: Any) -> VariantRecord:
        return VariantRecord(
            id=row.id,
            probe_key=row.probe_key,
            language=cast(Language, row.language),
            text=row.text,
            options=list(row.options),
            status=VariantStatus(row.status),
            generated_by=GeneratedBy(row.generated_by),
            llm_call_id=row.llm_call_id,
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
            included_in_kb_version=row.included_in_kb_version,
            created_at=row.created_at,
        )


@dataclass(frozen=True, slots=True)
class Batch:
    """What "approve batch" made: the CANDIDATE KB version and the variants it holds."""

    kb_version_id: int
    variant_ids: list[UUID]


def _active_kb(session: Session) -> tuple[int, KnowledgeBase]:
    active = active_version(session)
    if active is None:
        raise DelphiError("There is no ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
    return active.id, active.kb


def canonical_variant(kb: KnowledgeBase, probe_key: str) -> QuestionVariant:
    """The probe's source for drafting and review: its APPROVED English variant with the lowest
    ``variant_id`` ("variant 1", docs/07 §9). 404 for a probe the question bank lacks."""
    entry = kb.question_bank.get(probe_key)
    english = sorted(
        (
            v
            for v in (entry.variants if entry is not None else ())
            if v.language == CANONICAL_LANGUAGE and v.status == APPROVED
        ),
        key=lambda v: v.variant_id,
    )
    if not english:
        raise NotFoundError(f"The question bank has no question probe {probe_key!r}.")
    return english[0]


def _payload(source: QuestionVariant, probe_key: str, languages: Sequence[str]) -> dict[str, Any]:
    return {
        "probe_key": probe_key,
        "text": source.text,
        "options": [{"key": o.key, "text": o.text} for o in source.options],
        "languages": list(languages),
    }


def store_drafts(
    session: Session,
    clock: Clock,
    *,
    probe_key: str,
    languages: Sequence[str],
    drafted: QuestionVariants,
    llm_call_id: UUID,
    job_id: UUID | None,
) -> list[UUID]:
    """Store the model's variants as PENDING_REVIEW: the first one for each language asked for,
    others dropped. In the caller's transaction."""
    now = clock.now()
    wanted = set(languages)
    ids: list[UUID] = []
    for v in drafted.variants:
        if v.language not in wanted:
            continue
        wanted.discard(v.language)
        variant_id = uuid7()
        session.execute(
            question_variants.insert().values(
                id=variant_id,
                probe_key=probe_key,
                language=v.language,
                text=v.text,
                options=[{"key": o.key, "text": o.text} for o in v.options],
                status=VariantStatus.PENDING_REVIEW.value,
                generated_by=GeneratedBy.LLM.value,
                llm_call_id=llm_call_id,
                job_id=job_id,
                created_at=now,
            )
        )
        ids.append(variant_id)
    if ids:
        record(
            session,
            clock,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="question_variants.drafted",
            entity_type="question_probe",
            entity_id=probe_key,
            payload={"variant_ids": [str(i) for i in ids], "llm_call_id": str(llm_call_id)},
        )
    return ids


@dataclass(frozen=True, slots=True)
class QuestionVariantsJob:
    """``LLM_QUESTION_VARIANTS`` (``POST /question-bank/generate``, docs/04 §5.4): params
    ``{probe_key, languages}``. The model call runs outside any transaction. Idempotent: a retry
    finds the variants an earlier attempt committed (by ``job_id``)."""

    sessions: sessionmaker[Session]
    clock: Clock
    llm: LlmService

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        probe_key = str(ctx.job.params["probe_key"])
        languages = [str(lang) for lang in ctx.job.params["languages"]]
        with self.sessions() as session:
            done = list(
                session.execute(
                    select(question_variants.c.id).where(question_variants.c.job_id == ctx.job.id)
                ).scalars()
            )
            if done:
                return {"drafted": len(done), "variant_ids": [str(i) for i in done]}
            _, kb = _active_kb(session)
            source = canonical_variant(kb, probe_key)
        generated = self.llm.generate(
            Purpose.QUESTION_VARIANTS, _payload(source, probe_key, languages)
        )
        if generated.parsed is None or not generated.call_ids:
            log.warning("kb.question_variants_none", probe_key=probe_key, languages=languages)
            return {"drafted": 0, "reason": "no model output passed validation"}
        with self.sessions.begin() as session:
            ids = store_drafts(
                session,
                self.clock,
                probe_key=probe_key,
                languages=languages,
                drafted=cast(QuestionVariants, generated.parsed),
                llm_call_id=generated.call_ids[-1],
                job_id=ctx.job.id,
            )
        return {"drafted": len(ids), "variant_ids": [str(i) for i in ids]}


def get_variant(session: Session, variant_id: UUID) -> VariantRecord | None:
    row = session.execute(
        select(question_variants).where(question_variants.c.id == variant_id)
    ).one_or_none()
    return VariantRecord.of(row) if row is not None else None


def list_variants(
    session: Session,
    *,
    status: VariantStatus | None,
    probe_key: str | None,
    limit: int,
    after: tuple[datetime, UUID] | None = None,
    included: bool | None = None,
) -> list[VariantRecord]:
    """Oldest first, keyset-paged on ``(created_at, id)``. ``included=False`` with
    ``status=APPROVED`` is what "Approve batch" would take."""
    q = question_variants.c
    query = select(question_variants).order_by(q.created_at, q.id).limit(limit)
    if status is not None:
        query = query.where(q.status == status.value)
    if included is not None:
        has_version = q.included_in_kb_version.is_not(None)
        query = query.where(has_version if included else ~has_version)
    if probe_key is not None:
        query = query.where(q.probe_key == probe_key)
    if after is not None:
        query = query.where(
            or_(q.created_at > after[0], and_(q.created_at == after[0], q.id > after[1]))
        )
    return [VariantRecord.of(r) for r in session.execute(query)]


def review(
    session: Session,
    clock: Clock,
    variant_id: UUID,
    decision: Decision,
    *,
    reviewer: UUID,
    edited_text: str | None = None,
) -> VariantRecord:
    """APPROVE (optionally with an edited question text) or REJECT a PENDING_REVIEW variant,
    audited. 404 for an unknown variant, 409 once it was reviewed, 422 for an edit on a
    rejection or an edited text that breaks docs/15 §5 rule 6."""
    q = question_variants.c
    row = session.execute(
        select(question_variants).where(q.id == variant_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFoundError(f"No question variant {variant_id}.")
    if row.status != VariantStatus.PENDING_REVIEW.value:
        raise InvalidStateTransitionError(f"The variant is already {row.status}.")
    text = row.text
    if edited_text is not None:
        if decision is Decision.REJECT:
            raise DelphiError(
                "Only an approval can edit the text.", code=ProblemCode.VALIDATION_FAILED
            )
        text = edited_text.strip()
        issues = variant_text_issues(text)
        if not text or len(text) > VARIANT_MAX_CHARS:
            issues.append(f"must be 1 to {VARIANT_MAX_CHARS} characters")
        if issues:
            raise DelphiError(
                f"The edited text {', and '.join(issues)}.", code=ProblemCode.VALIDATION_FAILED
            )
    status = VariantStatus.APPROVED if decision is Decision.APPROVE else VariantStatus.REJECTED
    now = clock.now()
    session.execute(
        question_variants.update()
        .where(q.id == variant_id)
        .values(status=status.value, text=text, reviewed_by=reviewer, reviewed_at=now)
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(reviewer),
        action="question_variant.reviewed",
        entity_type="question_variant",
        entity_id=str(variant_id),
        payload={
            "decision": decision.value,
            "probe_key": row.probe_key,
            "language": row.language,
            "edited": text != row.text,
            **({"original_text": row.text} if text != row.text else {}),
        },
    )
    reviewed = get_variant(session, variant_id)
    assert reviewed is not None
    return reviewed


def _next_variant_id(entry: QuestionBankEntry, language: str) -> str:
    """The next id in the entry's ``<prefix>-<language>-<n>`` series (``PQ-te-1`` after none)."""
    prefix = entry.probe_key
    numbers = [0]
    for v in entry.variants:
        m = _NUMBERED.match(v.variant_id)
        if m is None:
            continue
        prefix = m["prefix"]
        if m["lang"] == language:
            numbers.append(int(m["n"]))
    return f"{prefix}-{language}-{max(numbers) + 1}"


def with_variants(kb: KnowledgeBase, approved: Sequence[VariantRecord]) -> KnowledgeBase:
    """``kb`` with each approved variant appended, APPROVED, to its probe's question bank."""
    bank = dict(kb.question_bank)
    for v in approved:
        entry = bank.get(v.probe_key)
        if entry is None:
            raise DelphiError(
                f"The ACTIVE KB has no question probe {v.probe_key!r}.",
                code=ProblemCode.VALIDATION_FAILED,
            )
        added = QuestionVariant(
            variant_id=_next_variant_id(entry, v.language),
            language=v.language,
            status=APPROVED,
            text=v.text,
            options=tuple(QuestionOption(key=o["key"], text=o["text"]) for o in v.options),
        )
        bank[v.probe_key] = entry.model_copy(update={"variants": (*entry.variants, added)})
    return kb.model_copy(update={"question_bank": bank})


def approve_batch(
    session: Session, clock: Clock, *, user: UUID, classifiers: frozenset[str]
) -> Batch:
    """docs/07 §9 "Approve batch": every APPROVED variant not yet included goes into a new
    CANDIDATE KB version, parent = ACTIVE, validated as any KB version is. 409 when there is
    none; 422 with the issues when the result breaks a KB rule."""
    q = question_variants.c
    # No cap: every row here was approved one by one by an FDE engineer, so the set is as large
    # as the reviews since the last batch, and the new KB version must hold all of them anyway.
    rows = session.execute(
        select(question_variants)
        .where(q.status == VariantStatus.APPROVED.value, q.included_in_kb_version.is_(None))
        .order_by(q.created_at, q.id)
        .with_for_update()
    ).all()
    if not rows:
        raise InvalidStateTransitionError("No approved variant is waiting for a KB version.")
    approved = [VariantRecord.of(r) for r in rows]
    parent_id, kb = _active_kb(session)
    candidate = with_variants(kb, approved)
    issues = validate_kb(candidate, classifiers)
    if issues:
        raise DelphiError(
            "The variants break a KB rule: " + "; ".join(str(i) for i in issues),
            code=ProblemCode.KB_VALIDATION_FAILED,
        )
    probes = sorted({v.probe_key for v in approved})
    version_id = insert_version(
        session,
        clock,
        candidate,
        status=KbStatus.CANDIDATE,
        change_summary=(
            f"{len(approved)} reviewed question variant(s) for {', '.join(probes)}"
            " (question bank, docs/07 §9)"
        ),
        parent_id=parent_id,
        created_by=user,
    )
    ids = [v.id for v in approved]
    session.execute(
        question_variants.update().where(q.id.in_(ids)).values(included_in_kb_version=version_id)
    )
    record(
        session,
        clock,
        actor_type=ActorType.USER,
        actor_id=str(user),
        action="question_variants.batched",
        entity_type="kb_version",
        entity_id=str(version_id),
        payload={"variant_ids": [str(i) for i in ids]},
    )
    return Batch(version_id, ids)


def register_question_variant_jobs(
    registry: JobRegistry, *, sessions: sessionmaker[Session], clock: Clock, llm: LlmService
) -> None:
    registry.register(JobType.LLM_QUESTION_VARIANTS, QuestionVariantsJob(sessions, clock, llm))
