"""The api serves the built SPA at ``/`` and keeps ``/api/*`` errors as problem+json
(docs/18, ADR-0008, FR-DEP-04)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from delphi.api.app import create_app
from delphi.api.spa import IMMUTABLE_CACHE, REVALIDATE_CACHE
from delphi.platform.settings import Settings
from delphi.platform.wiring import build_platform

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
UNREACHABLE_DB = "postgresql://nobody:x@127.0.0.1:1/none"
INDEX_HTML = "<!doctype html><title>DELPHI</title><div id=root></div>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the dist", encoding="utf-8")
    return root


def _client(static_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=UNREACHABLE_DB,
        migrations_dir=MIGRATIONS_DIR,
        static_dir=static_dir,
        db_connect_timeout_s=1,
    )
    platform = build_platform(settings, process="test")
    yield TestClient(create_app(platform), raise_server_exceptions=False)
    platform.close()


@pytest.fixture
def client(dist: Path) -> Iterator[TestClient]:
    yield from _client(dist)


@pytest.mark.parametrize("path", ["/", "/guardian", "/cases/0192-abc", "/login?next=%2Fkb"])
def test_client_side_routes_get_index_html(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == REVALIDATE_CACHE


def test_hashed_assets_are_cached_forever(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)"
    assert response.headers["cache-control"] == IMMUTABLE_CACHE


def test_other_static_files_are_revalidated(client: TestClient) -> None:
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE_CACHE


def test_missing_asset_is_404_not_index(client: TestClient) -> None:
    response = client.get("/assets/index-old999.js")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", ["/api", "/api/nope", "/api/v1/nope", "/api/v1/cases/1"])
def test_unknown_api_paths_stay_problem_json(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["instance"] == path


def test_api_routes_win_over_the_fallback(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "path", ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"]
)
def test_paths_cannot_escape_the_static_dir(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code in {200, 404}
    assert "outside the dist" not in response.text


def test_nothing_is_served_without_a_build(tmp_path: Path) -> None:
    for client in _client(tmp_path / "missing"):
        response = client.get("/")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert client.get("/api/v1/health").status_code == 200


def test_the_fallback_is_not_in_the_contract(client: TestClient) -> None:
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    assert set(paths) == {"/api/v1/health", "/api/v1/ready"}
