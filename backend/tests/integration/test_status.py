"""`GET /status` and the `NOT_INITIALISED` guard on a real database (docs/16 §2, docs/20 §3,
#88)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from delphi.api.app import create_app
from delphi.api.routers.status import Initialised
from delphi.api.security import Permission, Principal, current_principal
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform
from delphi.sensing.artifacts import ArtifactKind, TrainedModel, store_artifact
from delphi.sim.catalog import READY, datasets

from ._db import MIGRATIONS_DIR, engine_as, upgrade

T0 = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))
GUARDED = "/api/v1/__test__/guarded"


@dataclass
class Env:
    platform: Platform
    clock: FixedClock
    client: TestClient

    def status(self) -> dict[str, object]:
        response = self.client.get("/api/v1/status")
        assert response.status_code == 200, response.text
        body: dict[str, object] = response.json()
        return body

    def enqueue(self, job_type: JobType) -> UUID:
        with self.platform.sessions.begin() as s:
            return enqueue(s, self.clock, job_type, idempotency_key=str(uuid4())).job_id

    def add_dataset(self) -> None:
        job_id = self.enqueue(JobType.GENERATE_DATASET)
        with self.platform.sessions.begin() as s:
            s.execute(
                datasets.insert().values(
                    id=uuid4(),
                    job_id=job_id,
                    profile="test",
                    seed=1,
                    generator_version=1,
                    status=READY,
                    stats={},
                    created_at=T0,
                )
            )

    def add_model(self, kind: ArtifactKind, *, passed: bool = True) -> None:
        model = TrainedModel(kind, b"blob", {}, ("a",), T0, gate_passed=passed)
        with self.platform.sessions.begin() as s:
            store_artifact(s, self.clock, model, dataset_id=None, job_id=None)


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
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: VIEWER

    @app.get(GUARDED)
    def guarded(_: Initialised) -> dict[str, bool]:  # a stand-in feature endpoint
        return {"ok": True}

    # Ahead of the SPA fallback, which answers every other path.
    app.router.routes.insert(0, app.router.routes.pop())

    yield Env(platform, clock, TestClient(app))
    engine.dispose()


def test_nothing_is_ready_before_seeding(env: Env) -> None:
    assert env.status() == {
        "dataset_ready": False,
        "models_ready": False,
        "seed_job_id": None,
        "seed_progress": None,
        "models": {},
        "models_below_gate": [],
    }


def test_the_seed_job_is_followed_until_the_models_are_ready(env: Env) -> None:
    generate = env.enqueue(JobType.GENERATE_DATASET)
    body = env.status()
    assert body["seed_job_id"] == str(generate) and body["seed_progress"] == 0.0
    env.add_dataset()
    train = env.enqueue(JobType.TRAIN_SENSING)
    body = env.status()
    assert body["dataset_ready"] is True and body["seed_job_id"] == str(train)
    env.add_model(ArtifactKind.PRIOR, passed=False)
    env.add_model(ArtifactKind.ANOMALY)
    assert env.status()["models_ready"] is False  # MULE is still missing
    env.add_model(ArtifactKind.MULE)
    body = env.status()
    assert body["models_ready"] is True
    assert body["models"] == {
        "ANOMALY": {"version": 1, "gate_passed": True},
        "MULE": {"version": 1, "gate_passed": True},
        "PRIOR": {"version": 1, "gate_passed": False},
    }
    assert body["models_below_gate"] == ["PRIOR"]


def test_feature_endpoints_answer_not_initialised_until_ready(env: Env) -> None:
    response = env.client.get(GUARDED)
    assert response.status_code == 503 and response.json()["code"] == "NOT_INITIALISED"
    assert "dataset, PRIOR, ANOMALY, MULE" in response.json()["detail"]
    env.add_dataset()
    for kind in ArtifactKind:
        env.add_model(kind)
    response = env.client.get(GUARDED)
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_ready_does_not_depend_on_the_dataset_or_the_models(env: Env) -> None:
    checks = env.client.get("/api/v1/ready").json()["checks"]
    assert set(checks) == {"database", "migrations", "kb"}
