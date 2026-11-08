"""Prometheus metrics (docs/20 §2) and the cluster-internal listener on ``metrics_port``.

Both processes serve ``GET /metrics`` on port 9000, which is never routed publicly (FR-DEP-04).
The worker serves its ``GET /health`` from the same listener (docs/20 §3).

Labels stay bounded: route templates, job types, status names. Never ids.
This module defines the metrics whose subsystems exist so far. Guardian, probes, cases, LLM
and KB-version metrics are added here when those modules land.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4, generate_latest

# Seconds. Jobs range from a sub-second sweep to a dataset generation of several minutes.
_JOB_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)

HTTP_REQUESTS = Counter(
    "delphi_http_requests",
    "HTTP requests served by the api.",
    ["method", "route", "status_class"],
)
HTTP_REQUEST_DURATION = Histogram(
    "delphi_http_request_duration_seconds",
    "Time to serve an HTTP request (an SSE stream counts its whole connection).",
    ["route"],
)
JOBS = Counter(
    "delphi_jobs",
    "Job attempts finished by the worker, by outcome: SUCCEEDED, RETRY (failed, queued again),"
    " FAILED (no attempts left) or LOST (the lease was taken over).",
    ["type", "status"],
)
JOB_DURATION = Histogram(
    "delphi_job_duration_seconds",
    "Time a job handler ran, per attempt.",
    ["type"],
    buckets=_JOB_BUCKETS,
)
JOBS_QUEUED = Gauge(
    "delphi_jobs_queued",
    "Jobs waiting in the queue (status QUEUED), sampled by the worker's scheduler loop.",
    ["type"],
)
SSE_CLIENTS = Gauge("delphi_sse_clients", "Open live-event (SSE) connections to this api.")


def status_class(code: int) -> str:
    return f"{code // 100}xx"


# (status code, JSON body) for GET /health, or None when the process serves no /health here.
HealthCheck = Callable[[], tuple[int, dict[str, Any]]]


def metrics_server(port: int, health: HealthCheck | None = None) -> ThreadingHTTPServer:
    """A small HTTP listener: ``GET /metrics`` and, with ``health``, ``GET /health``.
    The caller runs ``serve_forever`` in a thread and calls ``shutdown`` on exit."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                # The classic text format: every Prometheus-compatible scraper reads it.
                self._reply(200, CONTENT_TYPE_PLAIN_0_0_4, generate_latest(REGISTRY))
            elif self.path == "/health" and health is not None:
                code, body = health()
                self._reply(code, "application/json", json.dumps(body).encode())
            else:
                self.send_error(404)

        def _reply(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:  # probes and scrapes: no log noise
            return

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)  # nosec B104: inside the pod only
