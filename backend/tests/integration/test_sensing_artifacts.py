"""Sensing artifacts on a real database (docs/13 §4, §7, FR-SNS-06, #88): the gate rules, the
`TRAIN_SENSING` job with stand-in trainers, and `POST /admin/sensing/train`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import IntegrityError

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import Job, JobContext, JobQueue, JobType, enqueue
from delphi.platform.settings import Settings
from delphi.platform.tables import jobs, outbox_events, users
from delphi.platform.wiring import Platform
from delphi.sensing.artifacts import (
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactStatus,
    TrainedModel,
    active_artifacts,
    load_blob,
    model_artifacts,
    store_artifact,
)
from delphi.sensing.jobs import NoDatasetError, TrainingRequest, TrainSensingJob
from delphi.sim.catalog import READY, datasets

from ._db import MIGRATIONS_DIR, engine_as, upgrade

T0 = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
CUTOFF = T0 - timedelta(days=60)
ADMIN = Principal(UUID(int=10), frozenset(Permission))
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "http://testserver"}


def _model(kind: ArtifactKind, *, passed: bool, blob: bytes = b"model") -> TrainedModel:
    return TrainedModel(
        kind=kind,
        blob=blob,
        metrics={"auc": 0.9 if passed else 0.5},
        feature_list=("a", "b"),
        training_cutoff=CUTOFF,
        gate_passed=passed,
        gate_reasons=() if passed else ("AUC 0.5 < 0.85",),
    )


@dataclass
class Env:
    platform: Platform
    clock: FixedClock
    client: TestClient

    def store(self, model: TrainedModel) -> ArtifactStatus:
        with self.platform.sessions.begin() as s:
            return store_artifact(s, self.clock, model, dataset_id=None, job_id=None).status

    def statuses(self, kind: ArtifactKind) -> list[tuple[int, str, bool]]:
        a = model_artifacts.c
        with self.platform.sessions() as s:
            rows = s.execute(
                select(a.version, a.status, a.gate_passed)
                .where(a.kind == kind.value)
                .order_by(a.version)
            )
            return [(r.version, r.status, r.gate_passed) for r in rows]

    def add_dataset(self) -> UUID:
        dataset_id = uuid4()
        with self.platform.sessions.begin() as s:
            job = enqueue(s, self.clock, JobType.GENERATE_DATASET, idempotency_key="seed")
            s.execute(
                datasets.insert().values(
                    id=dataset_id,
                    job_id=job.job_id,
                    profile="test",
                    seed=1,
                    generator_version=1,
                    status=READY,
                    stats={},
                    created_at=T0,
                )
            )
        return dataset_id

    def train_job(self) -> JobContext:
        with self.platform.sessions.begin() as s:
            queued = enqueue(s, self.clock, JobType.TRAIN_SENSING, idempotency_key=str(uuid4()))
        with self.platform.sessions() as s:
            row = s.execute(select(jobs).where(jobs.c.id == queued.job_id)).one()
        queue = JobQueue(self.platform.sessions, self.clock)
        return JobContext(job=Job.from_row(row), queue=queue, worker_id="w1")


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[Env]:
    url = create_database()
    upgrade(url)
    engine: Engine = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    with platform.sessions.begin() as s:
        s.execute(
            users.insert().values(
                id=ADMIN.user_id,
                username="admin",
                display_name="Admin",
                password_hash="unused",
                role="ADMIN",
                disabled=False,
                created_at=T0,
                version=1,
            )
        )
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: ADMIN
    yield Env(platform, clock, TestClient(app, headers=BROWSER))
    engine.dispose()


# ------------------------------------------------------------------------------ gate rules


def test_the_first_artifact_activates_even_below_its_gate(env: Env) -> None:
    assert env.store(_model(ArtifactKind.PRIOR, passed=False)) is ArtifactStatus.ACTIVE
    assert env.statuses(ArtifactKind.PRIOR) == [(1, "ACTIVE", False)]
    with env.platform.sessions() as s:
        events = s.execute(select(outbox_events.c.type, outbox_events.c.payload)).all()
    assert [e.type for e in events] == ["artifact.activated"]
    assert events[0].payload["gate_passed"] is False


def test_a_failed_gate_keeps_the_active_artifact(env: Env) -> None:
    env.store(_model(ArtifactKind.MULE, passed=True))
    assert env.store(_model(ArtifactKind.MULE, passed=False)) is ArtifactStatus.FAILED_GATE
    assert env.statuses(ArtifactKind.MULE) == [(1, "ACTIVE", True), (2, "FAILED_GATE", False)]
    with env.platform.sessions() as s:
        assert active_artifacts(s)[ArtifactKind.MULE].version == 1
        activated = s.execute(
            select(func.count()).where(outbox_events.c.type == "artifact.activated")
        ).scalar_one()
    assert activated == 1  # the failed one announced nothing


def test_a_passing_gate_retires_the_previous_artifact(env: Env) -> None:
    env.store(_model(ArtifactKind.ANOMALY, passed=False))
    env.store(_model(ArtifactKind.ANOMALY, passed=True))
    assert env.statuses(ArtifactKind.ANOMALY) == [(1, "RETIRED", False), (2, "ACTIVE", True)]
    env.store(_model(ArtifactKind.PRIOR, passed=True))  # kinds are independent
    with env.platform.sessions() as s:
        active = active_artifacts(s)
    assert {k: a.version for k, a in active.items()} == {
        ArtifactKind.ANOMALY: 2,
        ArtifactKind.PRIOR: 1,
    }


def test_the_database_allows_one_active_artifact_per_kind(env: Env) -> None:
    env.store(_model(ArtifactKind.PRIOR, passed=True))
    env.store(_model(ArtifactKind.PRIOR, passed=True))
    with pytest.raises(IntegrityError), env.platform.sessions.begin() as s:
        s.execute(update(model_artifacts).values(status="ACTIVE"))


def test_an_edited_blob_is_refused(env: Env) -> None:
    env.store(_model(ArtifactKind.PRIOR, passed=True, blob=b"original"))
    with env.platform.sessions() as s:
        artifact = active_artifacts(s)[ArtifactKind.PRIOR]
        assert load_blob(s, artifact.id) == b"original"
    with env.platform.sessions.begin() as s:
        s.execute(text("UPDATE delphi.model_artifacts SET blob = 'tampered'::bytea"))
    with env.platform.sessions() as s, pytest.raises(ArtifactIntegrityError):
        load_blob(s, artifact.id)


# ------------------------------------------------------------------------- TRAIN_SENSING


def _fixed(
    kind: ArtifactKind, calls: list[ArtifactKind]
) -> Callable[[TrainingRequest], TrainedModel]:
    def train(request: TrainingRequest) -> TrainedModel:
        calls.append(kind)
        assert request.dataset.profile == "test"
        return _model(kind, passed=True)

    return train


def test_the_job_trains_the_registered_kinds_and_skips_the_rest(env: Env) -> None:
    dataset_id = env.add_dataset()
    calls: list[ArtifactKind] = []
    job = TrainSensingJob(
        env.platform.engine,
        env.platform.sessions,
        env.clock,
        {k: _fixed(k, calls) for k in (ArtifactKind.PRIOR, ArtifactKind.ANOMALY)},
    )
    ctx = env.train_job()
    result = job(ctx)
    assert result is not None
    assert result["dataset_id"] == str(dataset_id)
    assert [a["kind"] for a in result["artifacts"]] == ["PRIOR", "ANOMALY"]
    assert result["skipped"] == ["MULE"]
    again = job(ctx)  # a retried attempt reuses what it stored and trains nothing
    assert again == result and calls == [ArtifactKind.PRIOR, ArtifactKind.ANOMALY]
    with env.platform.sessions() as s:
        stored = s.execute(select(model_artifacts.c.dataset_id, model_artifacts.c.job_id)).all()
    assert set(stored) == {(dataset_id, ctx.job.id)}


def test_the_job_needs_a_dataset(env: Env) -> None:
    job = TrainSensingJob(env.platform.engine, env.platform.sessions, env.clock, {})
    with pytest.raises(NoDatasetError):
        job(env.train_job())


# --------------------------------------------------------------- POST /admin/sensing/train

URL = "/api/v1/admin/sensing/train"


def test_retraining_needs_a_dataset(env: Env) -> None:
    response = env.client.post(URL, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 409 and response.json()["code"] == "INVALID_STATE_TRANSITION"


def test_an_admin_queues_one_training_job_at_a_time(env: Env) -> None:
    dataset_id = env.add_dataset()
    key = str(uuid4())
    first = env.client.post(URL, headers={"Idempotency-Key": key})
    assert first.status_code == 202, first.text
    assert first.json()["type"] == "TRAIN_SENSING" and first.json()["status"] == "QUEUED"
    replay = env.client.post(URL, headers={"Idempotency-Key": key})
    assert replay.json()["id"] == first.json()["id"]
    busy = env.client.post(URL, headers={"Idempotency-Key": str(uuid4())})
    assert busy.status_code == 409 and busy.json()["code"] == "JOB_ALREADY_RUNNING"
    with env.platform.sessions() as s:
        params = s.execute(
            select(jobs.c.params).where(jobs.c.type == JobType.TRAIN_SENSING.value)
        ).scalar_one()
    assert params == {"dataset_id": str(dataset_id)}
