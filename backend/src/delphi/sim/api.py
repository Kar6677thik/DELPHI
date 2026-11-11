"""The simulator's facade for the api, the CLI and the feature modules (docs/04 §4.1 rule 3):
asking the worker for a dataset (docs/16 §2 `POST /admin/datasets`, docs/23 §4 `delphi seed`,
#19), and the `bank` schema's table definitions.

Feature modules read the simulated bank through these re-exported `bank` tables (the probes'
sim adapters, sensing, the Navigator evidence rules), so the schema is defined once, in
`delphi.sim.bank`. They are **read-only** for everyone but the simulator, except
`account_scores`, which the mule scorer writes (docs/13 §6). Nothing from
`sim_truth` is exported: this module reads no ground truth, so the api may import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from sqlalchemy import Connection, exists, func, select
from sqlalchemy.orm import Session

from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, InvalidStateTransitionError, ProblemCode
from delphi.platform.jobs import Enqueued, JobStatus, JobType, enqueue, requeue_failed
from delphi.platform.locks import DATASET_REQUEST_LOCK_KEY
from delphi.platform.tables import jobs
from delphi.sim.bank import (
    account_scores,
    accounts,
    complaints,
    crm_events,
    customers,
    devices,
    ledger_entries,
    login_events,
    payees,
    payments,
    session_telemetry,
    sim_phone_risk,
    sim_registry_listings,
)
from delphi.sim.catalog import datasets

# The read-only `bank` tables re-exported for feature modules, and this facade's own API.
__all__ = [
    "DATASET_REQUEST_LOCK_KEY",
    "PROFILES",
    "DatasetInfo",
    "DatasetProfile",
    "account_scores",
    "accounts",
    "complaints",
    "crm_events",
    "current_dataset",
    "customers",
    "dataset_exists",
    "devices",
    "ledger_entries",
    "login_events",
    "payees",
    "payments",
    "request_dataset",
    "session_telemetry",
    "sim_phone_risk",
    "sim_registry_listings",
]

# The generator's profiles (docs/14 §2); a test checks they match `generator.yaml`.
DatasetProfile = Literal["test", "demo", "eval"]
PROFILES: tuple[str, ...] = get_args(DatasetProfile)
_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def dataset_exists(session: Session) -> bool:
    return bool(session.execute(select(exists().select_from(datasets))).scalar_one())


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """The dataset in the bank, as feature modules see it (never its ground truth)."""

    id: UUID
    profile: str
    seed: int
    created_at: datetime


def current_dataset(conn: Connection | Session) -> DatasetInfo | None:
    """The bank holds at most one dataset (docs/14 §10); ``None`` before seeding."""
    row = conn.execute(
        select(datasets.c.id, datasets.c.profile, datasets.c.seed, datasets.c.created_at).limit(1)
    ).first()
    return DatasetInfo(row.id, row.profile, row.seed, row.created_at) if row is not None else None


def request_dataset(
    session: Session,
    clock: Clock,
    *,
    idempotency_key: str,
    params: Mapping[str, Any],
    created_by: UUID | None = None,
    allowed_profiles: frozenset[str] | None = None,
) -> Enqueued:
    """Queue a `GENERATE_DATASET` job in the caller's transaction. A request with a key already
    used returns that job (so `delphi seed` can run on every deploy), and queues it again if it
    had FAILED (#78), as `POST /jobs/{id}/retry` would. Otherwise the bank must hold no dataset
    (409 INVALID_STATE_TRANSITION) and no generation may be under way (409
    JOB_ALREADY_RUNNING): the bank tables hold one dataset."""
    profile = params.get("profile")
    if allowed_profiles is not None and profile not in allowed_profiles:
        raise DelphiError(
            f"Profile {profile!r} is turned off on this deployment "
            f"(DELPHI_DATASET_PROFILES: {', '.join(sorted(allowed_profiles))}).",
            code=ProblemCode.VALIDATION_FAILED,
        )
    session.execute(select(func.pg_advisory_xact_lock(DATASET_REQUEST_LOCK_KEY)))
    known = session.execute(
        select(jobs.c.id, jobs.c.status).where(jobs.c.idempotency_key == idempotency_key)
    ).one_or_none()
    if known is not None and known.status != JobStatus.FAILED.value:
        return Enqueued(known.id, created=False)
    _refuse_a_second_dataset(session)
    if known is not None:  # it FAILED: give it a fresh set of attempts
        requeued = requeue_failed(session, clock, known.id)
        return Enqueued(known.id, created=False, requeued=requeued)
    return enqueue(
        session,
        clock,
        JobType.GENERATE_DATASET,
        idempotency_key=idempotency_key,
        params=params,
        created_by=created_by,
    )


def _refuse_a_second_dataset(session: Session) -> None:
    if dataset_exists(session):
        raise InvalidStateTransitionError(
            "The database already holds a dataset; reset it before generating another."
        )
    running = session.execute(
        select(jobs.c.id)
        .where(jobs.c.type == JobType.GENERATE_DATASET.value, jobs.c.status.in_(_ACTIVE))
        .limit(1)
    ).scalar_one_or_none()
    if running is not None:
        raise DelphiError(
            f"A dataset is already being generated (job {running}).",
            code=ProblemCode.JOB_ALREADY_RUNNING,
        )
