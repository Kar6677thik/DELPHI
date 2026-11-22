"""`delphi verify-restore`, the restore drill's check (docs/23 §8, #128): a restored database is
usable if its migrations are at head, exactly one KB version is ACTIVE, the audit chain verifies
and it has users."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from delphi.cli import main, verify_restore_report
from delphi.kb.loader import load_and_validate
from delphi.kb.store import ensure_kb_v1
from delphi.platform.accounts import Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.settings import Settings
from delphi.probes.registry import CLASSIFIER_NAMES
from tests.conftest import KB_DIR

from ._db import MIGRATIONS_DIR, engine_as, upgrade

T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def _restored(create_database: Callable[[], str], *, users: bool = True) -> str:
    """A database as a good restore leaves it: migrated, KB v1 ACTIVE, audited, with users."""
    url = create_database()
    upgrade(url)
    engine = engine_as(url)
    clock = FixedClock(T0)
    with make_session_factory(engine).begin() as s:
        ensure_kb_v1(s, clock, load_and_validate(KB_DIR, CLASSIFIER_NAMES))
        if users:
            create_user(
                s,
                clock,
                username="admin",
                display_name="Admin",
                password="correct horse battery",
                role=Role.ADMIN,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )
    engine.dispose()
    return url


def _settings(url: str) -> Settings:
    return Settings(database_url=url, migrations_dir=MIGRATIONS_DIR)


def test_a_good_restore_passes_and_counts_its_rows(create_database: Callable[[], str]) -> None:
    report = verify_restore_report(_settings(_restored(create_database)))
    assert report["ok"] is True
    assert report["checks"] == {
        "migrations_at_head": True,
        "one_active_kb": True,
        "audit_chain_verifies": True,
        "has_users": True,
    }
    counts = report["row_counts"]
    assert isinstance(counts, dict)
    assert counts["delphi.users"] == 1 and counts["delphi.kb_versions"] == 1
    assert counts["delphi.audit_log"] >= 1 and "bank.payments" in counts
    assert report["revision"] == report["head"]


def test_a_tampered_audit_entry_fails_the_restore(create_database: Callable[[], str]) -> None:
    url = _restored(create_database)
    engine = engine_as(url)  # the owner role: the app role cannot change audit_log
    with engine.begin() as conn:
        conn.execute(text("UPDATE delphi.audit_log SET action = 'edited' WHERE id = 1"))
    engine.dispose()
    report = verify_restore_report(_settings(url))
    assert report["ok"] is False and report["audit_first_bad_id"] == 1
    assert report["checks"]["audit_chain_verifies"] is False  # type: ignore[index]


def test_an_empty_restore_fails(create_database: Callable[[], str]) -> None:
    report = verify_restore_report(_settings(_restored(create_database, users=False)))
    assert report["ok"] is False
    assert report["checks"]["has_users"] is False  # type: ignore[index]


def test_the_command_prints_the_report_and_exits_by_it(
    create_database: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DELPHI_DATABASE_URL", _restored(create_database))
    monkeypatch.setenv("DELPHI_MIGRATIONS_DIR", str(MIGRATIONS_DIR))
    assert main(["verify-restore"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    [report] = [line for line in lines if line.get("event") == "verify_restore.report"]
    assert report["ok"] is True
