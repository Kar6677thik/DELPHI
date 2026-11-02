"""The baseline migration: schema, constraints and the role grants of docs/17 §2."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from delphi.platform.db import current_revision, migration_head

from ._db import MIGRATIONS_DIR, downgrade, engine_as, upgrade

PLATFORM_TABLES = {
    "users",
    "auth_sessions",
    "idempotency_keys",
    "audit_log",
    "jobs",
    "outbox_events",
}
HASH = b"\x00" * 32
INSERT_AUDIT = (
    "INSERT INTO delphi.audit_log (at, actor_type, action, entity_type, prev_hash, hash)"
    " VALUES (now(), 'SYSTEM', 'test', 'test', :p, :h)"
)


def _audit_row(conn_engine: Engine) -> None:
    with conn_engine.begin() as conn:
        conn.execute(
            text(INSERT_AUDIT),
            {"p": HASH, "h": uuid.uuid4().bytes * 2},
        )


def test_head_creates_platform_tables_and_schemas(owner_engine: Engine) -> None:
    insp = inspect(owner_engine)
    assert set(insp.get_schema_names()) >= {"delphi", "bank", "sim_truth"}
    assert set(insp.get_table_names(schema="delphi")) >= PLATFORM_TABLES
    with owner_engine.connect() as conn:
        assert current_revision(conn) == migration_head(MIGRATIONS_DIR)
        roles = set(
            conn.scalars(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'delphi_%'"))
        )
    assert roles >= {"delphi_app", "delphi_sim"}


def test_enum_checks_reject_unknown_values(owner_engine: Engine) -> None:
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.jobs (id, type, status, idempotency_key) "
                "VALUES (:id, 'X', 'PAUSED', 'k1')"
            ),
            {"id": uuid.uuid4()},
        )
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delphi.users (id, username, display_name, password_hash, role) "
                "VALUES (:id, 'u', 'U', 'h', 'ROOT')"
            ),
            {"id": uuid.uuid4()},
        )


def test_app_role_cannot_read_ground_truth(migrated_url: str, owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS sim_truth.probe (x int)"))
    app, sim = engine_as(migrated_url, "delphi_app"), engine_as(migrated_url, "delphi_sim")
    try:
        with pytest.raises(ProgrammingError, match="permission denied"), app.connect() as conn:
            conn.execute(text("SELECT * FROM sim_truth.probe"))
        with sim.connect() as conn:
            conn.execute(text("SELECT * FROM sim_truth.probe"))  # the sim role may
    finally:
        app.dispose()
        sim.dispose()
        with owner_engine.begin() as conn:
            conn.execute(text("DROP TABLE sim_truth.probe"))


@pytest.mark.parametrize("role", ["delphi_app", "delphi_sim"])
def test_audit_log_is_append_only_for_application_roles(migrated_url: str, role: str) -> None:
    engine = engine_as(migrated_url, role)
    try:
        _audit_row(engine)  # INSERT is allowed
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM delphi.audit_log")) >= 1
        for statement in (
            "UPDATE delphi.audit_log SET action = 'x'",
            "DELETE FROM delphi.audit_log",
            "TRUNCATE delphi.audit_log",
        ):
            with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as conn:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def test_app_role_has_dml_on_product_tables(migrated_url: str) -> None:
    engine = engine_as(migrated_url, "delphi_app")
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO delphi.outbox_events (type, payload) VALUES ('t', '{}')")
            )
            conn.execute(text("DELETE FROM delphi.outbox_events WHERE type = 't'"))
            # No DDL rights: the application cannot change the schema.
        with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as conn:
            conn.execute(text("CREATE TABLE delphi.rogue (x int)"))
    finally:
        engine.dispose()


def test_downgrade_to_base_and_back(create_database: Callable[[], str]) -> None:
    url = create_database()
    upgrade(url)
    downgrade(url, "base")
    engine = engine_as(url)
    try:
        assert "delphi" not in inspect(engine).get_schema_names()
        with engine.connect() as conn:
            assert current_revision(conn) is None
    finally:
        engine.dispose()
    upgrade(url)  # the round trip leaves a usable database


def test_hash_columns_must_be_sha256_sized(owner_engine: Engine) -> None:
    with pytest.raises(DBAPIError), owner_engine.begin() as conn:
        conn.execute(
            text(INSERT_AUDIT),
            {"p": HASH, "h": b"short"},
        )
