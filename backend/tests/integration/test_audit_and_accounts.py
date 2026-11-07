"""The audit hash chain (FR-PLT-04, docs/17 §4) and accounts (FR-PLT-03, docs/19 §2–3)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.accounts import (
    PERMISSIONS,
    BootstrapError,
    Permission,
    Role,
    authenticate,
    create_user,
    ensure_bootstrap_admin,
    hash_password,
)
from delphi.platform.audit import ActorType, AuditChainBroken, AuditVerifyJob, record, verify
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory
from delphi.platform.errors import DelphiError
from delphi.platform.tables import audit_log, users

from ._db import engine_as, upgrade

T0 = datetime(2026, 11, 7, 6, 0, 0, 123456, tzinfo=UTC)
PASSWORD = "correct horse battery"


@pytest.fixture
def db(create_database: Callable[[], str]) -> Iterator[tuple[Engine, Engine]]:
    """(app-role engine, owner engine): the owner can tamper with the log, the app cannot."""
    url = create_database()
    upgrade(url)
    app, owner = engine_as(url, "delphi_app"), engine_as(url)
    yield app, owner
    app.dispose()
    owner.dispose()


@pytest.fixture
def sessions(db: tuple[Engine, Engine]) -> sessionmaker[Session]:
    return make_session_factory(db[0])


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


def _audit(sessions: sessionmaker[Session], clock: FixedClock, n: int) -> list[int]:
    ids = []
    for i in range(n):
        clock.advance(timedelta(seconds=1))
        with sessions.begin() as s:
            ids.append(
                record(
                    s,
                    clock,
                    actor_type=ActorType.USER,
                    actor_id="u1",
                    action="case.disposed",
                    entity_type="case",
                    entity_id=f"c{i}",
                    payload={"decision": "RELEASE", "n": i},
                )
            )
    return ids


# ----------------------------------------------------------------------------- audit chain


def test_the_chain_verifies_and_links_from_genesis(
    sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    _audit(sessions, clock, 3)
    with sessions() as s:
        result = verify(s)
        first = s.execute(
            select(audit_log.c.prev_hash).order_by(audit_log.c.id).limit(1)
        ).scalar_one()
    assert result.ok and result.checked == 3
    assert first == bytes(32)


def test_an_entry_exists_only_if_its_transaction_commits(
    sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    """FR-PLT-04: a rolled-back change leaves no audit row behind."""
    with pytest.raises(RuntimeError), sessions.begin() as s:
        record(
            s,
            clock,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="x",
            entity_type="t",
            entity_id=None,
        )
        raise RuntimeError("the change failed")
    with sessions() as s:
        assert s.execute(select(audit_log.c.id)).first() is None


def test_concurrent_writers_keep_one_unbroken_chain(
    sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    def writer(i: int) -> None:
        for j in range(10):
            with sessions.begin() as s:
                record(
                    s,
                    clock,
                    actor_type=ActorType.JOB,
                    actor_id=str(i),
                    action="tick",
                    entity_type="job",
                    entity_id=f"{i}-{j}",
                )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with sessions() as s:
        result = verify(s)
    assert result.ok and result.checked == 40


@pytest.mark.parametrize("tamper", ["edit", "delete"])
def test_tampering_is_detected_at_the_first_bad_entry(
    db: tuple[Engine, Engine], sessions: sessionmaker[Session], clock: FixedClock, tamper: str
) -> None:
    ids = _audit(sessions, clock, 5)
    with db[1].begin() as conn:  # only the schema owner can do this
        if tamper == "edit":
            conn.execute(
                update(audit_log)
                .where(audit_log.c.id == ids[2])
                .values(payload={"decision": "CONFIRM_FRAUD", "n": 2})
            )
        else:
            conn.execute(delete(audit_log).where(audit_log.c.id == ids[2]))
    with sessions() as s:
        result = verify(s)
    assert not result.ok
    assert result.first_bad_id == (ids[2] if tamper == "edit" else ids[3])
    assert result.checked == 2


def test_a_window_is_checked_against_the_entry_before_it(
    db: tuple[Engine, Engine], sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    ids = _audit(sessions, clock, 5)
    with sessions() as s:
        window = verify(s, since=T0 + timedelta(seconds=4))
    assert window.ok and window.checked == 2
    with db[1].begin() as conn:  # deleting an entry just before the window breaks the link
        conn.execute(delete(audit_log).where(audit_log.c.id == ids[2]))
    with sessions() as s:
        assert verify(s, since=T0 + timedelta(seconds=4)).first_bad_id == ids[3]


def test_the_daily_job_fails_loudly_on_a_broken_chain(
    db: tuple[Engine, Engine], sessions: sessionmaker[Session], clock: FixedClock
) -> None:
    ids = _audit(sessions, clock, 3)
    job = AuditVerifyJob(sessions, clock)
    assert job(None) == {"ok": True, "checked": 3}
    with db[1].begin() as conn:
        conn.execute(update(audit_log).where(audit_log.c.id == ids[1]).values(action="forged"))
    with pytest.raises(AuditChainBroken, match=str(ids[1])):
        job(None)


# ----------------------------------------------------------------------------- accounts


def test_the_permission_matrix_matches_docs_19() -> None:
    v, d, cw, chw, f, a = (
        Permission.VIEW,
        Permission.DEMO_DRIVE,
        Permission.CASE_WRITE,
        Permission.CHASE_WRITE,
        Permission.FDE,
        Permission.ADMIN,
    )
    assert PERMISSIONS[Role.VIEWER] == {v}
    assert PERMISSIONS[Role.INVESTIGATOR] == {v, d, cw, chw}
    assert PERMISSIONS[Role.FDE_ENGINEER] == {v, d, cw, chw, f}
    assert PERMISSIONS[Role.ADMIN] == {v, d, cw, chw, f, a}


def test_passwords_need_twelve_characters() -> None:
    with pytest.raises(DelphiError, match="12"):
        hash_password("short")
    assert hash_password(PASSWORD).startswith("$argon2id$")


def test_authenticate(sessions: sessionmaker[Session], clock: FixedClock) -> None:
    with sessions.begin() as s:
        user = create_user(
            s,
            clock,
            username="  Asha ",
            display_name="Asha",
            password=PASSWORD,
            role=Role.INVESTIGATOR,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
    assert user.username == "asha"
    with sessions() as s:
        assert authenticate(s, "ASHA", PASSWORD) == user  # usernames are case-insensitive
        assert authenticate(s, "asha", "wrong password!") is None
        assert authenticate(s, "nobody", PASSWORD) is None
    with sessions.begin() as s:
        s.execute(update(users).values(disabled=True))
    with sessions() as s:
        assert authenticate(s, "asha", PASSWORD) is None  # disabled accounts cannot sign in
        actions = s.execute(select(audit_log.c.action)).scalars().all()
    assert actions == ["user.created"]


def test_bootstrap_admin(sessions: sessionmaker[Session], clock: FixedClock) -> None:
    """FR-PLT-03: empty users + no configuration fails fast; otherwise the ADMIN is created
    once."""
    with (
        pytest.raises(BootstrapError, match="DELPHI_BOOTSTRAP_ADMIN_USERNAME"),
        sessions.begin() as s,
    ):
        ensure_bootstrap_admin(s, clock, username=None, password=None)
    with sessions.begin() as s:
        admin = ensure_bootstrap_admin(s, clock, username="admin", password=PASSWORD)
    assert admin is not None and admin.role is Role.ADMIN
    with sessions.begin() as s:
        assert ensure_bootstrap_admin(s, clock, username="other", password=PASSWORD) is None
        assert ensure_bootstrap_admin(s, clock, username=None, password=None) is None
        assert s.execute(select(users.c.username)).scalars().all() == ["admin"]
