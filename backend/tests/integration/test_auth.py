"""Sign-in, sessions, lockout, CSRF and security headers (FR-PLT-01, docs/16 §1, docs/19 §2, §4)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from delphi.api.app import create_app
from delphi.platform.accounts import Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.tables import audit_log, auth_sessions, users
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as, upgrade

T0 = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
ORIGIN = "https://delphi.example.com"
BROWSER = {"X-Delphi-Csrf": "1", "Origin": ORIGIN}
PASSWORD = "correct horse battery"


@pytest.fixture
def env(create_database: Callable[[], str]) -> Iterator[tuple[TestClient, Platform, FixedClock]]:
    url = create_database()
    upgrade(url)
    engine = engine_as(url, "delphi_app")
    clock = FixedClock(T0)
    platform = Platform(
        settings=Settings(database_url=url, migrations_dir=MIGRATIONS_DIR, public_origin=ORIGIN),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=T0,
        migration_head=migration_head(MIGRATIONS_DIR),
    )
    with platform.sessions.begin() as s:
        create_user(
            s,
            clock,
            username="asha",
            display_name="Asha",
            password=PASSWORD,
            role=Role.INVESTIGATOR,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
    # https: the session cookie is Secure, so the test client only sends it over https.
    client = TestClient(create_app(platform), base_url="https://testserver", headers=BROWSER)
    yield client, platform, clock
    engine.dispose()


def _login(client: TestClient, password: str = PASSWORD, username: str = "asha") -> int:
    return client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).status_code


def test_login_me_logout(env: tuple[TestClient, Platform, FixedClock]) -> None:
    client, platform, _ = env
    assert client.get("/api/v1/auth/me").status_code == 401
    response = client.post("/api/v1/auth/login", json={"username": "ASHA", "password": PASSWORD})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    for flag in ("delphi_session=", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    me = client.get("/api/v1/auth/me").json()
    assert me["username"] == "asha" and me["role"] == "INVESTIGATOR"
    assert me["permissions"] == ["case:write", "chase:write", "demo:drive", "view"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    with platform.sessions() as s:
        assert s.execute(select(auth_sessions.c.id)).first() is None
        actions = s.execute(select(audit_log.c.action).order_by(audit_log.c.id)).scalars().all()
    assert actions == ["user.created", "auth.login", "auth.logout"]


def test_failures_are_generic_and_audited(env: tuple[TestClient, Platform, FixedClock]) -> None:
    client, platform, _ = env
    wrong = client.post(
        "/api/v1/auth/login", json={"username": "asha", "password": "nope nope nope"}
    )
    unknown = client.post("/api/v1/auth/login", json={"username": "ghost", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # no user enumeration
    with platform.sessions() as s:
        failed = (
            s.execute(select(audit_log.c.payload).where(audit_log.c.action == "auth.login_failed"))
            .scalars()
            .all()
        )
    assert [f["username"] for f in failed] == ["asha", "ghost"]


def test_lockout_after_five_failures_per_username_and_ip(
    env: tuple[TestClient, Platform, FixedClock],
) -> None:
    client, _, clock = env
    for _ in range(5):
        assert _login(client, "wrong password!") == 401
    locked = client.post("/api/v1/auth/login", json={"username": "asha", "password": PASSWORD})
    assert locked.status_code == 429 and locked.json()["code"] == "RATE_LIMITED"
    assert int(locked.headers["retry-after"]) == 15 * 60
    clock.advance(timedelta(minutes=15))
    assert _login(client) == 204  # the window has passed


def test_sessions_end_when_idle_or_too_old(env: tuple[TestClient, Platform, FixedClock]) -> None:
    client, _, clock = env
    assert _login(client) == 204
    clock.advance(timedelta(hours=11, minutes=59))
    assert client.get("/api/v1/auth/me").status_code == 200  # use refreshes last_seen_at
    clock.advance(timedelta(hours=12))
    assert client.get("/api/v1/auth/me").status_code == 401  # idle 12 h

    assert _login(client) == 204
    signed_in = clock.now()
    while client.get("/api/v1/auth/me").status_code == 200:  # active every 11 h
        clock.advance(timedelta(hours=11))
        assert clock.now() - signed_in <= timedelta(days=7, hours=11), "never expired"
    assert clock.now() - signed_in >= timedelta(days=7)


def test_login_rotates_the_session_and_disabling_signs_out(
    env: tuple[TestClient, Platform, FixedClock],
) -> None:
    client, platform, _ = env
    assert _login(client) == 204
    first = client.cookies.get("delphi_session")
    assert _login(client) == 204
    assert client.cookies.get("delphi_session") != first
    with platform.sessions() as s:  # the old session no longer exists
        assert len(s.execute(select(auth_sessions.c.id)).all()) == 1
    with platform.sessions.begin() as s:
        s.execute(update(users).values(disabled=True))
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Origin": ORIGIN}, 403),  # no custom header
        ({"X-Delphi-Csrf": "1", "Origin": "https://evil.example"}, 403),  # wrong origin
        ({"X-Delphi-Csrf": "1"}, 403),  # no origin
        (BROWSER, 401),  # passes CSRF, then fails authentication
    ],
)
def test_csrf_on_mutating_requests(
    env: tuple[TestClient, Platform, FixedClock], headers: dict[str, str], expected: int
) -> None:
    client, _, _ = env
    bare = TestClient(client.app, base_url="https://testserver")
    response = bare.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["code"] == "CSRF_REJECTED"
    assert bare.get("/api/v1/health").status_code == 200  # safe methods need nothing


def test_security_headers_on_every_response(env: tuple[TestClient, Platform, FixedClock]) -> None:
    client, _, _ = env
    for response in (
        client.get("/api/v1/health"),
        client.get("/api/v1/nope"),
        client.post("/api/v1/auth/login", json={}),
    ):
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
