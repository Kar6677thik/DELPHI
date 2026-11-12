"""The ``TRAIN_SENSING`` job (docs/13 §7, docs/04 §5.4, #88).

It trains every sensing model a trainer is registered for, on the dataset in the bank, and
stores each result through ``store_artifact`` (which applies the gate rules). Each model is
stored in its own transaction, so a failure part-way keeps what was already trained, and a
retried job skips the kinds it stored before.

A trainer is a function from a ``TrainingRequest`` to a ``TrainedModel``. It reads the `bank`
tables through ``delphi.sim.api`` on ``request.conn`` (as ``delphi_app``, never `sim_truth`),
picks its own rows and cutoff, and returns the blob, metrics, feature list and gate result. The
trainers come from #89 (prior), #90 (anomaly) and #91 (mule scorer); a kind with no trainer
yet is skipped and reported, never faked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext, JobRegistry, JobType
from delphi.platform.logging import get_logger
from delphi.sensing.artifacts import (
    ArtifactKind,
    StoredArtifact,
    TrainedModel,
    artifacts_of_job,
    store_artifact,
)
from delphi.sim.api import DatasetInfo, current_dataset

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """What a trainer gets: a read connection, the dataset and the job, for progress."""

    conn: Connection
    dataset: DatasetInfo
    ctx: JobContext


Trainer = Callable[[TrainingRequest], TrainedModel]

# Registered by #89 (PRIOR), #90 (ANOMALY) and #91 (MULE) as each lands.
DEFAULT_TRAINERS: Mapping[ArtifactKind, Trainer] = {}


class LeaseLostError(RuntimeError):
    """The job's lease passed to another worker: stop without storing more."""


class NoDatasetError(RuntimeError):
    """``TRAIN_SENSING`` ran before a dataset was written."""


def _summary(artifact: StoredArtifact) -> dict[str, Any]:
    return {
        "kind": artifact.kind.value,
        "id": str(artifact.id),
        "version": artifact.version,
        "status": artifact.status.value,
        "gate_passed": artifact.gate_passed,
    }


@dataclass(frozen=True, slots=True)
class TrainSensingJob:
    engine: Engine  # as delphi_app
    sessions: sessionmaker[Session]
    clock: Clock
    trainers: Mapping[ArtifactKind, Trainer]

    def __call__(self, ctx: JobContext) -> Mapping[str, Any] | None:
        wanted = ctx.job.params.get("dataset_id")
        with self.engine.connect() as conn:
            dataset = current_dataset(conn)
            done = artifacts_of_job(conn, ctx.job.id)
        if dataset is None:
            raise NoDatasetError("there is no dataset to train on; generate one first")
        if wanted is not None and UUID(str(wanted)) != dataset.id:
            raise NoDatasetError(f"dataset {wanted} is no longer in the bank ({dataset.id} is)")
        kinds = list(ArtifactKind)
        stored: list[dict[str, Any]] = []
        skipped: list[str] = []
        for i, kind in enumerate(kinds):
            if kind in done:
                stored.append(_summary(done[kind]))
                continue
            trainer = self.trainers.get(kind)
            if trainer is None:
                skipped.append(kind.value)
                log.warning("sensing.trainer_missing", kind=kind.value)
                continue
            with self.engine.connect() as conn:
                model = trainer(TrainingRequest(conn, dataset, ctx))
            if model.kind is not kind:
                raise ValueError(f"the {kind} trainer returned a {model.kind} model")
            with self.sessions.begin() as session:
                artifact = store_artifact(
                    session, self.clock, model, dataset_id=dataset.id, job_id=ctx.job.id
                )
            stored.append(_summary(artifact))
            if not ctx.report((i + 1) / len(kinds)):
                raise LeaseLostError(f"job {ctx.job.id} is no longer this worker's")
        return {"dataset_id": str(dataset.id), "artifacts": stored, "skipped": skipped}


def register_sensing_jobs(
    registry: JobRegistry,
    *,
    engine: Engine,
    sessions: sessionmaker[Session],
    clock: Clock,
    trainers: Mapping[ArtifactKind, Trainer] = DEFAULT_TRAINERS,
) -> None:
    registry.register(JobType.TRAIN_SENSING, TrainSensingJob(engine, sessions, clock, trainers))
