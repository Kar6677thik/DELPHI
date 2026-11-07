"""Every endpoint against every role (FR-PLT-02, docs/19 §3): deny by default.

``ACCESS`` lists every API route with what it needs. A route missing from it fails the test, so
a new endpoint cannot ship without a decision about who may call it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.platform.accounts import PERMISSIONS, Permission, Role, create_user
from delphi.platform.audit import ActorType
from delphi.platform.clock import FixedClock
from delphi.platform.db import make_session_factory, migration_head
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform

from ._db import MIGRATIONS_DIR, engine_as, upgrade

PUBLIC, SIGNED_IN = "public", "signed-in"
ACCESS: dict[tuple[str, str], str | Permission] = {
    ("GET", "/api/v1/openapi.json"): PUBLIC,
    ("GET", "/api/v1/health"): PUBLIC,
    ("GET", "/api/v1/ready"): PUBLIC,
    ("POST", "/api/v1/auth/login"): PUBLIC,
    ("POST", "/api/v1/auth/logout"): SIGNED_IN,
    ("GET", "/api/v1/auth/me"): SIGNED_IN,
    ("GET", "/api/v1/events/stream"): Permission.VIEW,
    ("GET", "/api/v1/jobs/{job_id}"): Permission.VIEW,
    ("GET", "/api/v1/jobs"): Permission.ADMIN,
    ("POST", "/api/v1/jobs/{job_id}/retry"): Permission.ADMIN,
    ("GET", "/api/v1/admin/users"): Permission.ADMIN,
    ("POST", "/api/v1/admin/users"): Permission.ADMIN,
    ("PATCH", "/api/v1/admin/users/{user_id}"): Permission.ADMIN,
    ("GET", "/api/v1/admin/audit/verify"): Permission.ADMIN,
}
PASSWORD = "correct horse battery"
BROWSER = {"X-Delphi-Csrf": "1", "Origin": "https://testserver"}


@pytest.fixture(scope="module")
def clients(create_database: Callable[[], str]) -> Iterator[dict[Role | None, TestClient]]:
    """One signed-in client per role, plus an anonymous one (None)."""
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
    app = create_app(platform)
    result: dict[Role | None, TestClient] = {}
    for role in Role:
        with platform.sessions.begin() as s:
            create_user(
                s,
                clock,
                username=role.value.lower(),
                display_name=role.value,
                password=PASSWORD,
                role=role,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )
        client = TestClient(app, base_url="https://testserver", headers=BROWSER)
        login = client.post(
            "/api/v1/auth/login", json={"username": role.value.lower(), "password": PASSWORD}
        )
        assert login.status_code == 204
        result[role] = client
    result[None] = TestClient(app, base_url="https://testserver", headers=BROWSER)
    yield result
    engine.dispose()


def _api_routes(client: TestClient) -> set[tuple[str, str]]:
    """Every API operation, from the generated contract (which every API route is in: the SPA
    fallback is the only route kept out of it), plus the contract itself."""
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    routes = {(method.upper(), path) for path, item in paths.items() for method in item}
    return routes | {("GET", "/api/v1/openapi.json")}


def test_every_api_route_has_a_declared_access_rule(clients: dict[Role | None, TestClient]) -> None:
    assert _api_routes(clients[None]) == set(ACCESS)


@pytest.mark.parametrize("role", [None, *Role], ids=lambda r: getattr(r, "value", "anonymous"))
@pytest.mark.parametrize(("method", "path"), sorted(ACCESS))
def test_access(
    clients: dict[Role | None, TestClient], role: Role | None, method: str, path: str
) -> None:
    rule = ACCESS[(method, path)]
    url = path.replace("{job_id}", str(uuid4())).replace("{user_id}", str(uuid4()))
    if (method, path) in {("POST", "/api/v1/auth/logout")}:
        # Signing out would end this module's shared session; check access with a probe
        # that needs the same rule (a signed-in caller).
        method, url = "GET", "/api/v1/auth/me"
    if (method, path) == ("POST", "/api/v1/auth/login"):
        status = clients[role].post(url, json={"username": "x", "password": "y"}).status_code
        assert status == 401  # reachable by anyone: wrong credentials, not "forbidden"
        return
    status = clients[role].request(method, url).status_code
    if rule == PUBLIC:
        allowed = True
    elif role is None:
        assert status == 401
        return
    else:
        allowed = rule == SIGNED_IN or rule in PERMISSIONS[role]
    if allowed:
        assert status not in (401, 403), status
    else:
        assert status == 403
