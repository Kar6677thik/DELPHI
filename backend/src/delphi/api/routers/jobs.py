"""Jobs (docs/16 §2, "Admin and jobs"): read a job, list jobs, retry a failed one."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from delphi.api.routers import API_PREFIX
from delphi.api.security import Permission, Principal, require
from delphi.platform.errors import (
    DelphiError,
    InvalidStateTransitionError,
    NotFoundError,
    ProblemCode,
)
from delphi.platform.jobs import JobQueue, JobRecord, JobStatus, JobType, get_job, list_jobs
from delphi.platform.wiring import Platform

router = APIRouter(prefix=API_PREFIX, tags=["jobs"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200  # docs/16 §1


class JobView(BaseModel):
    id: UUID
    type: JobType
    status: JobStatus
    progress: float
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    created_at: datetime
    finished_at: datetime | None

    @staticmethod
    def of(record: JobRecord) -> JobView:
        return JobView(
            id=record.id,
            type=JobType(record.type),
            status=JobStatus(record.status),
            progress=record.progress,
            result=dict(record.result) if record.result else None,
            error=record.error,
            attempts=record.attempts,
            created_at=record.created_at,
            finished_at=record.finished_at,
        )


class JobPage(BaseModel):
    items: list[JobView]
    next_cursor: str | None


def _platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


def _encode_cursor(job_id: UUID) -> str:
    return base64.urlsafe_b64encode(job_id.bytes).decode().rstrip("=")


def _decode_cursor(cursor: str) -> UUID:
    try:
        return UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError):
        raise DelphiError("The cursor is not valid.", code=ProblemCode.VALIDATION_FAILED) from None


@router.get("/jobs/{job_id}")
def read_job(
    job_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.VIEW))],
) -> JobView:
    with _platform(request).sessions() as session:
        record = get_job(session, job_id)
    if record is None:
        raise NotFoundError("No job with this id.")
    return JobView.of(record)


@router.get("/jobs")
def read_jobs(
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.ADMIN))],
    type: Annotated[JobType | None, Query()] = None,
    status: Annotated[JobStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> JobPage:
    before = _decode_cursor(cursor) if cursor else None
    with _platform(request).sessions() as session:
        records = list_jobs(
            session,
            limit=limit + 1,
            before=before,
            job_type=type.value if type else None,
            status=status.value if status else None,
        )
    page, more = records[:limit], len(records) > limit
    return JobPage(
        items=[JobView.of(r) for r in page],
        next_cursor=_encode_cursor(page[-1].id) if more and page else None,
    )


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require(Permission.ADMIN))],
) -> JobView:
    """FAILED → QUEUED with fresh attempts. Retrying is naturally idempotent: a second call
    finds the job no longer FAILED and gets 409."""
    platform = _platform(request)
    if not JobQueue(platform.sessions, platform.clock, platform.settings.queue_config).retry(
        job_id
    ):
        with platform.sessions() as session:
            exists = get_job(session, job_id) is not None
        if not exists:
            raise NotFoundError("No job with this id.")
        raise InvalidStateTransitionError("Only a FAILED job can be retried.")
    with platform.sessions() as session:
        record = get_job(session, job_id)
    assert record is not None
    return JobView.of(record)
