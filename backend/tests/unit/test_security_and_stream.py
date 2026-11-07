"""Fail-closed permissions until #6, and the stream's guards (docs/16 §1, §3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.platform.events import TOPIC_PREFIXES, EventType, matches
from delphi.platform.settings import Settings
from delphi.platform.wiring import build_platform

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
STREAM = "/api/v1/events/stream"


@pytest.fixture
def app_and_client() -> Iterator[tuple[object, TestClient]]:
    settings = Settings(
        database_url="postgresql://x:y@127.0.0.1:1/none", migrations_dir=MIGRATIONS_DIR
    )
    platform = build_platform(settings, process="test")
    app = create_app(platform)  # no event hub
    yield app, TestClient(app)
    platform.close()


def test_protected_endpoints_are_closed_until_authentication_exists(
    app_and_client: tuple[object, TestClient],
) -> None:
    _, client = app_and_client
    response = client.get(STREAM)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


def test_a_principal_without_the_permission_is_forbidden(
    app_and_client: tuple[object, TestClient],
) -> None:
    app, client = app_and_client
    app.dependency_overrides[current_principal] = lambda: Principal(  # type: ignore[attr-defined]
        UUID(int=2), frozenset({Permission.ADMIN})
    )
    response = client.get(STREAM)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_without_a_hub_the_stream_is_not_initialised(
    app_and_client: tuple[object, TestClient],
) -> None:
    app, client = app_and_client
    app.dependency_overrides[current_principal] = lambda: Principal(  # type: ignore[attr-defined]
        UUID(int=1), frozenset({Permission.VIEW})
    )
    response = client.get(STREAM)
    assert response.status_code == 503
    assert response.json()["code"] == "NOT_INITIALISED"


def test_every_event_type_belongs_to_exactly_one_topic() -> None:
    for event_type in EventType:
        owners = [t for t in TOPIC_PREFIXES if matches(event_type.value, frozenset({t}))]
        assert len(owners) == 1, (event_type, owners)


def test_a_slow_subscriber_is_closed_instead_of_blocking_the_hub() -> None:
    import asyncio
    from datetime import UTC, datetime

    from delphi.platform.events import Event, EventHub

    async def scenario() -> None:
        hub = EventHub(sessions=None, listen_dsn="postgresql://unused", queue_size=1)  # type: ignore[arg-type]
        slow = hub.subscribe({"jobs"})
        other = hub.subscribe({"cases"})
        at = datetime(2026, 11, 7, tzinfo=UTC)
        hub.deliver([Event(i, "job.updated", "j", {}, at) for i in (1, 2)])
        assert slow.closed and hub.subscriber_count == 1  # closed on overflow
        assert not other.closed and other.queue.empty()  # topic filter: no case events

    asyncio.run(scenario())
