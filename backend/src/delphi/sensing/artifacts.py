"""Sensing model artifacts and their lifecycle (docs/13 §4, §7, FR-SNS-06, #88).

A trainer turns a dataset into a ``TrainedModel``: the joblib blob, the metrics its gate read,
the feature list, the training cutoff and whether the gate passed. ``store_artifact`` keeps every
one, and decides what becomes ACTIVE:

- the gate passed: the new artifact is ACTIVE, and the previous ACTIVE one of its kind RETIRED;
- the gate failed and an ACTIVE artifact of its kind exists: the new one is FAILED_GATE, and the
  previous one stays ACTIVE;
- the gate failed and none exists (the first training): it is activated anyway with
  ``gate_passed = false``, because the demo must always have a model; ``/status`` reports it so
  the Overview page shows "sensing below quality gate".

Each activation publishes ``artifact.activated``, so the api and the worker reload the model.
Reading a blob checks its SHA-256, so an edited row is refused instead of unpickled.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Session

from delphi.platform.audit import ActorType, record
from delphi.platform.clock import Clock
from delphi.platform.events import EventType, publish
from delphi.platform.ids import uuid7
from delphi.platform.locks import MODEL_ARTIFACT_LOCK_KEY
from delphi.platform.logging import get_logger

log = get_logger(__name__)

metadata = MetaData(schema="delphi")

model_artifacts = Table(
    "model_artifacts",
    metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("gate_passed", Boolean, nullable=False),
    Column("dataset_id", PgUUID(as_uuid=True)),
    Column("job_id", PgUUID(as_uuid=True)),
    Column("trained_at", DateTime(timezone=True), nullable=False),
    Column("training_cutoff", DateTime(timezone=True), nullable=False),
    Column("metrics", JSONB, nullable=False),
    Column("feature_list", JSONB, nullable=False),
    Column("blob", LargeBinary, nullable=False),
    Column("sha256", LargeBinary, nullable=False),
    Column("activated_at", DateTime(timezone=True)),
    Column("retired_at", DateTime(timezone=True)),
)


class ArtifactKind(StrEnum):
    """The sensing models (docs/13 §1)."""

    PRIOR = "PRIOR"
    ANOMALY = "ANOMALY"
    MULE = "MULE"


class ArtifactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED_GATE = "FAILED_GATE"


class ArtifactIntegrityError(RuntimeError):
    """A stored blob that no longer matches its SHA-256."""


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """What a trainer hands back. ``metrics`` must be JSON-serialisable."""

    kind: ArtifactKind
    blob: bytes
    metrics: Mapping[str, Any]
    feature_list: Sequence[str]
    training_cutoff: datetime
    gate_passed: bool
    gate_reasons: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """An artifact row without its blob."""

    id: UUID
    kind: ArtifactKind
    version: int
    status: ArtifactStatus
    gate_passed: bool
    dataset_id: UUID | None
    job_id: UUID | None
    trained_at: datetime
    training_cutoff: datetime
    metrics: Mapping[str, Any]
    feature_list: tuple[str, ...]
    sha256: str
    activated_at: datetime | None

    @staticmethod
    def of(row: Any) -> StoredArtifact:
        return StoredArtifact(
            id=row.id,
            kind=ArtifactKind(row.kind),
            version=row.version,
            status=ArtifactStatus(row.status),
            gate_passed=row.gate_passed,
            dataset_id=row.dataset_id,
            job_id=row.job_id,
            trained_at=row.trained_at,
            training_cutoff=row.training_cutoff,
            metrics=dict(row.metrics),
            feature_list=tuple(row.feature_list),
            sha256=bytes(row.sha256).hex(),
            activated_at=row.activated_at,
        )


_META = [c for c in model_artifacts.columns if c.name != "blob"]


def store_artifact(
    session: Session,
    clock: Clock,
    model: TrainedModel,
    *,
    dataset_id: UUID | None,
    job_id: UUID | None,
) -> StoredArtifact:
    """Store ``model`` in the caller's transaction and apply the gate rules (module docstring)."""
    session.execute(select(func.pg_advisory_xact_lock(MODEL_ARTIFACT_LOCK_KEY)))
    a = model_artifacts.c
    now = clock.now()
    current = session.execute(
        select(a.id).where(a.kind == model.kind.value, a.status == ArtifactStatus.ACTIVE.value)
    ).scalar_one_or_none()
    version = (
        session.execute(select(func.max(a.version)).where(a.kind == model.kind.value)).scalar_one()
        or 0
    ) + 1
    activate = model.gate_passed or current is None
    if activate and current is not None:
        session.execute(
            update(model_artifacts)
            .where(a.id == current)
            .values(status=ArtifactStatus.RETIRED.value, retired_at=now)
        )
    status = ArtifactStatus.ACTIVE if activate else ArtifactStatus.FAILED_GATE
    artifact_id = uuid7(unix_ms=int(now.timestamp() * 1000))
    session.execute(
        model_artifacts.insert().values(
            id=artifact_id,
            kind=model.kind.value,
            version=version,
            status=status.value,
            gate_passed=model.gate_passed,
            dataset_id=dataset_id,
            job_id=job_id,
            trained_at=now,
            training_cutoff=model.training_cutoff,
            metrics=dict(model.metrics),
            feature_list=list(model.feature_list),
            blob=model.blob,
            sha256=hashlib.sha256(model.blob).digest(),
            activated_at=now if activate else None,
        )
    )
    payload = {
        "kind": model.kind.value,
        "version": version,
        "status": status.value,
        "gate_passed": model.gate_passed,
        "gate_reasons": list(model.gate_reasons),
    }
    record(
        session,
        clock,
        actor_type=ActorType.JOB,
        actor_id=str(job_id) if job_id is not None else None,
        action="sensing.artifact.stored",
        entity_type="model_artifact",
        entity_id=str(artifact_id),
        payload=payload,
    )
    if activate:
        publish(session, EventType.ARTIFACT_ACTIVATED, str(artifact_id), payload)
        if not model.gate_passed:
            log.warning("sensing.artifact_below_gate", **payload)
    row = session.execute(select(*_META).where(a.id == artifact_id)).one()
    return StoredArtifact.of(row)


def active_artifacts(conn: Connection | Session) -> dict[ArtifactKind, StoredArtifact]:
    """The ACTIVE artifact of each kind that has one."""
    rows = conn.execute(
        select(*_META).where(model_artifacts.c.status == ArtifactStatus.ACTIVE.value)
    ).all()
    return {ArtifactKind(r.kind): StoredArtifact.of(r) for r in rows}


def artifacts_of_job(
    conn: Connection | Session, job_id: UUID
) -> dict[ArtifactKind, StoredArtifact]:
    """What a ``TRAIN_SENSING`` job stored already, so a retried job doesn't train twice."""
    rows = conn.execute(select(*_META).where(model_artifacts.c.job_id == job_id)).all()
    return {ArtifactKind(r.kind): StoredArtifact.of(r) for r in rows}


def load_blob(conn: Connection | Session, artifact_id: UUID) -> bytes:
    """The artifact's blob, after checking its SHA-256. Load it only from this table."""
    row = conn.execute(
        select(model_artifacts.c.blob, model_artifacts.c.sha256).where(
            model_artifacts.c.id == artifact_id
        )
    ).one()
    blob = bytes(row.blob)
    if hashlib.sha256(blob).digest() != bytes(row.sha256):
        raise ArtifactIntegrityError(f"model artifact {artifact_id}: the blob fails its SHA-256")
    return blob
