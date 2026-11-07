"""User administration (FR-PLT-10), optimistic locking (FR-PLT-06) and audit verify."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from delphi.api.app import create_app
from delphi.platform.accounts import Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.tables import audit_log
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as, upgrade

PASSWORD = "correct horse battery"
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "https://testserver"}
USERS = "/api/v1/admin/users"


def _client(app: object, username: str, password: str = PASSWORD) -> TestClient:
    client = TestClient(app, base_url="https://testserver", headers=BROWSER)  # type: ignore[arg-type]
    status = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).status_code
    assert status == 204, status
    return client


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[tuple[object, Platform, TestClient]]:
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    clock = FixedClock(datetime(2026, 11, 7, tzinfo=UTC))
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=clock.now(),
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    with platform.sessions.begin() as s:
        create_user(
            s,
            clock,
            username="root",
            display_name="Root",
            password=PASSWORD,
            role=Role.ADMIN,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
    app = create_app(platform)
    yield app, platform, _client(app, "root")
    engine.dispose()


def _create(admin: TestClient, username: str = "asha", role: str = "INVESTIGATOR") -> dict:
    response = admin.post(
        USERS,
        json={"username": username, "display_name": "Asha", "password": PASSWORD, "role": role},
    )
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == '"1"'
    return dict(response.json())


def test_create_list_and_reject_bad_input(env: tuple[object, Platform, TestClient]) -> None:
    _, _, admin = env
    created = _create(admin, "Asha")
    assert created["username"] == "asha" and created["version"] == 1
    assert "password_hash" not in created
    assert [u["username"] for u in admin.get(USERS).json()] == ["asha", "root"]
    taken = admin.post(
        USERS,
        json={"username": "ASHA", "display_name": "x", "password": PASSWORD, "role": "VIEWER"},
    )
    assert taken.status_code == 422 and "taken" in taken.json()["detail"]
    short = admin.post(
        USERS,
        json={"username": "bob", "display_name": "Bob", "password": "short", "role": "VIEWER"},
    )
    assert short.status_code == 422


def test_patch_needs_a_current_if_match(env: tuple[object, Platform, TestClient]) -> None:
    """FR-PLT-06: a stale If-Match is 412; a missing one is refused."""
    _, _, admin = env
    user = _create(admin)
    url = f"{USERS}/{user['id']}"
    assert admin.patch(url, json={"display_name": "A"}).status_code == 422  # no If-Match
    renamed = admin.patch(url, json={"display_name": "Asha R"}, headers={"If-Match": '"1"'})
    assert renamed.status_code == 200 and renamed.headers["etag"] == '"2"'
    stale = admin.patch(url, json={"display_name": "Old"}, headers={"If-Match": '"1"'})
    assert stale.status_code == 412 and stale.json()["code"] == "VERSION_CONFLICT"
    missing = admin.patch(
        f"{USERS}/00000000-0000-7000-8000-000000000000",
        json={"role": "VIEWER"},
        headers={"If-Match": '"1"'},
    )
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "change", [{"disabled": True}, {"role": "VIEWER"}, {"password": "another long secret"}]
)
def test_security_changes_end_the_users_sessions(
    env: tuple[object, Platform, TestClient], change: dict[str, object]
) -> None:
    app, platform, admin = env
    user = _create(admin)
    asha = _client(app, "asha")
    assert asha.get("/api/v1/auth/me").status_code == 200
    response = admin.patch(f"{USERS}/{user['id']}", json=change, headers={"If-Match": '"1"'})
    assert response.status_code == 200
    assert asha.get("/api/v1/auth/me").status_code == 401  # signed out at once
    with platform.sessions() as s:
        actions = s.execute(select(audit_log.c.action).order_by(audit_log.c.id)).scalars().all()
    assert actions[-1] in {"user.disabled", "user.role_changed", "user.password_reset"}


def test_a_reset_password_works_and_the_old_one_does_not(
    env: tuple[object, Platform, TestClient],
) -> None:
    app, _, admin = env
    user = _create(admin)
    admin.patch(
        f"{USERS}/{user['id']}",
        json={"password": "a brand new password"},
        headers={"If-Match": '"1"'},
    )
    fresh = TestClient(app, base_url="https://testserver", headers=BROWSER)  # type: ignore[arg-type]
    old = fresh.post("/api/v1/auth/login", json={"username": "asha", "password": PASSWORD})
    assert old.status_code == 401
    _client(app, "asha", "a brand new password")


def test_the_last_enabled_admin_cannot_be_removed(env: tuple[object, Platform, TestClient]) -> None:
    _, _, admin = env
    root = next(u for u in admin.get(USERS).json() if u["username"] == "root")
    url = f"{USERS}/{root['id']}"
    for change in ({"disabled": True}, {"role": "FDE_ENGINEER"}):
        refused = admin.patch(url, json=change, headers={"If-Match": f'"{root["version"]}"'})
        assert refused.status_code == 409 and refused.json()["code"] == "INVALID_STATE_TRANSITION"
    _create(admin, "second", "ADMIN")  # with a second ADMIN, demoting root is allowed
    assert (
        admin.patch(
            url, json={"role": "FDE_ENGINEER"}, headers={"If-Match": f'"{root["version"]}"'}
        ).status_code
        == 200
    )


def test_audit_verify_endpoint(env: tuple[object, Platform, TestClient]) -> None:
    _, platform, admin = env
    _create(admin)
    ok = admin.get("/api/v1/admin/audit/verify").json()
    assert ok["ok"] is True and ok["checked"] >= 3  # root created, root login, asha created
    owner = engine_as(str(platform.engine.url.render_as_string(hide_password=False)))
    try:
        with owner.begin() as conn:  # tamper as the schema owner
            conn.execute(
                update(audit_log)
                .where(audit_log.c.action == "auth.login")
                .values(payload={"ip": "6.6.6.6"})
            )
    finally:
        owner.dispose()
    broken = admin.get("/api/v1/admin/audit/verify").json()
    assert broken["ok"] is False and broken["first_bad_id"] is not None
