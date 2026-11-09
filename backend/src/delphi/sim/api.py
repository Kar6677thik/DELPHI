"""The simulator's facade for the api, the CLI and the feature modules (docs/04 §4.1 rule 3):
asking the worker for a dataset (docs/16 §2 `POST /admin/datasets`, docs/23 §4 `delphi seed`,
#19), and the `bank` schema's table definitions.

Feature modules read the simulated bank through these re-exported `bank` tables (the probes'
sim adapters, sensing, the Navigator evidence rules), so the schema is defined once, in
`delphi.sim.bank`. They are **read-only** for everyone but the simulator. Nothing from
`sim_truth` is exported: this module reads no ground truth, so the api may import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, InvalidStateTransitionError, ProblemCode
from delphi.platform.jobs import Enqueued, JobStatus, JobType, enqueue
from delphi.platform.tables import jobs
from delphi.sim.bank import (
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
    "DatasetProfile",
    "accounts",
    "complaints",
    "crm_events",
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
# pg_advisory_xact_lock key that serialises dataset requests (44 serialises the writes).
DATASET_REQUEST_LOCK_KEY = 45
_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def dataset_exists(session: Session) -> bool:
    return bool(session.execute(select(exists().select_from(datasets))).scalar_one())


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
    used returns that job (so `delphi seed` can run on every deploy). Otherwise the bank must
    hold no dataset (409 INVALID_STATE_TRANSITION) and no generation may be under way (409
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
        select(jobs.c.id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if known is not None:
        return Enqueued(known, created=False)
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
    return enqueue(
        session,
        clock,
        JobType.GENERATE_DATASET,
        idempotency_key=idempotency_key,
        params=params,
        created_by=created_by,
    )
