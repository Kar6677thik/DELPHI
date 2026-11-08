"""POST /admin/datasets (docs/16 §2, #19) and `delphi seed` (docs/23 §4), on a real database."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.cli import main
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import JobType, enqueue
from delphi.platform.settings import Settings
from delphi.platform.tables import jobs, users
from delphi.platform.wiring import Platform
from delphi.sim.api import PROFILES
from delphi.sim.catalog import READY, datasets
from delphi.sim.generator_config import load_generator_config

from ._db import MIGRATIONS_DIR, engine_as, upgrade

ADMIN = Principal(UUID(int=10), frozenset(Permission))
VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))
T0 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "http://testserver"}
URL = "/api/v1/admin/datasets"


@dataclass
class Env:
    url: str
    platform: Platform
    client: TestClient

    def post(self, body: object, key: str | None = None) -> object:
        return self.client.post(URL, json=body, headers={"Idempotency-Key": key or str(uuid4())})

    def jobs(self) -> list[dict[str, object]]:
        with self.platform.sessions() as s:
            rows = s.execute(select(jobs).order_by(jobs.c.created_at)).mappings().all()
        return [dict(r) for r in rows]

    def add_dataset(self) -> None:
        """A dataset as the job leaves it (the job itself is tested elsewhere)."""
        with self.platform.sessions.begin() as s:
            job = enqueue(s, self.platform.clock, JobType.GENERATE_DATASET, idempotency_key="old")
            s.execute(
                datasets.insert().values(
                    id=uuid4(),
                    job_id=job.job_id,
                    profile="test",
                    seed=1,
                    generator_version=1,
                    status=READY,
                    stats={},
                    created_at=T0,
                )
            )


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
    with platform.sessions.begin() as s:  # idempotency keys and jobs point at their user
        for n, principal in enumerate((ADMIN, VIEWER)):
            s.execute(
                users.insert().values(
                    id=principal.user_id,
                    username=f"user{n}",
                    display_name=f"User {n}",
                    password_hash="unused",
                    role="ADMIN",
                    disabled=False,
                    created_at=T0,
                    version=1,
                )
            )
    app = create_app(platform)
    app.dependency_overrides[current_principal] = lambda: ADMIN
    yield Env(url, platform, TestClient(app, headers=BROWSER))
    engine.dispose()


def test_profiles_are_the_generators() -> None:
    assert set(PROFILES) == set(load_generator_config().profiles)


def test_an_admin_queues_a_dataset_and_a_replay_returns_the_same_job(env: Env) -> None:
    key = str(uuid4())
    first = env.post({"profile": "demo"}, key)
    assert first.status_code == 202
    job = first.json()
    assert job["type"] == "GENERATE_DATASET" and job["status"] == "QUEUED"
    again = env.post({"profile": "demo"}, key)
    assert again.status_code == 202 and again.json() == job
    assert again.headers["Idempotent-Replayed"] == "true"
    [row] = env.jobs()
    assert row["params"] == {"profile": "demo"} and row["created_by"] == ADMIN.user_id


def test_a_seed_is_passed_on(env: Env) -> None:
    assert env.post({"profile": "test", "seed": 42}).status_code == 202
    assert env.jobs()[0]["params"] == {"profile": "test", "seed": 42}


def test_one_generation_at_a_time(env: Env) -> None:
    assert env.post({"profile": "demo"}).status_code == 202
    second = env.post({"profile": "test"})
    assert second.status_code == 409 and second.json()["code"] == "JOB_ALREADY_RUNNING"
    assert len(env.jobs()) == 1


def test_no_second_dataset(env: Env) -> None:
    env.add_dataset()
    response = env.post({"profile": "demo"})
    assert response.status_code == 409 and response.json()["code"] == "INVALID_STATE_TRANSITION"


@pytest.mark.parametrize(
    "body",
    [{"profile": "huge"}, {}, {"profile": "demo", "seed": -1}, {"profile": "demo", "rows": 9}],
)
def test_bad_requests(env: Env, body: object) -> None:
    response = env.post(body)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    assert env.jobs() == []


def test_the_key_is_required(env: Env) -> None:
    response = env.client.post(URL, json={"profile": "demo"})
    assert response.status_code == 422 and "Idempotency-Key" in response.json()["detail"]


def test_only_an_admin(env: Env) -> None:
    env.client.app.dependency_overrides[current_principal] = lambda: VIEWER  # type: ignore[attr-defined]
    response = env.post({"profile": "demo"})
    assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"
    assert env.jobs() == []


# ---------------------------------------------------------------------------- delphi seed


def _seed(env: Env, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setenv("DELPHI_DATABASE_URL", env.url)
    return main(["seed", *args])


def test_seed_queues_once_however_often_it_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _seed(env, monkeypatch, "--if-empty", "--profile", "test", "--personas") == 0
    assert _seed(env, monkeypatch, "--if-empty", "--profile", "test", "--personas") == 0
    [job] = env.jobs()
    assert job["type"] == "GENERATE_DATASET"
    assert job["idempotency_key"] == "GENERATE_DATASET:test:seed-1"
    assert job["params"] == {"profile": "test", "seed": 1, "personas": True}
    out = capsys.readouterr().out
    assert "queued (test, seed 1)" in out and "already queued" in out


def test_seed_if_empty_does_nothing_once_a_dataset_is_there(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.add_dataset()
    assert _seed(env, monkeypatch, "--if-empty") == 0
    assert "already holds a dataset; nothing to do" in capsys.readouterr().out
    assert _seed(env, monkeypatch) == 1  # without --if-empty it is an error
    assert "already holds a dataset" in capsys.readouterr().err
    with env.platform.sessions() as s:
        assert s.execute(select(func.count()).select_from(jobs)).scalar_one() == 1  # "old"


def test_seed_refuses_while_another_generation_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.post({"profile": "eval"}).status_code == 202
    assert _seed(env, monkeypatch, "--profile", "demo", "--seed", "5") == 1
    assert "already being generated" in capsys.readouterr().err


def test_a_profile_turned_off_is_refused_everywhere(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/23 §4: DELPHI_DATASET_PROFILES=test,demo on the home server refuses `eval` in the
    api and in `delphi seed`, and the job refuses it too."""
    from dataclasses import replace

    from delphi.sim.jobs import GenerateDatasetJob

    limited = env.platform.settings.model_copy(update={"dataset_profiles": "test,demo"})
    env.client.app.state.platform = replace(env.platform, settings=limited)  # type: ignore[attr-defined]
    refused = env.post({"profile": "eval"})
    assert refused.status_code == 422  # type: ignore[attr-defined]
    assert "turned off" in refused.json()["detail"]  # type: ignore[attr-defined]
    assert env.post({"profile": "demo"}).status_code == 202  # type: ignore[attr-defined]

    monkeypatch.setenv("DELPHI_DATASET_PROFILES", "test,demo")
    assert _seed(env, monkeypatch, "--profile", "eval") == 1
    assert "turned off" in capsys.readouterr().err

    job = GenerateDatasetJob.__new__(GenerateDatasetJob)
    object.__setattr__(job, "allowed_profiles", frozenset({"test", "demo"}))
    object.__setattr__(job, "config", load_generator_config())

    ctx = SimpleNamespace(job=SimpleNamespace(params={"profile": "eval"}, id=uuid4()))
    with pytest.raises(ValueError, match="turned off"):
        job(ctx)  # type: ignore[arg-type]
