"""Prometheus metrics and the :9000 listener (docs/20 §2, FR-DEP-04)."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from delphi.api.app import create_app
from delphi.platform.metrics import SSE_CLIENTS, metrics_server, status_class
from delphi.platform.settings import Settings
from delphi.platform.wiring import build_platform

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
UNREACHABLE_DB = "postgresql://nobody:x@127.0.0.1:1/none"


def _serve(server: ThreadingHTTPServer) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _get(url: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers["Content-Type"], ""


@pytest.fixture
def api_metrics() -> Iterator[str]:
    server = metrics_server(0)
    try:
        yield _serve(server)
    finally:
        server.shutdown()


def test_metrics_are_served_in_the_prometheus_text_format(api_metrics: str) -> None:
    SSE_CLIENTS.set(2)
    status, content_type, body = _get(f"{api_metrics}/metrics")
    assert status == 200
    assert content_type.startswith("text/plain; version=0.0.4")  # what every scraper reads
    assert "# TYPE delphi_http_requests_total counter" in body
    assert "# TYPE delphi_jobs_queued gauge" in body
    assert "delphi_sse_clients 2.0" in body


def test_the_api_listener_has_no_health_and_nothing_else(api_metrics: str) -> None:
    assert _get(f"{api_metrics}/health")[0] == 404
    assert _get(f"{api_metrics}/")[0] == 404


def test_http_requests_are_counted_by_route_template_not_raw_path(tmp_path: Path) -> None:
    settings = Settings(
        database_url=UNREACHABLE_DB,
        version="deadbeef",
        migrations_dir=MIGRATIONS_DIR,
        static_dir=tmp_path / "no-frontend-build",
        db_connect_timeout_s=1,
    )
    platform = build_platform(settings, process="test")
    try:
        client = TestClient(create_app(platform))
        labels = {"method": "GET", "route": "/api/v1/health", "status_class": "2xx"}
        before = REGISTRY.get_sample_value("delphi_http_requests_total", labels) or 0.0
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/health").status_code == 200
        assert REGISTRY.get_sample_value("delphi_http_requests_total", labels) == before + 2
        count = REGISTRY.get_sample_value(
            "delphi_http_request_duration_seconds_count", {"route": "/api/v1/health"}
        )
        assert count is not None and count >= 2

        client.get("/api/v1/no-such-thing")
        unmatched = {"method": "GET", "route": "<unmatched>", "status_class": "4xx"}
        assert REGISTRY.get_sample_value("delphi_http_requests_total", unmatched)
    finally:
        platform.close()


def test_the_public_port_never_serves_metrics(tmp_path: Path) -> None:
    """FR-DEP-04: /metrics on the app port is not an exposition body."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>DELPHI</title>", encoding="utf-8")
    settings = Settings(
        database_url=UNREACHABLE_DB,
        version="deadbeef",
        migrations_dir=MIGRATIONS_DIR,
        static_dir=dist,
        db_connect_timeout_s=1,
    )
    platform = build_platform(settings, process="test")
    try:
        client = TestClient(create_app(platform))
        for path in ("/metrics", "/api/v1/metrics"):
            assert "# HELP" not in client.get(path).text
    finally:
        platform.close()


@pytest.mark.parametrize(("code", "expected"), [(200, "2xx"), (304, "3xx"), (503, "5xx")])
def test_status_class(code: int, expected: str) -> None:
    assert status_class(code) == expected
