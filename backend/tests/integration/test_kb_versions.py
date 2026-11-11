"""KB versions in PostgreSQL (docs/07 §1, FR-KB-02, #15): the table's invariants, `delphi migrate`
loading KB v1 once, the stored hash, and `/ready` requiring an ACTIVE KB."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError

from delphi.api.app import create_app
from delphi.cli import main
from delphi.kb.canonical import content_sha256
from delphi.kb.loader import load_and_validate
from delphi.kb.store import (
    KB_V1_SUMMARY,
    KbIntegrityError,
    KbStatus,
    active_version,
    insert_version,
    kb_versions,
)
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.tables import audit_log
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES

from ._db import MIGRATIONS_DIR, engine_as, upgrade

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
KB_V1 = load_and_validate(KB_DIR, CLASSIFIER_NAMES)


@pytest.fixture
def db(create_database: Callable[[], str]) -> Iterator[tuple[str, Engine]]:
    """A fresh database at the migration head, with no KB version yet."""
    url = create_database()
    upgrade(url)
    engine = engine_as(url)
    yield url, engine
    engine.dispose()


def _migrate(url: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("DELPHI_DATABASE_URL", url)
    return main(["migrate"])


def _rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(kb_versions)).mappings()]


def test_migrate_loads_kb_v1_as_active_once(
    db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, engine = db
    assert _migrate(url, monkeypatch) == 0
    assert _migrate(url, monkeypatch) == 0  # every deploy runs it: the second run adds nothing
    [row] = _rows(engine)
    assert row["status"] == "ACTIVE" and row["parent_id"] is None
    assert row["change_summary"] == KB_V1_SUMMARY
    assert row["created_by"] is None and row["activated_at"] is not None
    assert row["content_sha256"] == content_sha256(KB_V1)
    with engine.connect() as conn:
        audited = conn.execute(
            select(func.count()).where(audit_log.c.action == "kb.version.created")
        ).scalar_one()
    assert audited == 1


def test_the_stored_content_reproduces_the_hash(
    db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, engine = db
    _migrate(url, monkeypatch)
    with make_session_factory(engine)() as session:
        active = active_version(session)
    assert active is not None
    assert active.kb == KB_V1  # jsonb keeps every number exactly
    assert content_sha256(active.kb) == active.content_sha256


def test_a_row_edited_outside_the_app_is_refused(
    db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, engine = db
    _migrate(url, monkeypatch)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE delphi.kb_versions"
                " SET content = jsonb_set(content, '{name}'::text[], '\"tampered\"'::jsonb)"
            )
        )
    with make_session_factory(engine)() as session, pytest.raises(KbIntegrityError):
        active_version(session)


def test_at_most_one_active_and_one_shadow(
    db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, engine = db
    _migrate(url, monkeypatch)
    sessions = make_session_factory(engine)
    clock = SystemClock()
    with pytest.raises(IntegrityError), sessions.begin() as session:
        insert_version(session, clock, KB_V1, status=KbStatus.ACTIVE, change_summary="second")
    with sessions.begin() as session:
        insert_version(session, clock, KB_V1, status=KbStatus.SHADOW, change_summary="shadow")
        insert_version(session, clock, KB_V1, status=KbStatus.CANDIDATE, change_summary="c1")
        insert_version(session, clock, KB_V1, status=KbStatus.CANDIDATE, change_summary="c2")
    with pytest.raises(IntegrityError), sessions.begin() as session:
        insert_version(session, clock, KB_V1, status=KbStatus.SHADOW, change_summary="shadow 2")
    assert sorted(str(r["status"]) for r in _rows(engine)) == [
        "ACTIVE",
        "CANDIDATE",
        "CANDIDATE",
        "SHADOW",
    ]


def test_the_status_and_hash_are_checked(db: tuple[str, Engine]) -> None:
    _, engine = db
    values = {"content": "{}", "summary": "x"}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.kb_versions (status, content, content_sha256, change_summary,"
                " created_at) VALUES ('LIVE', :content, :sha, :summary, now())"
            ),
            {**values, "sha": "0" * 64},
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.kb_versions (status, content, content_sha256, change_summary,"
                " created_at) VALUES ('CANDIDATE', :content, :sha, :summary, now())"
            ),
            {**values, "sha": "not-a-hash"},
        )


def test_the_app_role_reads_and_writes_versions(
    db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = db
    _migrate(url, monkeypatch)
    app = engine_as(url, "delphi_app")
    try:
        sessions = make_session_factory(app)
        with sessions.begin() as session:
            insert_version(
                session, SystemClock(), KB_V1, status=KbStatus.CANDIDATE, change_summary="c"
            )
        with sessions() as session:
            assert active_version(session) is not None
    finally:
        app.dispose()


# ------------------------------------------------------------------------------------ /ready


def _client(url: str) -> TestClient:
    settings = Settings(database_url=url, version="abc123", migrations_dir=MIGRATIONS_DIR)
    engine = engine_as(url, "delphi_app")
    platform = Platform(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    return TestClient(create_app(platform))


def test_ready_needs_an_active_kb(db: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch) -> None:
    url, _ = db
    with _client(url) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["kb"] == {
            "status": "failed",
            "detail": "no ACTIVE KB version (run delphi migrate)",
        }
        _migrate(url, monkeypatch)
        response = client.get("/api/v1/ready")
    assert response.status_code == 200, response.text
    assert response.json()["checks"]["kb"] == {"status": "ok", "detail": None}
