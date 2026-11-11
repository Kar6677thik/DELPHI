"""The sensing facade for the api and the CLI (docs/04 §4.1 rule 3): asking for a retrain
(`POST /admin/sensing/train`, docs/13 §7) and reading the ACTIVE artifacts."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, InvalidStateTransitionError, ProblemCode
from delphi.platform.jobs import Enqueued, JobStatus, JobType, enqueue
from delphi.platform.tables import jobs
from delphi.sensing.artifacts import ArtifactKind, StoredArtifact, active_artifacts
from delphi.sim.api import current_dataset

__all__ = ["ArtifactKind", "StoredArtifact", "active_artifacts", "request_training"]

_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def request_training(
    session: Session, clock: Clock, *, idempotency_key: str, created_by: UUID | None
) -> Enqueued:
    """Queue ``TRAIN_SENSING`` on the dataset in the bank, in the caller's transaction. A key
    already used returns that job. Otherwise 409 ``INVALID_STATE_TRANSITION`` without a
    dataset, and 409 ``JOB_ALREADY_RUNNING`` while a training job is queued or running."""
    known = session.execute(
        select(jobs.c.id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if known is not None:
        return Enqueued(known, created=False)
    dataset = current_dataset(session)
    if dataset is None:
        raise InvalidStateTransitionError("There is no dataset to train on; generate one first.")
    running = session.execute(
        select(jobs.c.id).where(
            jobs.c.type == JobType.TRAIN_SENSING.value, jobs.c.status.in_(_ACTIVE)
        )
    ).scalar_one_or_none()
    if running is not None:
        raise DelphiError(
            f"Sensing models are already being trained (job {running}).",
            code=ProblemCode.JOB_ALREADY_RUNNING,
        )
    return enqueue(
        session,
        clock,
        JobType.TRAIN_SENSING,
        idempotency_key=idempotency_key,
        params={"dataset_id": str(dataset.id)},
        created_by=created_by,
    )
