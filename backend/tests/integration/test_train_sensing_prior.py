"""The seed trains the prior (docs/13 §7, #88's last criteria, #159): GENERATE_DATASET writes the
`test` dataset and delivers its labels, queues TRAIN_SENSING, and TRAIN_SENSING's PRIOR trainer
stores an ACTIVE artifact from the delivered labels, end to end in 60 s or less."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobQueue, JobRegistry, JobStatus, JobType, QueueConfig, enqueue
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sensing.artifacts import ArtifactKind, ArtifactStatus, active_artifacts, load_blob
from delphi.sensing.jobs import register_sensing_jobs
from delphi.sensing.prior import PriorModel
from delphi.sim.generator_config import history_end, load_generator_config
from delphi.sim.jobs import register_sim_jobs

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
WORKER = "worker-1"
SECONDS = 60.0  # #88: `test` trains end to end in 60 s or less


def test_the_seed_trains_an_active_prior_from_delivered_labels(
    create_database: Callable[[], str], kb: KnowledgeBase
) -> None:
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(T0)
    sessions = make_session_factory(app)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    register_sensing_jobs(registry, engine=app, sessions=sessions, clock=clock)
    queue = JobQueue(sessions, clock, QueueConfig())

    def run(job_type: JobType) -> dict[str, Any]:
        job = queue.claim(WORKER, frozenset({job_type}))
        assert job is not None
        run_job(queue, registry, job, WORKER)
        with sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == job.id)).mappings().one()
        assert row["status"] == JobStatus.SUCCEEDED, row["last_error"]
        result: dict[str, Any] = row["result"]
        return result

    try:
        started = time.perf_counter()
        with sessions.begin() as s:
            enqueue(
                s,
                clock,
                JobType.GENERATE_DATASET,
                idempotency_key="seed",
                params={"profile": "test"},
            )
        seeded = run(JobType.GENERATE_DATASET)
        assert seeded["rows"]["labels"] > 0
        trained = run(JobType.TRAIN_SENSING)  # queued by the seed
        elapsed = time.perf_counter() - started

        assert trained["dataset_id"] == seeded["dataset_id"]
        assert [a["kind"] for a in trained["artifacts"]] == ["PRIOR"]
        assert set(trained["skipped"]) == {"ANOMALY", "MULE"}  # #90 and #91 register theirs
        with app.connect() as conn:
            prior = active_artifacts(conn)[ArtifactKind.PRIOR]
            model = PriorModel.from_bytes(load_blob(conn, prior.id))
        assert prior.status is ArtifactStatus.ACTIVE
        assert prior.dataset_id == UUID(seeded["dataset_id"])
        assert prior.training_cutoff == history_end(load_generator_config(), "test")
        report = model.report
        assert report["profile"] == "test" and report["n_train"] > 0
        assert not report["gate"]["enforced"]  # `test` reports its gate only (docs/13 §4)
        assert elapsed <= SECONDS, f"{elapsed:.1f} s"
    finally:
        app.dispose()
        sim.dispose()
