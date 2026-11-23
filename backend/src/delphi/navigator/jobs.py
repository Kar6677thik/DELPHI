"""The Navigator's jobs (docs/04 §5.4): ``LLM_NARRATIVE``, the evidence card's narrative.

The evidence fetch enqueues one per new record (docs/09 §3). The handler builds the docs/15 §7
payload from the record (the adapter's facts, a pseudonymous subject, the case's ``as_of``),
asks ``LlmService`` for the text (the model, or the source's template: the fallback chain and
its log are the service's), stores it on the record with its source and citations, and
publishes `evidence.narrative_ready`. It never changes an outcome or a posterior (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.store import get_version
from delphi.llm.minimise import pseudonym
from delphi.llm.port import Purpose
from delphi.llm.service import LlmService
from delphi.navigator.ddpack import GuardianTrail, refresh_complete
from delphi.navigator.evidence import narrative_subject
from delphi.navigator.reviews import AccountReviewSweepJob, SweepLimits
from delphi.navigator.store import get_case
from delphi.navigator.tables import evidence_records
from delphi.platform.clock import Clock
from delphi.platform.events import EventType, publish
from delphi.platform.jobs import JobContext, JobRegistry, JobType


def narrative_payload(session: Session, record_id: UUID) -> dict[str, Any] | None:
    """docs/15 §7: ``{source_key, record_id, subject, as_of, facts}``; ``None`` if the record is
    gone or already has its narrative."""
    row = session.execute(
        select(evidence_records).where(evidence_records.c.id == record_id)
    ).one_or_none()
    if row is None or row.narrative is not None:
        return None
    case = get_case(session, row.case_id)
    assert case is not None
    version = get_version(session, case.kb_version_id)
    assert version is not None
    account, as_of = narrative_subject(session, case, version.kb.sources[row.source_key].subject)
    return {
        "source_key": row.source_key,
        "record_id": str(row.id),
        "subject": pseudonym("ACC", account) if account is not None else "the customer",
        "as_of": as_of.isoformat(),
        "facts": dict(row.raw),
    }


@dataclass(frozen=True, slots=True)
class NarrativeJob:
    """``LLM_NARRATIVE``: ``params = {"evidence_record_id": …}``."""

    sessions: sessionmaker[Session]
    llm: LlmService
    trail: GuardianTrail | None = None  # the case's Guardian session trail (DD pack, docs/09 §7)

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        record_id = UUID(str(ctx.job.params["evidence_record_id"]))
        with self.sessions() as session:
            payload = narrative_payload(session, record_id)
        if payload is None:
            return {"written": False}
        generated = self.llm.generate(Purpose.EVIDENCE_NARRATIVE, payload)
        if generated.parsed is None or generated.source is None:
            return {"written": False, "source": None}
        text = getattr(generated.parsed, "finding", "")
        citations = list(getattr(generated.parsed, "citations", []))
        e = evidence_records.c
        with self.sessions.begin() as session:
            case_id = session.execute(
                update(evidence_records)
                .where(e.id == record_id, e.narrative.is_(None))
                .values(
                    narrative=text,
                    narrative_source=generated.source.value,
                    narrative_citations=citations,
                )
                .returning(e.case_id)
            ).scalar_one_or_none()
            if case_id is not None:
                refresh_complete(session, case_id, self.trail)  # section 6 may now be complete
                publish(
                    session,
                    EventType.EVIDENCE_NARRATIVE_READY,
                    str(case_id),
                    {"case_id": str(case_id), "evidence_record_id": str(record_id)},
                )
        return {"written": case_id is not None, "source": generated.source.value}


def register_navigator_jobs(
    registry: JobRegistry,
    *,
    sessions: sessionmaker[Session],
    llm: LlmService,
    trail: GuardianTrail | None = None,
    clock: Clock,
    p_mule_fallback: float,
    review_limits: SweepLimits,
) -> None:
    registry.register(JobType.LLM_NARRATIVE, NarrativeJob(sessions, llm, trail))
    # The SLA runs in real time until the DEMO time mode is wired, as the Guardian's does.
    registry.register(
        JobType.ACCOUNT_REVIEW_SWEEP,
        AccountReviewSweepJob(
            sessions,
            clock,
            p_mule_fallback=p_mule_fallback,
            demo_time=False,
            limits=review_limits,
        ),
    )
