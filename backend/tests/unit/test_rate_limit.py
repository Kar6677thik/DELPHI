"""The global API rate limit (docs/16 §1, #74): token buckets on a FixedClock, then the
middleware."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from delphi.api.app import create_app
from delphi.api.rate_limit import EXEMPT
from delphi.api.security import SESSION_COOKIE
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.rate_limit import TokenBuckets
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform

T0 = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
PING = "/api/v1/__test__/ping"


def test_a_burst_then_the_refill_rate() -> None:
    clock = FixedClock(T0)
    buckets = TokenBuckets(clock, rate=20, burst=40)
    assert all(buckets.take("a") is None for _ in range(40))
    wait = buckets.take("a")
    assert wait == pytest.approx(0.05)  # one token per 1/20 s
    clock.advance(timedelta(milliseconds=50))
    assert buckets.take("a") is None
    assert buckets.take("a") is not None
    clock.advance(timedelta(seconds=10))  # refills to the burst, not beyond
    assert all(buckets.take("a") is None for _ in range(40))
    assert buckets.take("a") is not None


def test_callers_have_their_own_buckets() -> None:
    buckets = TokenBuckets(FixedClock(T0), rate=1, burst=2)
    assert buckets.take("a") is None and buckets.take("a") is None
    assert buckets.take("a") is not None
    assert buckets.take("b") is None


def test_full_buckets_are_swept_from_memory() -> None:
    clock = FixedClock(T0)
    buckets = TokenBuckets(clock, rate=100, burst=5)
    for i in range(1023):
        buckets.take(f"caller-{i}")
    assert len(buckets) == 1023
    clock.advance(timedelta(seconds=1))  # every bucket is full again
    buckets.take("one-more")  # the 1024th check sweeps
    assert len(buckets) == 1


def test_bad_parameters_are_refused() -> None:
    with pytest.raises(ValueError):
        TokenBuckets(FixedClock(T0), rate=0, burst=1)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """An app with a 3-request burst refilling at 1/s; the database is never touched."""
    settings = Settings(
        database_url="postgresql://nobody:x@127.0.0.1:1/none",
        migrations_dir=MIGRATIONS_DIR,
        static_dir=tmp_path / "no-frontend-build",
        rate_limit_per_s=1,
        rate_limit_burst=3,
        db_connect_timeout_s=1,
    )
    engine = create_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    clock = FixedClock(T0)
    platform = Platform(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=T0,
        migration_head=None,
    )
    app = create_app(platform)

    @app.get(PING)
    def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def test_over_the_burst_is_429_with_retry_after(client: TestClient) -> None:
    assert [client.get(PING).status_code for _ in range(3)] == [200, 200, 200]
    response = client.get(PING)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json()["code"] == "RATE_LIMITED"
    assert response.headers["x-request-id"]  # the request id still wraps it
    assert response.headers["x-content-type-options"] == "nosniff"  # and the security headers


def test_sessions_are_limited_separately_from_addresses(client: TestClient) -> None:
    for _ in range(3):
        client.get(PING)
    assert client.get(PING).status_code == 429  # this address is out of tokens
    client.cookies.set(SESSION_COOKIE, "session-a")
    assert client.get(PING).status_code == 200  # a signed-in caller has its own bucket
    client.cookies.set(SESSION_COOKIE, "session-b")
    assert client.get(PING).status_code == 200


def test_the_probes_and_non_api_paths_are_exempt(client: TestClient) -> None:
    for _ in range(3):
        client.get(PING)
    assert client.get(PING).status_code == 429
    assert all(client.get("/api/v1/health").status_code == 200 for _ in range(10))
    assert client.get("/not-api").status_code != 429
    # /ready would wait on the unreachable database here; it is exempt the same way.
    assert {"/api/v1/health", "/api/v1/ready"} == EXEMPT
