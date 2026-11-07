"""The PostgreSQL job queue (docs/04 §5.4, FR-PLT-07).

Jobs are rows in `delphi.jobs`. A worker claims one with `FOR UPDATE SKIP LOCKED` and holds it
under a **lease** (`locked_until`), which it extends while the handler runs. If the worker dies,
the lease expires and another worker claims the job again; that is the at-least-once guarantee,
so every handler must be idempotent (it upserts its results, keyed by the job).

Every write after the claim is guarded by `locked_by = <this worker> AND status = 'RUNNING'`, so
a worker that lost its lease (it stalled past `locked_until` and someone else took the job) can
no longer change the job. Times come from the `Clock` port, so tests can expire a lease without
waiting.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Row, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.logging import get_logger
from delphi.platform.tables import jobs

log = get_logger(__name__)

# The longest error text kept on a job row; the full traceback goes to the log.
MAX_ERROR_CHARS = 2000
DEFAULT_MAX_ATTEMPTS = 3  # docs/04 §5.4, FR-PLT-07; settings may override


def _announce(session: Session, job_id: UUID, status: str, progress: float | None = None) -> None:
    """`job.updated` in the same transaction as the change (docs/16 §3)."""
    payload: dict[str, Any] = {"status": status}
    if progress is not None:
        payload["progress"] = progress
    publish(session, EventType.JOB_UPDATED, str(job_id), payload)


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    """Every job type in docs/04 §5.4. A type is only claimed once a handler is registered."""

    GENERATE_DATASET = "GENERATE_DATASET"
    TRAIN_SENSING = "TRAIN_SENSING"
    RUN_EVALUATION = "RUN_EVALUATION"
    RUN_SCOUT = "RUN_SCOUT"
    RUN_LEARNING = "RUN_LEARNING"
    RUN_BACKTEST = "RUN_BACKTEST"
    KB_REBASE = "KB_REBASE"
    LLM_NARRATIVE = "LLM_NARRATIVE"
    LLM_CASE_SUMMARY = "LLM_CASE_SUMMARY"
    LLM_QUESTION_VARIANTS = "LLM_QUESTION_VARIANTS"
    LLM_TYPOLOGY_DESCRIPTION = "LLM_TYPOLOGY_DESCRIPTION"
    SIM_OUTREACH_CALL = "SIM_OUTREACH_CALL"
    DEMO_STORY = "DEMO_STORY"
    DEMO_RESET = "DEMO_RESET"
    # Recurring (enqueued by the scheduler, docs/04 §5.4)
    SIM_TICK = "SIM_TICK"
    CHASE_RETRACE = "CHASE_RETRACE"
    MULE_SWEEP = "MULE_SWEEP"
    SESSION_TIMEOUT_SWEEP = "SESSION_TIMEOUT_SWEEP"
    SCOUT_AUTO_TRIGGER = "SCOUT_AUTO_TRIGGER"
    LABEL_DELIVERY = "LABEL_DELIVERY"
    PROBE_YIELD_DAILY = "PROBE_YIELD_DAILY"
    RETENTION_SWEEP = "RETENTION_SWEEP"
    AUDIT_VERIFY = "AUDIT_VERIFY"


class Lane(StrEnum):
    """Worker lanes (docs/04 §5.4): the fast lane keeps the simulation clock and the re-trace
    moving while long jobs (dataset generation, training) occupy the default lane."""

    DEFAULT = "DEFAULT"
    FAST = "FAST"


FAST_LANE = frozenset({JobType.SIM_TICK, JobType.CHASE_RETRACE})


def lane_of(job_type: JobType) -> Lane:
    return Lane.FAST if job_type in FAST_LANE else Lane.DEFAULT


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    type: JobType
    status: JobStatus
    params: Mapping[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str
    locked_by: str | None
    locked_until: datetime | None

    @staticmethod
    def from_row(row: Row[Any]) -> Job:
        return Job(
            id=row.id,
            type=JobType(row.type),
            status=JobStatus(row.status),
            params=row.params,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            idempotency_key=row.idempotency_key,
            locked_by=row.locked_by,
            locked_until=row.locked_until,
        )


@dataclass(frozen=True, slots=True)
class Enqueued:
    job_id: UUID
    created: bool  # False: a job with this idempotency key already existed


@dataclass(frozen=True, slots=True)
class QueueConfig:
    lease: timedelta = timedelta(seconds=60)
    retry_backoff: timedelta = timedelta(seconds=30)
    default_max_attempts: int = DEFAULT_MAX_ATTEMPTS


def enqueue(
    session: Session,
    clock: Clock,
    job_type: JobType,
    *,
    idempotency_key: str,
    params: Mapping[str, Any] | None = None,
    run_after: datetime | None = None,
    created_by: UUID | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Enqueued:
    """Add a job in the caller's transaction. The same idempotency key never makes a second
    job: it returns the existing one (`created=False`), whatever its status."""
    now = clock.now()
    # The id's timestamp comes from the same clock as created_at, so id order is creation order
    # (to the millisecond), which GET /jobs relies on for keyset paging.
    job_id = uuid7(unix_ms=int(now.timestamp() * 1000))
    inserted = session.execute(
        insert(jobs)
        .values(
            id=job_id,
            type=job_type.value,
            status=JobStatus.QUEUED.value,
            params=dict(params or {}),
            progress=0.0,
            attempts=0,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            run_after=run_after or now,
            created_by=created_by,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[jobs.c.idempotency_key])
        .returning(jobs.c.id)
    ).scalar_one_or_none()
    if inserted is not None:
        _announce(session, inserted, JobStatus.QUEUED.value, 0.0)
        return Enqueued(inserted, created=True)
    existing = session.execute(
        select(jobs.c.id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one()
    return Enqueued(existing, created=False)


@dataclass(slots=True)
class JobQueue:
    """Claiming and finishing jobs. Each method runs in its own short transaction."""

    sessions: sessionmaker[Session]
    clock: Clock
    config: QueueConfig = field(default_factory=QueueConfig)

    def claim(self, worker_id: str, types: frozenset[JobType]) -> Job | None:
        """Take the oldest due job of one of ``types``: a QUEUED job whose ``run_after`` has
        passed, or a RUNNING job whose lease has expired (its worker died) and that has
        attempts left. Returns None when there is nothing to do."""
        if not types:
            return None
        now = self.clock.now()
        due = or_(
            and_(jobs.c.status == JobStatus.QUEUED.value, jobs.c.run_after <= now),
            and_(
                jobs.c.status == JobStatus.RUNNING.value,
                jobs.c.locked_until < now,
                jobs.c.attempts < jobs.c.max_attempts,
            ),
        )
        candidate = (
            select(jobs.c.id)
            .where(jobs.c.type.in_([t.value for t in types]), due)
            .order_by(jobs.c.run_after, jobs.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        with self.sessions.begin() as session:
            row = session.execute(
                update(jobs)
                .where(jobs.c.id == candidate)
                .values(
                    status=JobStatus.RUNNING.value,
                    attempts=jobs.c.attempts + 1,
                    locked_by=worker_id,
                    locked_until=now + self.config.lease,
                    started_at=func.coalesce(jobs.c.started_at, now),
                    error=None,
                )
                .returning(*jobs.c)
            ).one_or_none()
            if row is not None:
                _announce(session, row.id, JobStatus.RUNNING.value, row.progress)
        if row is None:
            return None
        job = Job.from_row(row)
        log.info("job.claimed", job_id=str(job.id), job_type=job.type, attempt=job.attempts)
        return job

    def reap_expired(self) -> int:
        """Fail RUNNING jobs whose lease expired with no attempts left: their worker died on
        the last attempt, so nobody will claim them again."""
        now = self.clock.now()
        with self.sessions.begin() as session:
            result = session.execute(
                update(jobs)
                .where(
                    jobs.c.status == JobStatus.RUNNING.value,
                    jobs.c.locked_until < now,
                    jobs.c.attempts >= jobs.c.max_attempts,
                )
                .values(
                    status=JobStatus.FAILED.value,
                    error="lease expired on the last attempt (worker stopped)",
                    locked_by=None,
                    locked_until=None,
                    finished_at=now,
                )
                .returning(jobs.c.id)
            )
            reaped = [r.id for r in result]
            for job_id in reaped:
                _announce(session, job_id, JobStatus.FAILED.value)
        for job_id in reaped:
            log.warning("job.reaped", job_id=str(job_id))
        return len(reaped)

    def heartbeat(self, job: Job, worker_id: str, progress: float | None = None) -> bool:
        """Extend the lease (and optionally record progress in [0, 1]). False means this worker
        no longer owns the job and must stop working on it."""
        values: dict[str, Any] = {"locked_until": self.clock.now() + self.config.lease}
        if progress is not None:
            values["progress"] = min(max(float(progress), 0.0), 1.0)
        return self._owned_update(job, worker_id, values)

    def succeed(self, job: Job, worker_id: str, result: Mapping[str, Any] | None) -> bool:
        now = self.clock.now()
        owned = self._owned_update(
            job,
            worker_id,
            {
                "status": JobStatus.SUCCEEDED.value,
                "result": json.loads(json.dumps(dict(result or {}), default=str)),
                "progress": 1.0,
                "locked_by": None,
                "locked_until": None,
                "finished_at": now,
            },
        )
        if owned:
            log.info("job.succeeded", job_id=str(job.id), job_type=job.type)
        return owned

    def fail(self, job: Job, worker_id: str, error: str) -> JobStatus | None:
        """Record a failed attempt: back to QUEUED after an exponential backoff while attempts
        remain (30 s, 60 s, ... by default), else FAILED. None if the lease was lost."""
        now = self.clock.now()
        retry = job.attempts < job.max_attempts
        values: dict[str, Any] = {
            "error": error[:MAX_ERROR_CHARS],
            "locked_by": None,
            "locked_until": None,
        }
        if retry:
            backoff = self.config.retry_backoff * (2 ** (job.attempts - 1))
            values |= {"status": JobStatus.QUEUED.value, "run_after": now + backoff}
            status = JobStatus.QUEUED
        else:
            values |= {"status": JobStatus.FAILED.value, "finished_at": now}
            status = JobStatus.FAILED
        if not self._owned_update(job, worker_id, values):
            return None
        log.warning(
            "job.attempt_failed",
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempts,
            next_status=status,
        )
        return status

    def retry(self, job_id: UUID) -> bool:
        """FAILED → QUEUED with a fresh set of attempts (POST /jobs/{id}/retry, docs/16 §2)."""
        now = self.clock.now()
        with self.sessions.begin() as session:
            done = session.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == JobStatus.FAILED.value)
                .values(
                    status=JobStatus.QUEUED.value,
                    attempts=0,
                    run_after=now,
                    progress=0.0,
                    error=None,
                    finished_at=None,
                )
                .returning(jobs.c.id)
            ).one_or_none()
            if done is not None:
                _announce(session, job_id, JobStatus.QUEUED.value, 0.0)
        return done is not None

    def _owned_update(self, job: Job, worker_id: str, values: Mapping[str, Any]) -> bool:
        with self.sessions.begin() as session:
            row = session.execute(
                update(jobs)
                .where(
                    jobs.c.id == job.id,
                    jobs.c.locked_by == worker_id,
                    jobs.c.status == JobStatus.RUNNING.value,
                )
                .values(**values)
                .returning(jobs.c.id, jobs.c.status, jobs.c.progress)
            ).one_or_none()
            if row is not None and ("status" in values or "progress" in values):
                _announce(session, row.id, row.status, row.progress)
        if row is None:
            log.warning("job.lease_lost", job_id=str(job.id), worker=worker_id)
            return False
        return True


Handler = Callable[["JobContext"], Mapping[str, Any] | None]


@dataclass(slots=True)
class JobContext:
    """What a handler gets: the job, the shared resources, and a way to report progress (which
    also extends the lease). A handler that sees ``report`` return False must stop."""

    job: Job
    queue: JobQueue
    worker_id: str

    def report(self, progress: float) -> bool:
        return self.queue.heartbeat(self.job, self.worker_id, progress)


@dataclass(slots=True)
class JobRegistry:
    """Handlers by job type, filled by the composition root. Only registered types are claimed,
    so a worker never fails a job just because its handler ships in a later release."""

    handlers: dict[JobType, Handler] = field(default_factory=dict)

    def register(self, job_type: JobType, handler: Handler) -> None:
        if job_type in self.handlers:
            raise ValueError(f"a handler for {job_type} is already registered")
        self.handlers[job_type] = handler

    def types(self, lane: Lane) -> frozenset[JobType]:
        return frozenset(t for t in self.handlers if lane_of(t) is lane)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """A job as the api shows it (docs/16 §2, GET /jobs/{id})."""

    id: UUID
    type: str
    status: str
    progress: float
    result: Mapping[str, Any] | None
    error: str | None
    attempts: int
    created_at: datetime
    finished_at: datetime | None


_RECORD_COLUMNS = (
    jobs.c.id,
    jobs.c.type,
    jobs.c.status,
    jobs.c.progress,
    jobs.c.result,
    jobs.c.error,
    jobs.c.attempts,
    jobs.c.created_at,
    jobs.c.finished_at,
)


def get_job(session: Session, job_id: UUID) -> JobRecord | None:
    row = session.execute(select(*_RECORD_COLUMNS).where(jobs.c.id == job_id)).one_or_none()
    return JobRecord(*row) if row is not None else None


def list_jobs(
    session: Session,
    *,
    limit: int,
    before: UUID | None = None,
    job_type: str | None = None,
    status: str | None = None,
) -> list[JobRecord]:
    """Newest first. Job ids are UUIDv7, so id order is creation order and ``before`` (the last
    id of the previous page) is a keyset cursor (docs/16 §1)."""
    query = select(*_RECORD_COLUMNS).order_by(jobs.c.id.desc()).limit(limit)
    if before is not None:
        query = query.where(jobs.c.id < before)
    if job_type is not None:
        query = query.where(jobs.c.type == job_type)
    if status is not None:
        query = query.where(jobs.c.status == status)
    return [JobRecord(*row) for row in session.execute(query)]
