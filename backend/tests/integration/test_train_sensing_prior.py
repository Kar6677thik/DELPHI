"""The seed trains the prior and the anomaly detector (docs/13 §5, §7, #88's last criteria, #159,
#90): GENERATE_DATASET writes the `test` dataset and delivers its labels, queues TRAIN_SENSING,
and TRAIN_SENSING's PRIOR and ANOMALY trainers store ACTIVE artifacts from the delivered labels,
end to end in 60 s or less. A manual retrain
cuts at the clock, so it learns from the labels delivered since (#162 review)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobQueue, JobRegistry, JobStatus, JobType, QueueConfig, enqueue
from delphi.platform.tables import jobs
from delphi.platform.worker import run_job
from delphi.sensing.anomaly import AnomalyModel
from delphi.sensing.artifacts import (
    ArtifactKind,
    ArtifactStatus,
    StoredArtifact,
    active_artifacts,
    load_blob,
)
from delphi.sensing.features import PROBE_INPUT_FEATURES
from delphi.sensing.jobs import register_sensing_jobs
from delphi.sensing.prior import PriorModel
from delphi.sim.generator_config import history_end, load_generator_config
from delphi.sim.jobs import register_sim_jobs

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
WORKER = "worker-1"
SECONDS = 60.0  # #88: `test` trains end to end in 60 s or less
END = history_end(load_generator_config(), "test")


@dataclass
class Worker:
    app: Engine
    sessions: sessionmaker[Session]
    clock: FixedClock
    queue: JobQueue
    registry: JobRegistry

    def enqueue(self, job_type: JobType, key: str, **params: Any) -> None:
        with self.sessions.begin() as s:
            enqueue(s, self.clock, job_type, idempotency_key=key, params=params)

    def run(self, job_type: JobType) -> dict[str, Any]:
        job = self.queue.claim(WORKER, frozenset({job_type}))
        assert job is not None
        run_job(self.queue, self.registry, job, WORKER)
        with self.sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == job.id)).mappings().one()
        assert row["status"] == JobStatus.SUCCEEDED, row["error"]
        result: dict[str, Any] = row["result"]
        return result

    def prior(self) -> tuple[StoredArtifact, PriorModel]:
        with self.app.connect() as conn:
            artifact = active_artifacts(conn)[ArtifactKind.PRIOR]
            return artifact, PriorModel.from_bytes(load_blob(conn, artifact.id))

    def anomaly(self) -> tuple[StoredArtifact, AnomalyModel]:
        with self.app.connect() as conn:
            artifact = active_artifacts(conn)[ArtifactKind.ANOMALY]
            return artifact, AnomalyModel.from_bytes(load_blob(conn, artifact.id))


@pytest.fixture
def worker(create_database: Callable[[], str], kb: KnowledgeBase) -> Iterator[Worker]:
    url = create_database()
    upgrade(url)
    app, sim = engine_as(url, "delphi_app"), engine_as(url, "delphi_sim")
    clock = FixedClock(T0)
    sessions = make_session_factory(app)
    registry = JobRegistry()
    register_sim_jobs(registry, sim_engine=sim, clock=clock, kb=kb)
    register_sensing_jobs(registry, engine=app, sessions=sessions, clock=clock)
    yield Worker(app, sessions, clock, JobQueue(sessions, clock, QueueConfig()), registry)
    app.dispose()
    sim.dispose()


def _seed_and_train(worker: Worker) -> tuple[dict[str, Any], dict[str, Any], float]:
    started = time.perf_counter()
    worker.enqueue(JobType.GENERATE_DATASET, "seed", profile="test")
    seeded = worker.run(JobType.GENERATE_DATASET)
    trained = worker.run(JobType.TRAIN_SENSING)  # queued by the seed
    return seeded, trained, time.perf_counter() - started


def test_the_seed_trains_an_active_prior_from_delivered_labels(worker: Worker) -> None:
    seeded, trained, elapsed = _seed_and_train(worker)
    assert seeded["rows"]["labels"] > 0
    assert trained["dataset_id"] == seeded["dataset_id"]
    assert [a["kind"] for a in trained["artifacts"]] == ["PRIOR", "ANOMALY"]
    assert set(trained["skipped"]) == {"MULE"}  # #91 registers its trainer
    assert trained["cutoff"] == END.isoformat()  # the seed trains on what its history knew
    prior, model = worker.prior()
    assert prior.status is ArtifactStatus.ACTIVE
    assert prior.dataset_id == UUID(seeded["dataset_id"])
    assert prior.training_cutoff == END
    assert model.report["profile"] == "test" and model.report["n_train"] > 0
    assert not model.report["gate"]["enforced"]  # `test` reports its gate only (docs/13 §4)
    assert elapsed <= SECONDS, f"{elapsed:.1f} s"


def test_a_manual_retrain_learns_from_the_labels_delivered_since(worker: Worker) -> None:
    seeded, _, _ = _seed_and_train(worker)
    _, first = worker.prior()
    worker.enqueue(JobType.LABEL_DELIVERY, "tick")
    assert worker.run(JobType.LABEL_DELIVERY)["delivered"] > 0  # T0 is past every due date
    # POST /admin/sensing/train's job: the same params as the seed's, but no cutoff.
    worker.enqueue(
        JobType.TRAIN_SENSING, "TRAIN_SENSING:api:retrain", dataset_id=seeded["dataset_id"]
    )
    retrained = worker.run(JobType.TRAIN_SENSING)
    assert retrained["cutoff"] == T0.isoformat()
    prior, model = worker.prior()
    assert prior.training_cutoff == T0 and prior.version == 2
    assert model.report["n_train"] >= first.report["n_train"]


def test_the_seed_trains_an_active_anomaly_detector_on_the_priors_features(
    worker: Worker,
) -> None:
    """docs/13 §5 (#134 review): the forest uses the prior's feature list, so `q` never sees a
    probe input, and trains on the GENUINE rows of the prior's training split."""
    seeded, _, _ = _seed_and_train(worker)
    prior, prior_model = worker.prior()
    anomaly, model = worker.anomaly()
    assert anomaly.status is ArtifactStatus.ACTIVE and anomaly.gate_passed
    assert anomaly.dataset_id == UUID(seeded["dataset_id"])
    assert anomaly.training_cutoff == prior.training_cutoff == END
    assert tuple(anomaly.feature_list) == tuple(prior.feature_list) == model.feature_list
    assert not set(anomaly.feature_list) & PROBE_INPUT_FEATURES
    # GENUINE rows of the training split only: fewer than the prior's whole split
    assert 0 < model.n_train < prior_model.report["n_train"]
    assert anomaly.metrics["n_train"] == model.n_train
    assert model.percentile_of_score(float(model.quantiles[0])) > 0.0
    assert model.percentile_of_score(float(model.quantiles[-1]) + 1.0) == 1.0
