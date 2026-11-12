"""The GENERATE_DATASET job (docs/04 §5.4, docs/14 §6, docs/17 §3, #19): real PostgreSQL, the
real queue and worker step, the `test` profile with the personas (as the E2E stack runs it)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import (
    Job,
    JobContext,
    JobQueue,
    JobRegistry,
    JobStatus,
    JobType,
    QueueConfig,
    enqueue,
)
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sim import bank
from delphi.sim.catalog import datasets
from delphi.sim.dataset import generate_dataset
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.persona_config import load_persona_config
from delphi.sim.personas import load_manifest
from delphi.sim.response_models import load_config
from delphi.sim.truth import persona_manifests
from delphi.sim.writer import write_dataset

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
TYPES = frozenset({JobType.GENERATE_DATASET})
WORKER = "worker-1"


@dataclass
class Stack:
    sessions: sessionmaker[Session]  # delphi_app, as the worker's platform pool
    sim: Engine  # delphi_sim, the worker's second pool
    queue: JobQueue
    registry: JobRegistry

    def enqueue(self, key: str, **params: Any) -> UUID:
        with self.sessions.begin() as s:
            queued = enqueue(
                s, self.queue.clock, JobType.GENERATE_DATASET, idempotency_key=key, params=params
            )
        return queued.job_id

    def run_next(self) -> UUID:
        job = self.queue.claim(WORKER, TYPES)
        assert job is not None
        run_job(self.queue, self.registry, job, WORKER)
        return job.id

    def job(self, job_id: UUID) -> Mapping[str, Any]:
        with self.sessions() as s:
            return s.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()

    def context(self, job_id: UUID, worker_id: str = WORKER) -> JobContext:
        with self.sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == job_id)).one()
        return JobContext(job=Job.from_row(row), queue=self.queue, worker_id=worker_id)

    def count(self, table: str) -> int:
        with self.sim.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


@pytest.fixture
def stack(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Stack]:
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(T0)
    sessions = make_session_factory(app)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    yield Stack(sessions, sim, JobQueue(sessions, clock, QueueConfig()), registry)
    app.dispose()
    sim.dispose()


def _train_jobs(stack: Stack) -> list[dict[str, Any]]:
    with stack.queue.sessions() as s:
        rows = s.execute(select(jobs.c.params).where(jobs.c.type == JobType.TRAIN_SENSING.value))
        return [dict(params) for (params,) in rows]


def test_the_job_queues_sensing_training_on_its_dataset(stack: Stack) -> None:
    """docs/13 §7: the seed dataset is followed by one TRAIN_SENSING job (#88)."""
    job_id = stack.enqueue("seed-test", profile="test")
    stack.run_next()
    result = stack.job(job_id)["result"]
    assert _train_jobs(stack) == [{"dataset_id": result["dataset_id"]}]
    assert result["train_job_id"]


def test_the_job_writes_the_dataset_its_record_and_the_manifest(
    stack: Stack, kb: KnowledgeBase
) -> None:
    job_id = stack.enqueue("seed-test", profile="test", personas=True)
    assert stack.run_next() == job_id
    job = stack.job(job_id)
    assert job["status"] == JobStatus.SUCCEEDED and job["progress"] == 1.0
    result = job["result"]
    assert result["profile"] == "test" and result["seed"] == 1
    assert result["generator_version"] == load_config().generator_version
    for table in bank.metadata.tables.values():  # account_scores: written by sensing, not here
        assert stack.count(table.fullname) == result["rows"].get(table.name, 0), table.name
    with stack.sim.connect() as conn:
        record = conn.execute(select(datasets)).mappings().one()
        stored = conn.execute(select(persona_manifests.c.manifest)).scalar_one()
    assert str(record["id"]) == result["dataset_id"] and record["job_id"] == job_id
    assert record["status"] == "READY" and record["seed_completed_at"] == T0
    assert record["stats"]["report"]["profile"] == "test"
    expected = generate_dataset(load_generator_config(), "test", kb=kb, personas=True)
    assert load_manifest(stored, load_persona_config()) == expected.personas


def test_a_retried_attempt_returns_the_committed_dataset(stack: Stack) -> None:
    """At least once: an attempt that committed but lost its job row is not written twice."""
    stack.enqueue("seed-test", profile="test")
    job_id = stack.run_next()
    first = stack.job(job_id)["result"]
    again = stack.registry.handlers[JobType.GENERATE_DATASET](stack.context(job_id))
    assert again == first
    assert _train_jobs(stack) == [{"dataset_id": first["dataset_id"]}]  # queued once (#88)
    with stack.sim.connect() as conn:
        assert conn.execute(select(func.count()).select_from(datasets)).scalar_one() == 1


def test_a_second_dataset_is_refused_and_leaves_nothing_behind(stack: Stack) -> None:
    stack.enqueue("first", profile="test")
    stack.run_next()
    customers = stack.count("bank.customers")
    second = stack.enqueue("second", profile="test", seed=2)
    stack.run_next()
    job = stack.job(second)
    assert job["status"] == JobStatus.QUEUED  # retried later, as any failed attempt
    assert "already holds dataset" in job["error"]
    assert stack.count("bank.customers") == customers
    assert stack.count("delphi.datasets") == 1


def test_a_job_that_lost_its_lease_writes_nothing(stack: Stack) -> None:
    job_id = stack.enqueue("lost", profile="test")
    job = stack.queue.claim(WORKER, TYPES)
    assert job is not None and job.id == job_id
    handler = stack.registry.handlers[JobType.GENERATE_DATASET]
    with pytest.raises(RuntimeError, match="no longer this worker's"):
        handler(stack.context(job_id, worker_id="someone-else"))
    assert stack.count("bank.customers") == 0
    assert stack.count("delphi.datasets") == 0


def test_bad_parameters_fail_the_attempt(stack: Stack) -> None:
    job_id = stack.enqueue("bad", profile="huge")
    stack.run_next()
    job = stack.job(job_id)
    assert job["status"] == JobStatus.QUEUED and "unknown profile" in job["error"]
    assert stack.count("delphi.datasets") == 0


def test_rows_no_dataset_accounts_for_are_not_overwritten(stack: Stack, kb: KnowledgeBase) -> None:
    """A bank written some other way (e.g. by hand with the writer) is left alone."""
    with stack.sim.begin() as conn:
        write_dataset(conn, generate_dataset(load_generator_config(), "test", kb=kb))
    job_id = stack.enqueue("over-the-top", profile="test")
    stack.run_next()
    assert "no dataset accounts for" in stack.job(job_id)["error"]
    assert stack.count("delphi.datasets") == 0
