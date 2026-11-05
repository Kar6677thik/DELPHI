"""/health and /ready against a real database (FR-PLT-09, docs/20 §3)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as


def _client(url: str, *, role: str | None = "delphi_app") -> TestClient:
    # The api connects as delphi_app; SET ROLE at connect time exercises that role's grants.
    settings = Settings(database_url=url, version="abc123", migrations_dir=MIGRATIONS_DIR)
    engine = engine_as(url, role)
    platform = Platform(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    return TestClient(create_app(platform))


@pytest.fixture
def ready_client(migrated_url: str) -> Iterator[TestClient]:
    with _client(migrated_url) as client:
        yield client


def test_ready_when_database_is_at_head(ready_client: TestClient) -> None:
    response = ready_client.get("/api/v1/ready")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "checks": {
            "database": {"status": "ok", "detail": None},
            "migrations": {"status": "ok", "detail": None},
        },
    }


def test_not_ready_when_migrations_are_behind(create_database: Callable[[], str]) -> None:
    url = create_database()  # empty: no alembic_version table at all
    with _client(url, role=None) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["migrations"] == {
        "status": "failed",
        "detail": f"schema at no revision, expected {migration_head(MIGRATIONS_DIR)}",
    }


def test_health_reports_the_build_version(ready_client: TestClient) -> None:
    body = ready_client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["version"] == "abc123"
