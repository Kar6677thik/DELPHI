"""Label delivery (docs/14 §7–8, docs/17 `labels`, #159): the seed delivers the labels due by the
end of the history, LABEL_DELIVERY delivers the rest as they fall due, once, and the app role
reads `delphi.labels` but never `sim_truth`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobQueue, JobRegistry, JobStatus, JobType, QueueConfig, enqueue
from delphi.platform.labels import labels
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sim import api
from delphi.sim.catalog import datasets
from delphi.sim.delivery import deliver_labels
from delphi.sim.generator_config import history_end, load_generator_config
from delphi.sim.jobs import register_sim_jobs
from delphi.sim.labels import FRAUD_OTHER, LabelSource
from delphi.sim.truth import label_schedule

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)  # after every `test` label is due
WORKER = "worker-1"
END = history_end(load_generator_config(), "test")
SOON = END + timedelta(days=2)  # the worker's clock at the seed: some labels are already due


@dataclass
class Bank:
    app: Engine
    sim: Engine
    sessions: sessionmaker[Session]
    registry: JobRegistry
    clock: FixedClock

    def run(self, job_type: JobType, key: str, **params: Any) -> dict[str, Any]:
        queue = JobQueue(self.sessions, self.clock, QueueConfig())
        with self.sessions.begin() as s:
            job_id = enqueue(s, self.clock, job_type, idempotency_key=key, params=params).job_id
        job = queue.claim(WORKER, frozenset({job_type}))
        assert job is not None and job.id == job_id
        run_job(queue, self.registry, job, WORKER)
        with self.sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        assert row["status"] == JobStatus.SUCCEEDED, row["last_error"]
        result: dict[str, Any] = row["result"]
        return result

    def schedule(self) -> list[Any]:
        ls = label_schedule
        with self.sim.connect() as conn:
            return list(
                conn.execute(
                    select(
                        ls.c.subject_type,
                        ls.c.subject_id,
                        ls.c.hypothesis_key,
                        ls.c.label_source,
                        ls.c.due_at,
                        ls.c.delivered,
                    )
                )
            )

    def delivered(self) -> dict[tuple[str, UUID, str], tuple[str, datetime]]:
        """What the app role reads from `delphi.labels`, through the sim facade."""
        with self.app.connect() as conn:
            rows = conn.execute(
                select(
                    api.labels.c.subject_type,
                    api.labels.c.subject_id,
                    api.labels.c.label_source,
                    api.labels.c.hypothesis_key,
                    api.labels.c.known_at,
                )
            )
            return {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows}


@pytest.fixture
def seeded(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Bank]:
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(SOON)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    bank = Bank(app, sim, make_session_factory(app), registry, clock)
    bank.run(JobType.GENERATE_DATASET, "seed", profile="test")
    yield bank
    app.dispose()
    sim.dispose()


def test_the_seed_delivers_exactly_the_labels_due_by_the_end_of_history(
    seeded: Bank, kb: KnowledgeBase
) -> None:
    schedule = seeded.schedule()
    due = [r for r in schedule if r.due_at <= END]
    assert due and len(due) < len(schedule)  # the `test` schedule runs past its history
    assert all(r.delivered == (r.due_at <= END) for r in schedule)
    known = seeded.delivered()
    assert set(known) == {(r.subject_type, r.subject_id, r.label_source) for r in due}
    for r in due:
        key, known_at = known[(r.subject_type, r.subject_id, r.label_source)]
        assert known_at == r.due_at
        hidden = r.label_source == LabelSource.CUSTOMER_COMPLAINT and r.hypothesis_key not in (
            kb.hypotheses
        )
        assert key == (FRAUD_OTHER if hidden else r.hypothesis_key)
    with seeded.sim.connect() as conn:
        stats = conn.execute(select(datasets.c.stats)).scalar_one()
    assert stats["rows"]["labels"] == len(due)


def test_label_delivery_delivers_what_falls_due_once(seeded: Bank) -> None:
    later = [r for r in seeded.schedule() if r.due_at > END]
    first = seeded.run(JobType.LABEL_DELIVERY, "tick-1")
    assert first["delivered"] == sum(r.due_at <= SOON for r in later) > 0
    assert seeded.run(JobType.LABEL_DELIVERY, "tick-2")["delivered"] == 0  # nothing new
    seeded.clock.advance(T0 - SOON)
    rest = seeded.run(JobType.LABEL_DELIVERY, "tick-3")
    assert rest["delivered"] == sum(r.due_at > SOON for r in later)
    assert len(seeded.delivered()) == len(seeded.schedule())
    assert all(r.delivered for r in seeded.schedule())


def test_a_complaint_outside_the_active_kb_is_fraud_other(seeded: Bank) -> None:
    """docs/14 §7: a complaint names its true hypothesis only if the ACTIVE KB has it."""
    with seeded.sim.begin() as conn:
        delivered = deliver_labels(conn, until=T0, active_hypotheses=frozenset({"GENUINE"}), now=T0)
    assert delivered > 0
    schedule = {(r.subject_type, r.subject_id, r.label_source): r for r in seeded.schedule()}
    complaints = 0
    for key, (hypothesis, _) in seeded.delivered().items():
        row = schedule[key]
        if row.due_at <= END:
            continue  # delivered at the seed, under the worker's KB
        if row.label_source == LabelSource.CUSTOMER_COMPLAINT:
            complaints += 1
            assert hypothesis == FRAUD_OTHER
        else:
            assert hypothesis == row.hypothesis_key
    assert complaints > 0


def test_the_app_role_reads_labels_but_not_the_schedule(seeded: Bank) -> None:
    with seeded.app.connect() as conn:
        assert conn.execute(select(func.count()).select_from(labels)).scalar_one() > 0
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(select(func.count()).select_from(label_schedule))
