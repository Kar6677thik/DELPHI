"""The transactional outbox and the SSE stream (FR-PLT-08, docs/04 §5.5, docs/16 §3).

The stream is tested through a real uvicorn server: an SSE response never ends, which the
in-process test client cannot handle.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from delphi.api.app import create_app
from delphi.api.security import Permission, Principal, current_principal
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.events import EventHub, EventType, publish
from delphi.platform.jobs import JobQueue, JobType, enqueue
from delphi.platform.settings import Settings, libpq_url
from delphi.platform.tables import outbox_events
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as, upgrade

VIEWER = Principal(UUID(int=1), frozenset({Permission.VIEW}))


@pytest.fixture
def db(create_database: Callable[[], str]) -> Iterator[tuple[str, Engine]]:
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    yield url, engine
    engine.dispose()


@pytest.fixture
def sessions(db: tuple[str, Engine]) -> sessionmaker[Session]:
    return make_session_factory(db[1])


def _publish(sessions: sessionmaker[Session], event_type: EventType, entity: str) -> int:
    with sessions.begin() as s:
        return publish(s, event_type, entity, {"n": entity})


def test_publish_is_part_of_the_callers_transaction(sessions: sessionmaker[Session]) -> None:
    first = _publish(sessions, EventType.CASE_OPENED, "a")
    with pytest.raises(RuntimeError), sessions.begin() as s:
        publish(s, EventType.CASE_OPENED, "rolled-back")
        raise RuntimeError("abort")
    second = _publish(sessions, EventType.CASE_OPENED, "b")
    with sessions() as s:
        rows = s.execute(select(outbox_events.c.entity_id).order_by(outbox_events.c.id)).all()
    assert [r.entity_id for r in rows] == ["a", "b"]
    assert second > first


def test_job_transitions_publish_job_updated(sessions: sessionmaker[Session]) -> None:
    clock = SystemClock()
    with sessions.begin() as s:
        job_id = enqueue(s, clock, JobType.DEMO_RESET, idempotency_key="k").job_id
    queue = JobQueue(sessions, clock)
    job = queue.claim("w", frozenset({JobType.DEMO_RESET}))
    assert job is not None
    queue.succeed(job, "w", None)
    with sessions() as s:
        rows = s.execute(
            select(
                outbox_events.c.type, outbox_events.c.entity_id, outbox_events.c.payload
            ).order_by(outbox_events.c.id)
        ).all()
    assert [r.type for r in rows] == ["job.updated"] * 3
    assert {r.entity_id for r in rows} == {str(job_id)}
    assert [r.payload["status"] for r in rows] == ["QUEUED", "RUNNING", "SUCCEEDED"]


# --------------------------------------------------------------------------- the live stream


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def server(db: tuple[str, Engine], sessions: sessionmaker[Session]) -> Iterator[str]:
    """The api on a real port, with the event hub running and a VIEWER signed in."""
    url, engine = db
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR),
        engine=engine,
        sessions=sessions,
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    hub = EventHub(sessions, libpq_url(url), poll_s=0.5)
    app = create_app(platform, event_hub=hub)
    app.dependency_overrides[current_principal] = lambda: VIEWER
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started
    assert hub.listening.wait(10), "the event hub never started listening"
    yield f"http://127.0.0.1:{port}/api/v1/events/stream"
    srv.should_exit = True
    thread.join(10)


def _read_events(
    url: str,
    *,
    count: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    while_open: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Open the stream, run ``while_open``, and collect ``count`` events (id, event, data)."""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    with httpx.stream("GET", url, headers=headers, params=params, timeout=10) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        if while_open is not None:
            while_open()
        for line in response.iter_lines():
            if line.startswith("id: "):
                current["id"] = int(line[4:])
            elif line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                events.append(current)
                current = {}
                if len(events) == count:
                    break
    return events


def test_live_events_reach_subscribers_filtered_by_topic(
    server: str, sessions: sessionmaker[Session]
) -> None:
    def produce() -> None:
        time.sleep(0.3)
        _publish(sessions, EventType.CASE_OPENED, "case-1")
        _publish(sessions, EventType.JOB_UPDATED, "job-1")
        _publish(sessions, EventType.JOB_UPDATED, "job-2")

    received = _read_events(server, count=2, params={"topics": "jobs"}, while_open=produce)
    assert [e["event"] for e in received] == ["job.updated", "job.updated"]
    assert [e["data"]["entity_id"] for e in received] == ["job-1", "job-2"]
    assert received[0]["data"]["at"].endswith("Z")
    assert received[0]["id"] < received[1]["id"]


def test_reconnect_with_last_event_id_replays_missed_events(
    server: str, sessions: sessionmaker[Session]
) -> None:
    """FR-PLT-08 acceptance: events published while the client was away are replayed."""
    seen = _publish(sessions, EventType.CASE_UPDATED, "seen")
    missed = [_publish(sessions, EventType.CASE_UPDATED, f"missed-{i}") for i in range(3)]
    received = _read_events(server, count=3, headers={"Last-Event-ID": str(seen)})
    assert [e["id"] for e in received] == missed
    assert [e["data"]["entity_id"] for e in received] == ["missed-0", "missed-1", "missed-2"]


def test_the_stream_says_it_is_open_at_once(server: str) -> None:
    """#73: a comment goes out first, so proxies that hold the response head until the first
    body bytes (Vite's dev proxy) do not keep the client waiting for the 15 s heartbeat."""
    started = time.monotonic()
    with httpx.stream("GET", server, timeout=5) as response:
        first = next(response.iter_lines())
    assert first == ": open"
    assert time.monotonic() - started < 2


def test_a_new_event_source_resumes_with_the_query_parameter(
    server: str, sessions: sessionmaker[Session]
) -> None:
    """#73: a client that opens a new EventSource after an HTTP error cannot set headers, so it
    passes the last id it saw as `?last_event_id=`; the header still wins when both are sent."""
    seen = _publish(sessions, EventType.JOB_UPDATED, "seen")
    missed = [_publish(sessions, EventType.JOB_UPDATED, f"missed-{i}") for i in range(2)]
    by_query = _read_events(server, count=2, params={"last_event_id": str(seen)})
    assert [e["id"] for e in by_query] == missed
    both = _read_events(
        server, count=1, headers={"Last-Event-ID": str(missed[0])}, params={"last_event_id": "0"}
    )
    assert [e["id"] for e in both] == missed[1:]


def test_the_stream_rejects_bad_requests(server: str) -> None:
    bad_topic = httpx.get(server, params={"topics": "nope"}, timeout=5)
    assert bad_topic.status_code == 422
    assert bad_topic.json()["code"] == "VALIDATION_FAILED"
    bad_id = httpx.get(server, headers={"Last-Event-ID": "abc"}, timeout=5)
    assert bad_id.status_code == 422
    bad_param = httpx.get(server, params={"last_event_id": "-1"}, timeout=5)
    assert bad_param.status_code == 422


def test_publish_order_is_commit_order_under_concurrency(sessions: sessionmaker[Session]) -> None:
    """Concurrent publishers commit in id order (the advisory lock), so a reader that has seen
    id N has already seen every id below N."""
    errors: list[BaseException] = []

    def publisher(i: int) -> None:
        try:
            for j in range(10):
                _publish(sessions, EventType.SIM_TRANSFER, f"{i}-{j}")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=publisher, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with sessions() as s:
        ids = s.execute(select(outbox_events.c.id).order_by(outbox_events.c.id)).scalars().all()
        assert s.execute(select(func.count()).select_from(outbox_events)).scalar_one() == 50
    assert ids == sorted(ids) and len(set(ids)) == 50
