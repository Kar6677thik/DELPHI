"""Idempotency-Key (FR-PLT-05) and the /jobs endpoints (docs/16 §2), on a real database."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Column, Engine, MetaData, Table, Text, func, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from delphi.api.app import create_app
from delphi.api.idempotency import IdempotencyKey, RawBody, run_idempotent
from delphi.api.security import Permission, Principal, current_principal, require
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.jobs import JobQueue, JobType, enqueue
from delphi.platform.settings import Settings
from delphi.platform.tables import jobs
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as, upgrade

ADMIN = Principal(UUID(int=10), frozenset(Permission))
VIEWER = Principal(UUID(int=11), frozenset({Permission.VIEW}))
OTHER = Principal(UUID(int=99), frozenset(Permission))
T0 = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
# Users arrive with #6; the tests only need rows to point at.
users = Table(
    "users",
    MetaData(schema="delphi"),
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("username", Text),
    Column("display_name", Text),
    Column("password_hash", Text),
    Column("role", Text),
)


def _test_router() -> APIRouter:
    """A state-creating route, as later features will write them: it enqueues a job."""
    router = APIRouter(prefix="/api/v1/test")

    @router.post("/things", status_code=201)
    def create(
        request: Request,
        body: RawBody,
        key: IdempotencyKey,
        principal: Annotated[Principal, Depends(require(Permission.ADMIN))],
    ) -> JSONResponse:
        platform: Platform = request.app.state.platform

        def make(session: Session) -> tuple[int, Any]:
            made = enqueue(
                session, platform.clock, JobType.DEMO_RESET, idempotency_key=str(uuid4())
            )
            return 201, {"job_id": made.job_id}

        return run_idempotent(request, body, principal, key, make)

    return router


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[tuple[FastAPI, Platform, FixedClock]]:
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
    # Idempotency keys belong to a user (a foreign key), so the test principals exist.
    with platform.sessions.begin() as s:
        for n, uid in enumerate((ADMIN.user_id, VIEWER.user_id, OTHER.user_id)):
            s.execute(
                users.insert().values(
                    id=uid,
                    username=f"user{n}",
                    display_name=f"User {n}",
                    password_hash="unused",
                    role="ADMIN",
                )
            )
    app = create_app(platform)
    app.include_router(_test_router())
    app.dependency_overrides[current_principal] = lambda: ADMIN
    yield app, platform, clock
    engine.dispose()


def _jobs_count(platform: Platform) -> int:
    with platform.sessions() as s:
        return s.execute(select(func.count()).select_from(jobs)).scalar_one()


# ----------------------------------------------------------------------------- idempotency


def test_same_key_and_body_replays_the_stored_response(
    env: tuple[FastAPI, Platform, FixedClock],
) -> None:
    app, platform, _ = env
    client = TestClient(app)
    key = str(uuid4())
    first = client.post(
        "/api/v1/test/things", json={"a": 1, "b": 2}, headers={"Idempotency-Key": key}
    )
    # The same object serialised differently is the same request.
    again = client.post(
        "/api/v1/test/things",
        content=b'{ "b": 2, "a": 1 }',
        headers={"Idempotency-Key": key, "Content-Type": "application/json"},
    )
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert _jobs_count(platform) == 1  # the handler ran once


def test_same_key_with_a_different_body_is_a_conflict(
    env: tuple[FastAPI, Platform, FixedClock],
) -> None:
    app, platform, _ = env
    client = TestClient(app)
    key = str(uuid4())
    client.post("/api/v1/test/things", json={"a": 1}, headers={"Idempotency-Key": key})
    conflict = client.post("/api/v1/test/things", json={"a": 2}, headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert _jobs_count(platform) == 1


def test_keys_are_per_user_and_expire(env: tuple[FastAPI, Platform, FixedClock]) -> None:
    app, platform, clock = env
    client = TestClient(app)
    key = str(uuid4())
    client.post("/api/v1/test/things", json={}, headers={"Idempotency-Key": key})
    app.dependency_overrides[current_principal] = lambda: OTHER
    assert (
        client.post("/api/v1/test/things", json={}, headers={"Idempotency-Key": key}).headers.get(
            "Idempotent-Replayed"
        )
        is None
    )
    assert _jobs_count(platform) == 2
    clock.advance(timedelta(hours=25))  # past the 24 h retention: the key is free again
    fresh = client.post("/api/v1/test/things", json={}, headers={"Idempotency-Key": key})
    assert fresh.status_code == 201 and fresh.headers.get("Idempotent-Replayed") is None
    assert _jobs_count(platform) == 3


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "not-a-uuid"}])
def test_the_key_is_required_and_must_be_a_uuid(
    env: tuple[FastAPI, Platform, FixedClock], headers: dict[str, str]
) -> None:
    app, _, _ = env
    response = TestClient(app).post("/api/v1/test/things", json={}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_concurrent_requests_with_one_key_run_once(
    env: tuple[FastAPI, Platform, FixedClock],
) -> None:
    app, platform, _ = env
    key = str(uuid4())
    statuses: list[int] = []

    def call() -> None:
        response = TestClient(app).post(
            "/api/v1/test/things", json={"x": 1}, headers={"Idempotency-Key": key}
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [201] * 5
    assert _jobs_count(platform) == 1


# ----------------------------------------------------------------------------- /jobs


def _add_jobs(platform: Platform, clock: FixedClock, n: int) -> list[UUID]:
    ids = []
    for i in range(n):
        clock.advance(timedelta(milliseconds=5))  # distinct UUIDv7 timestamps
        with platform.sessions.begin() as s:
            ids.append(enqueue(s, clock, JobType.DEMO_RESET, idempotency_key=f"j{i}").job_id)
    return ids


def test_read_one_job(env: tuple[FastAPI, Platform, FixedClock]) -> None:
    app, platform, clock = env
    (job_id,) = _add_jobs(platform, clock, 1)
    app.dependency_overrides[current_principal] = lambda: VIEWER  # `view` is enough
    client = TestClient(app)
    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "QUEUED" and body["type"] == "DEMO_RESET" and body["progress"] == 0
    missing = client.get(f"/api/v1/jobs/{uuid4()}")
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"


def test_listing_jobs_is_admin_only_newest_first_and_paged(
    env: tuple[FastAPI, Platform, FixedClock],
) -> None:
    app, platform, clock = env
    ids = _add_jobs(platform, clock, 5)
    client = TestClient(app)
    first = client.get("/api/v1/jobs", params={"limit": 2}).json()
    assert [j["id"] for j in first["items"]] == [str(i) for i in reversed(ids[-2:])]
    second = client.get("/api/v1/jobs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    third = client.get("/api/v1/jobs", params={"limit": 2, "cursor": second["next_cursor"]}).json()
    seen = [j["id"] for page in (first, second, third) for j in page["items"]]
    assert seen == [str(i) for i in reversed(ids)]
    assert third["next_cursor"] is None
    assert client.get("/api/v1/jobs", params={"status": "FAILED"}).json()["items"] == []
    assert client.get("/api/v1/jobs", params={"cursor": "%%%"}).status_code == 422

    app.dependency_overrides[current_principal] = lambda: VIEWER
    assert client.get("/api/v1/jobs").status_code == 403


def test_retry_moves_a_failed_job_back_to_queued(env: tuple[FastAPI, Platform, FixedClock]) -> None:
    app, platform, clock = env
    (job_id,) = _add_jobs(platform, clock, 1)
    queue = JobQueue(platform.sessions, clock)
    for _ in range(3):
        job = queue.claim("w", frozenset({JobType.DEMO_RESET}))
        assert job is not None
        queue.fail(job, "w", "boom")
        clock.advance(timedelta(minutes=10))
    client = TestClient(app)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "FAILED"
    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "QUEUED"
    again = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert again.status_code == 409 and again.json()["code"] == "INVALID_STATE_TRANSITION"
    assert client.post(f"/api/v1/jobs/{uuid4()}/retry").status_code == 404
