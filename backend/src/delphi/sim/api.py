"""The simulator's facade for the api, the CLI and the feature modules (docs/04 §4.1 rule 3):
asking the worker for a dataset (docs/16 §2 `POST /admin/datasets`, docs/23 §4 `delphi seed`,
#19) or a demo story (`POST /admin/demo/scenarios/{story}/run`, #101), the mock phone's
payments and personas (#98), and the `bank` schema's table definitions.

Feature modules read the simulated bank through these re-exported `bank` tables (the probes'
sim adapters, sensing, the Navigator evidence rules), so the schema is defined once, in
`delphi.sim.bank`. They are **read-only** for everyone but the simulator, except
`account_scores`, which the mule scorer writes (docs/13 §6). `labels` (`delphi.labels`, #159) is
what the bank has observed so far, delivered from the simulator's schedule by `LABEL_DELIVERY`;
the sensing trainers read it. Nothing from `sim_truth` is exported: this module reads no ground
truth, so the api may import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, get_args
from uuid import UUID

from sqlalchemy import Connection, exists, func, select
from sqlalchemy.orm import Session

from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, InvalidStateTransitionError, ProblemCode
from delphi.platform.jobs import (
    Enqueued,
    JobStatus,
    JobType,
    enqueue,
    get_job,
    requeue_failed,
)
from delphi.platform.labels import labels  # the labels the bank has observed (#159)
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
from delphi.sim.baselines import Baseline, baseline_decisions
from delphi.sim.catalog import datasets
from delphi.sim.external import cfcfrms_ack_delay
from delphi.sim.generator_config import GeneratorConfig, history_end, load_generator_config
from delphi.sim.payments import (
    STATUS_FOR_DECISION,
    PaymentStatus,
    PaymentView,
    apply_case_disposition,
    apply_guardian_decision,
    cancel_payment,
    get_payment,
    get_payments,
)
from delphi.sim.persona_config import load_persona_config
from delphi.sim.phone import (
    PHONE_CHANNELS,
    PayChannel,
    PaymentRequest,
    Persona,
    PhoneTelemetry,
    customer_exists,
    initiate_payment,
    phone_personas,
)
from delphi.sim.population import HOME_BANK

# The read-only `bank` tables re-exported for feature modules, and this facade's own API.
__all__ = [
    "DATASET_REQUEST_LOCK_KEY",
    "HOME_BANK",
    "PHONE_CHANNELS",
    "PROFILES",
    "RUNNABLE_STORIES",
    "STATUS_FOR_DECISION",
    "STORIES",
    "Baseline",
    "DatasetInfo",
    "DatasetProfile",
    "GeneratorConfig",
    "HistoryWindow",
    "PayChannel",
    "PaymentRequest",
    "PaymentStatus",
    "PaymentView",
    "Persona",
    "PhoneTelemetry",
    "StoryMode",
    "StoryName",
    "account_scores",
    "accounts",
    "apply_case_disposition",
    "apply_guardian_decision",
    "baseline_decisions",
    "cancel_payment",
    "cfcfrms_ack_delay",
    "complaints",
    "crm_events",
    "current_dataset",
    "customer_exists",
    "customers",
    "dataset_exists",
    "devices",
    "get_payment",
    "get_payments",
    "history_window",
    "initiate_payment",
    "labels",
    "ledger_entries",
    "load_generator_config",
    "login_events",
    "payees",
    "payments",
    "phone_personas",
    "refuse_during_reset",
    "request_dataset",
    "request_reset",
    "request_story",
    "scripted_priors",
    "session_telemetry",
    "sim_phone_risk",
    "sim_registry_listings",
    "story_of_job",
]

# The generator's profiles (docs/14 §2); a test checks they match `generator.yaml`.
DatasetProfile = Literal["test", "demo", "eval"]
PROFILES: tuple[str, ...] = get_args(DatasetProfile)
_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)

# The demo stories (docs/14 §6, §10) and how each is played; S-3 and S-4 run in a later release.
StoryName = Literal["S1", "S2", "S3", "S4"]
STORIES: tuple[str, ...] = get_args(StoryName)
RUNNABLE_STORIES: tuple[str, ...] = ("S1", "S2")
StoryMode = Literal["interactive", "autoplay"]


# What a staged story payment must match: its job's result `payment` (stories._payment_body).
_STAGED_FIELDS = ("payer_account_id", "payee_account_id", "amount_paise", "channel", "device_id")


def story_of_job(session: Session, job_id: UUID, request: PaymentRequest) -> str:
    """The story a phone payment plays (docs/14 §10, #291): the story of the interactive
    `DEMO_STORY` job that staged it. The server sets it, never the client: the job must exist,
    have succeeded, and have staged exactly this payment (payer, payee, amount, channel and
    device). Anything else is a 422."""
    job = get_job(session, job_id)
    result = job.result if job is not None else None
    staged = result.get("payment") if result else None
    sent = {
        "payer_account_id": str(request.payer_account_id),
        "payee_account_id": str(request.payee_account_id),
        "amount_paise": request.amount_paise,
        "channel": request.channel,
        "device_id": str(request.device_id),
    }
    if (
        job is None
        or job.type != JobType.DEMO_STORY.value
        or job.status != JobStatus.SUCCEEDED.value
        or result is None
        or result.get("mode") != "interactive"
        or not isinstance(staged, Mapping)
        or any(staged.get(f) != sent[f] for f in _STAGED_FIELDS)
        or result.get("story") not in RUNNABLE_STORIES
    ):
        raise DelphiError(
            "story_job_id does not name the interactive demo story that staged this payment.",
            code=ProblemCode.VALIDATION_FAILED,
        )
    return str(result["story"])


def scripted_priors() -> dict[str, dict[str, float]]:
    """Each runnable story's scripted prior (docs/06 §17, #291), from `personas.yaml`: the
    Guardian's composition root starts a story payment's session from it."""
    stories = load_persona_config().stories
    return {
        name: dict(stories[name].scripted_prior or {})
        for name in RUNNABLE_STORIES
        if stories[name].scripted_prior
    }


def dataset_exists(session: Session) -> bool:
    return bool(session.execute(select(exists().select_from(datasets))).scalar_one())


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """The dataset in the bank, as feature modules see it (never its ground truth)."""

    id: UUID
    profile: str
    seed: int
    created_at: datetime
    # Where the generated history ends (`stats.history_end`, #161); None on older datasets.
    history_end: datetime | None = None


def current_dataset(conn: Connection | Session) -> DatasetInfo | None:
    """The bank holds at most one dataset (docs/14 §10); ``None`` before seeding."""
    d = datasets.c
    row = conn.execute(
        select(d.id, d.profile, d.seed, d.created_at, d.stats["history_end"].astext).limit(1)
    ).first()
    if row is None:
        return None
    ended = datetime.fromisoformat(row[4]) if row[4] else None
    return DatasetInfo(row.id, row.profile, row.seed, row.created_at, ended)


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """The dataset's generated history, as the sensing trainers need it (docs/13 §4): it runs
    from ``start`` to ``end``, and a GENUINE label arrives ``maturity_days`` after a payment."""

    start: datetime
    end: datetime
    maturity_days: int


def history_window(dataset: DatasetInfo) -> HistoryWindow:
    """docs/14 §2: where the dataset's history ends, as the seed recorded it
    (``stats.history_end``). A dataset written before that was recorded falls back to the
    profile's rule: `demo` at its ``created_at`` (within the generation time), `test` and `eval`
    at the generator's fixed epoch."""
    config = load_generator_config()
    profile = config.profiles[dataset.profile]
    end = dataset.history_end or history_end(config, dataset.profile, created_at=dataset.created_at)
    return HistoryWindow(end - timedelta(days=profile.history_days), end, profile.maturity_days)


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


def refuse_during_reset(session: Session) -> None:
    """409 INVALID_STATE_TRANSITION while a `DEMO_RESET` is queued or running: a payment or a
    story started now would race the reset's deletes (docs/14 §10)."""
    pending = session.execute(
        select(jobs.c.id)
        .where(jobs.c.type == JobType.DEMO_RESET.value, jobs.c.status.in_(_ACTIVE))
        .limit(1)
    ).scalar_one_or_none()
    if pending is not None:
        raise InvalidStateTransitionError(
            f"A demo reset is in progress (job {pending}); try again when it has finished."
        )


def request_story(
    session: Session,
    clock: Clock,
    *,
    story: str,
    mode: StoryMode,
    idempotency_key: str,
    created_by: UUID | None = None,
) -> Enqueued:
    """Queue a `DEMO_STORY` job for the worker, which holds the persona manifest (docs/14
    §10). The caller checks the story is one of ``RUNNABLE_STORIES`` (the job refuses the
    others). A key already used returns its job; otherwise 409 INVALID_STATE_TRANSITION while
    a demo reset is queued or running."""
    known = session.execute(
        select(jobs.c.id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if known is not None:
        return Enqueued(known, created=False)
    refuse_during_reset(session)
    return enqueue(
        session,
        clock,
        JobType.DEMO_STORY,
        idempotency_key=idempotency_key,
        params={"story": story, "mode": mode},
        created_by=created_by,
    )


def request_reset(
    session: Session,
    clock: Clock,
    *,
    idempotency_key: str,
    created_by: UUID | None = None,
) -> Enqueued:
    """Queue a `DEMO_RESET` job (docs/14 §10): the worker deletes what the demo did since the
    seed. A key already used returns its job. Otherwise 409 INVALID_STATE_TRANSITION while the
    bank holds no dataset, and 409 JOB_ALREADY_RUNNING while a reset is queued or running."""
    known = session.execute(
        select(jobs.c.id).where(jobs.c.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if known is not None:
        return Enqueued(known, created=False)
    if not dataset_exists(session):
        raise InvalidStateTransitionError("The bank holds no dataset: there is nothing to reset.")
    running = session.execute(
        select(jobs.c.id)
        .where(jobs.c.type == JobType.DEMO_RESET.value, jobs.c.status.in_(_ACTIVE))
        .limit(1)
    ).scalar_one_or_none()
    if running is not None:
        raise DelphiError(
            f"A demo reset is already under way (job {running}).",
            code=ProblemCode.JOB_ALREADY_RUNNING,
        )
    return enqueue(
        session,
        clock,
        JobType.DEMO_RESET,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )
