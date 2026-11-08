"""The PostgreSQL job queue (FR-PLT-07, docs/04 §5.4) against a real database, as delphi_app."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import FixedClock, SystemClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import (
    JobContext,
    JobQueue,
    JobRegistry,
    JobStatus,
    JobType,
    Lane,
    QueueConfig,
    enqueue,
)
from delphi.platform.retention import RetentionSweep
from delphi.platform.scheduler import Recurring, Scheduler
from delphi.platform.settings import Settings
from delphi.platform.tables import jobs, outbox_events
from delphi.platform.wiring import Platform
from delphi.platform.worker import run_job, run_worker, sample_queue_depth

from ._db import MIGRATIONS_DIR, engine_as

T0 = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
CONFIG = QueueConfig(lease=timedelta(seconds=60), retry_backoff=timedelta(seconds=30))


@pytest.fixture
def db(create_database: Callable[[], str]) -> Iterator[tuple[str, Engine]]:
    """A fresh database per test (jobs are global state), used through the app role."""
    from ._db import upgrade

    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    yield url, engine
    engine.dispose()


@pytest.fixture
def sessions(db: tuple[str, Engine]) -> sessionmaker[Session]:
    return make_session_factory(db[1])


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def queue(sessions: sessionmaker[Session], clock: FixedClock) -> JobQueue:
    return JobQueue(sessions, clock, CONFIG)


def add(
    sessions: sessionmaker[Session],
    clock: FixedClock,
    key: str,
    job_type: JobType = JobType.DEMO_RESET,
    **kw: Any,
) -> UUID:
    with sessions.begin() as s:
        return enqueue(s, clock, job_type, idempotency_key=key, **kw).job_id


def status_of(sessions: sessionmaker[Session], job_id: UUID) -> Mapping[str, Any]:
    with sessions() as s:
        return s.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()


DEMO = frozenset({JobType.DEMO_RESET})


def job_count(job_type: JobType, outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "delphi_jobs_total", {"type": job_type.value, "status": outcome}
    )
    return value or 0.0


def test_the_queue_depth_gauge_counts_waiting_jobs_per_type(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    add(sessions, clock, "a")
    add(sessions, clock, "b")
    add(sessions, clock, "c", job_type=JobType.DEMO_STORY)
    claimed = queue.claim("w", frozenset({JobType.DEMO_STORY}))
    assert claimed is not None  # RUNNING now: no longer waiting

    sample_queue_depth(queue)

    def depth(job_type: JobType) -> float | None:
        return REGISTRY.get_sample_value("delphi_jobs_queued", {"type": job_type.value})

    assert depth(JobType.DEMO_RESET) == 2
    assert depth(JobType.DEMO_STORY) == 0
    assert depth(JobType.AUDIT_VERIFY) == 0  # every type is reported, not only those waiting


def test_enqueue_is_idempotent_on_the_key(
    sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    with sessions.begin() as s:
        first = enqueue(s, clock, JobType.DEMO_RESET, idempotency_key="k", params={"a": 1})
        again = enqueue(s, clock, JobType.DEMO_RESET, idempotency_key="k", params={"a": 2})
    assert first.created and not again.created
    assert again.job_id == first.job_id
    assert status_of(sessions, first.job_id)["params"] == {"a": 1}


def test_claim_takes_due_jobs_of_the_given_types_oldest_first(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    later = add(sessions, clock, "later", run_after=T0 + timedelta(minutes=5))
    clock.advance(timedelta(seconds=1))
    first = add(sessions, clock, "first")
    add(sessions, clock, "other-type", job_type=JobType.SIM_TICK)

    job = queue.claim("w1", DEMO)
    assert job is not None and job.id == first
    assert job.status is JobStatus.RUNNING and job.attempts == 1
    assert queue.claim("w1", DEMO) is None  # `later` is not due yet; SIM_TICK not asked for
    assert queue.succeed(job, "w1", None)  # else its lease would expire and it would come back
    clock.advance(timedelta(minutes=5))
    nxt = queue.claim("w1", DEMO)
    assert nxt is not None and nxt.id == later


def test_concurrent_workers_never_claim_the_same_job(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    ids = {add(sessions, clock, f"job-{i}") for i in range(20)}
    claimed: list[UUID] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while (job := queue.claim(name, DEMO)) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every job exactly once (SKIP LOCKED)


def test_a_crashed_job_is_claimed_again_after_its_lease(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    """FR-PLT-07 acceptance: the worker dies mid-job; after `locked_until` another takes it."""
    job_id = add(sessions, clock, "crash")
    crashed = queue.claim("dead-worker", DEMO)
    assert crashed is not None

    clock.advance(timedelta(seconds=59))
    assert queue.claim("w2", DEMO) is None  # still leased
    clock.advance(timedelta(seconds=2))
    again = queue.claim("w2", DEMO)
    assert again is not None and again.id == job_id and again.attempts == 2

    # The dead worker's late writes no longer count.
    assert not queue.succeed(crashed, "dead-worker", {"x": 1})
    assert queue.succeed(again, "w2", {"done": True})
    row = status_of(sessions, job_id)
    assert row["status"] == "SUCCEEDED" and row["result"] == {"done": True}
    assert row["progress"] == 1.0 and row["locked_by"] is None


def test_a_job_whose_worker_dies_on_the_last_attempt_is_failed(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    job_id = add(sessions, clock, "doomed", max_attempts=1)
    assert queue.claim("dead", DEMO) is not None
    clock.advance(timedelta(minutes=2))
    assert queue.claim("w2", DEMO) is None  # no attempts left, so not re-claimed
    assert queue.reap_expired() == 1
    row = status_of(sessions, job_id)
    assert row["status"] == "FAILED" and "lease expired" in row["error"]


def test_failures_retry_with_exponential_backoff_then_fail(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    job_id = add(sessions, clock, "flaky")
    backoffs = []
    for _ in range(2):
        job = queue.claim("w", DEMO)
        assert job is not None
        assert queue.fail(job, "w", "boom") is JobStatus.QUEUED
        row = status_of(sessions, job_id)
        backoffs.append(row["run_after"] - clock.now())
        assert queue.claim("w", DEMO) is None  # not before the backoff
        clock.advance(row["run_after"] - clock.now())
    assert backoffs == [timedelta(seconds=30), timedelta(seconds=60)]

    last = queue.claim("w", DEMO)
    assert last is not None and last.attempts == 3
    assert queue.fail(last, "w", "boom") is JobStatus.FAILED
    assert status_of(sessions, job_id)["status"] == "FAILED"

    assert queue.retry(job_id)  # POST /jobs/{id}/retry: FAILED → QUEUED, fresh attempts
    again = queue.claim("w", DEMO)
    assert again is not None and again.attempts == 1
    assert not queue.retry(job_id)  # only FAILED jobs can be retried


def test_run_job_records_success_progress_and_failure(
    sessions: sessionmaker[Session], clock: FixedClock, queue: JobQueue
) -> None:
    seen: list[float] = []

    def ok(ctx: JobContext) -> Mapping[str, Any]:
        assert ctx.report(0.5)
        seen.append(status_of(sessions, ctx.job.id)["progress"])
        return {"rows": 3}

    def broken(_ctx: JobContext) -> None:
        raise RuntimeError("disk full")

    registry = JobRegistry()
    registry.register(JobType.DEMO_RESET, ok)
    registry.register(JobType.DEMO_STORY, broken)
    good = add(sessions, clock, "ok")
    bad = add(sessions, clock, "bad", job_type=JobType.DEMO_STORY)
    ok_before = job_count(JobType.DEMO_RESET, "SUCCEEDED")
    retry_before = job_count(JobType.DEMO_STORY, "RETRY")

    for types in (DEMO, frozenset({JobType.DEMO_STORY})):
        job = queue.claim("w", types)
        assert job is not None
        run_job(queue, registry, job, "w")

    assert seen == [0.5]
    assert job_count(JobType.DEMO_RESET, "SUCCEEDED") == ok_before + 1
    assert job_count(JobType.DEMO_STORY, "RETRY") == retry_before + 1
    assert status_of(sessions, good)["result"] == {"rows": 3}
    failed = status_of(sessions, bad)
    assert failed["status"] == "QUEUED"  # attempt 1 of 3
    assert failed["error"] == "RuntimeError: disk full"


def test_registry_lanes_and_duplicates() -> None:
    registry = JobRegistry()
    registry.register(JobType.SIM_TICK, lambda _c: None)
    registry.register(JobType.GENERATE_DATASET, lambda _c: None)
    assert registry.types(Lane.FAST) == {JobType.SIM_TICK}
    assert registry.types(Lane.DEFAULT) == {JobType.GENERATE_DATASET}
    with pytest.raises(ValueError, match="already registered"):
        registry.register(JobType.SIM_TICK, lambda _c: None)


def test_scheduler_enqueues_each_slot_once_even_with_two_schedulers(
    sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    registry = JobRegistry()
    registry.register(JobType.LABEL_DELIVERY, lambda _c: None)
    schedule = (
        Recurring(JobType.LABEL_DELIVERY, every=timedelta(seconds=60)),
        Recurring(JobType.SIM_TICK, every=timedelta(seconds=10)),  # no handler: never enqueued
    )
    a = Scheduler(sessions, clock, registry, schedule)
    b = Scheduler(sessions, clock, registry, schedule)
    for _ in range(4):  # ticks at :00, :20, :40 and 06:01:00, so two slots
        a.tick()
        b.tick()
        clock.advance(timedelta(seconds=20))
    with sessions() as s:
        keys = s.execute(select(jobs.c.idempotency_key, jobs.c.type)).all()
    assert sorted(k for k, _ in keys) == [
        "LABEL_DELIVERY:2026-11-07T06:00:00+00:00",
        "LABEL_DELIVERY:2026-11-07T06:01:00+00:00",
    ]


def test_retention_sweep_deletes_only_expired_rows(
    db: tuple[str, Engine], sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    with sessions.begin() as s:
        for hours_ago in (30, 1):
            s.execute(
                outbox_events.insert().values(
                    type="job.updated", payload={}, at=T0 - timedelta(hours=hours_ago)
                )
            )
    sweep = RetentionSweep(sessions, clock, timedelta(hours=24), timedelta(hours=24))
    result = sweep(None)  # type: ignore[arg-type]  # the sweep does not use its context
    assert result["outbox_events"] == 1
    assert sweep(None)["outbox_events"] == 0  # type: ignore[arg-type]  # idempotent
    with sessions() as s:
        assert s.execute(text("SELECT count(*) FROM delphi.outbox_events")).scalar_one() == 1


def test_the_worker_runs_jobs_end_to_end(db: tuple[str, Engine]) -> None:
    """The real worker loop with the system clock: a queued job is picked up and finished."""
    url, engine = db
    settings = Settings(
        database_url=url, migrations_dir=MIGRATIONS_DIR, worker_poll_interval_s=0.05
    )
    platform = Platform(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    done = threading.Event()

    def handler(_ctx: JobContext) -> Mapping[str, Any]:
        done.set()
        return {"ok": True}

    registry = JobRegistry()
    registry.register(JobType.DEMO_RESET, handler)
    with platform.sessions.begin() as s:
        job_id = enqueue(s, platform.clock, JobType.DEMO_RESET, idempotency_key="e2e").job_id

    stop = threading.Event()
    thread = threading.Thread(
        target=run_worker,
        args=(platform, stop),
        kwargs={"registry": registry, "serve_health": False},
    )
    thread.start()
    try:
        assert done.wait(10), "the worker never ran the job"
        for _ in range(100):
            if status_of(platform.sessions, job_id)["status"] == "SUCCEEDED":
                break
            stop.wait(0.05)
        assert status_of(platform.sessions, job_id)["status"] == "SUCCEEDED"
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_table_descriptions_match_the_migrated_schema(db: tuple[str, Engine]) -> None:
    """platform.tables describes tables that migrations create: the columns must agree."""
    from sqlalchemy import inspect

    from delphi.platform.tables import metadata

    insp = inspect(db[1])
    for table in metadata.tables.values():
        actual = {c["name"] for c in insp.get_columns(table.name, schema="delphi")}
        described = {c.name for c in table.columns}
        assert described <= actual, f"{table.name}: {sorted(described - actual)}"
