# 20 — Observability

Proportionate to a single-node demo (KISS). There is no tracing backend and no log aggregation
stack. Logs go to stdout (`kubectl logs`), and Prometheus-format metrics are exposed for whatever
scraper the owner runs. If none runs, the in-app metrics pages cover the business KPIs.

## 1. Logging

- `structlog`, JSON lines to stdout. Fields: `ts`, `level`, `event`, `logger`, `request_id`,
  `user_id`, `session_id`/`case_id`/`job_id` when relevant, `kb_version_id`, `duration_ms`.
- Levels: INFO for state transitions and job lifecycle; WARNING for fallbacks (LLM → template,
  adapter timeout, fail-safe decision); ERROR for unhandled exceptions (with traceback).
- Never logged: passwords, session tokens, API keys, raw cookie headers, full account numbers,
  customer names. A redaction processor masks keys matching
  `(?i)(password|token|secret|api_key|authorization|cookie)`.
- Uvicorn access logs are disabled in favour of one structured request log line per request.

## 2. Metrics (`/metrics` on port 9000, cluster-internal only)

`prometheus-client`. Bounded label cardinality: no ids as labels (EFG §24).

- **Where:** both processes listen on `metrics_port` (9000). The api serves only `/metrics`
  there; the worker serves `/metrics` and `/health`. Port 9000 is never routed publicly, and the
  app port's `/metrics` is the SPA page (FR-DEP-04). The body is the classic text format
  (`text/plain; version=0.0.4`), plus the client library's standard `process_*` and `python_*`
  metrics.
- **Which process:** HTTP and SSE metrics come from the api; job metrics from the worker.
  `delphi_jobs_queued` is sampled by the worker's scheduler loop (every
  `worker_poll_interval_s`), with a series for every job type.
- **`delphi_jobs_total{status}`:** one per finished attempt: `SUCCEEDED`, `RETRY` (failed,
  queued again with backoff), `FAILED` (no attempts left) or `LOST` (the lease was taken over).
- **Rollout:** each metric is added with the module it measures. Today: the HTTP, job and SSE
  metrics. The Guardian, probe, case, LLM and KB-version metrics arrive in M1–M5.

| Metric | Type | Labels |
|---|---|---|
| `delphi_http_requests_total` | counter | method, route template, status class |
| `delphi_http_request_duration_seconds` | histogram | route template |
| `delphi_guardian_sessions_total` | counter | decision, terminal_reason |
| `delphi_guardian_planner_seconds` | histogram | — |
| `delphi_guardian_passive_phase_seconds` | histogram | — |
| `delphi_probe_runs_total` | counter | probe_key, status |
| `delphi_probe_yield_degraded` | gauge | source_key |
| `delphi_cases_open` | gauge | type, protocol |
| `delphi_case_sla_breached` | gauge | type |
| `delphi_jobs_total` | counter | type, status |
| `delphi_job_duration_seconds` | histogram | type |
| `delphi_jobs_queued` | gauge | type |
| `delphi_llm_calls_total` | counter | purpose, status, provider |
| `delphi_llm_tokens_total` | counter | direction |
| `delphi_sse_clients` | gauge | — |
| `delphi_kb_active_version` | gauge | — (value = version id) |

## 3. Health

- `/api/v1/health` (liveness and startup probe): process up; returns the version. Never
  touches the DB, so a DB outage does not trigger restart loops.
- `/api/v1/ready` (readiness): DB ping (≤ 500 ms), Alembic head matches, an ACTIVE KB exists
  (from M1). 503 lists the failing checks. Dataset and model readiness are **not** readiness
  conditions; they are reported by `/api/v1/status`, and feature endpoints return
  `503 NOT_INITIALISED` until they are ready.
- The worker exposes `/health` on port 9000 and is ready when the job loop heartbeat is less
  than 30 s old.

## 4. Alerts (in-app; no paging for a demo)

The Overview page shows an alert banner for: job failures in the last hour; jobs queued > 10
minutes; LLM fallback rate > 50% over the last 20 calls; `PROBE_YIELD_DEGRADED`; SSE outbox lag
> 30 s; backup older than 36 h (latest `ops_backup_runs` row).

## 5. Deployment verification

The deploy workflow polls `GET https://<public host>/api/v1/health` until `version` equals the
pushed commit SHA (doc 23 §4). This is the production verification step (EFG §23).
