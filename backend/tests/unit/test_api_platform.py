"""HTTP behaviour that needs no database: liveness, request ids, problem+json (docs/16 §1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.platform.errors import InvalidStateTransitionError
from delphi.platform.settings import Settings
from delphi.platform.wiring import build_platform

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
# Nothing listens on port 1: connections are refused at once, so no test waits on a timeout.
UNREACHABLE_DB = "postgresql://nobody:x@127.0.0.1:1/none"


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    settings = Settings(
        database_url=UNREACHABLE_DB,
        version="deadbeef",
        migrations_dir=MIGRATIONS_DIR,
        # No SPA: its catch-all would shadow the routes added below, and the result must not
        # depend on whether a local frontend build exists.
        static_dir=tmp_path / "no-frontend-build",
        db_connect_timeout_s=1,
    )
    platform = build_platform(settings, process="test")
    application = create_app(platform)

    @application.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    @application.get("/conflict")
    def conflict() -> None:
        raise InvalidStateTransitionError("The case is already disposed.")

    @application.get("/items/{n}")
    def item(n: int) -> dict[str, int]:
        return {"n": n}

    yield application
    platform.close()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_health_needs_no_database(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "deadbeef"
    assert body["started_at"].endswith("Z") or "+00:00" in body["started_at"]


def test_ready_is_503_when_database_is_down(client: TestClient) -> None:
    response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "database": {"status": "failed", "detail": "database unreachable"},
            "migrations": {"status": "failed", "detail": "database unreachable"},
        },
    }


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    generated = client.get("/api/v1/health").headers["x-request-id"]
    assert len(generated) == 36
    echoed = client.get("/api/v1/health", headers={"X-Request-Id": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "bad id\twith junk"})
    assert response.headers["x-request-id"] != "bad id\twith junk"


def _assert_problem(response: object, status: int, code: str) -> dict[str, object]:
    from httpx import Response

    assert isinstance(response, Response)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, object] = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["request_id"] == response.headers["x-request-id"]
    return body


def test_unknown_route_is_a_not_found_problem(client: TestClient) -> None:
    body = _assert_problem(client.get("/api/v1/nope"), 404, "NOT_FOUND")
    assert body["instance"] == "/api/v1/nope"


def test_domain_error_maps_to_its_stable_code(client: TestClient) -> None:
    body = _assert_problem(client.get("/conflict"), 409, "INVALID_STATE_TRANSITION")
    assert body["detail"] == "The case is already disposed."


def test_validation_error_lists_the_fields(client: TestClient) -> None:
    body = _assert_problem(client.get("/items/notanint"), 422, "VALIDATION_FAILED")
    errors = body["errors"]
    assert isinstance(errors, list)
    assert errors[0]["path"] == "path.n"


def test_unhandled_error_hides_internals(client: TestClient) -> None:
    body = _assert_problem(client.get("/boom"), 500, "INTERNAL")
    assert "secret internals" not in str(body)


def test_request_log_uses_the_route_template(client: TestClient) -> None:
    import io
    import json

    from delphi.platform.logging import configure_logging

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    client.get("/api/v1/health")
    client.get("/items/7")
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    routes = [x["route"] for x in lines if x["event"] == "http.request"]
    assert routes == ["/api/v1/health", "/items/{n}"]
