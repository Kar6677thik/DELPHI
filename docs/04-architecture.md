# 04 — Architecture

## 1. Architectural style

DELPHI is a **modular monolith** (ADR-0002, EFG §5 "default decision"). It is one Python package
and one container image, run as two processes:

- **`api`**: FastAPI (uvicorn). Serves the REST API, Server-Sent Events and the built React SPA.
- **`worker`**: a job runner that claims jobs from a PostgreSQL table with
  `FOR UPDATE SKIP LOCKED`. It runs long-running work: dataset generation, model training,
  evaluation runs, Scout runs, learning runs, backtests, LLM narratives, and the simulated
  mule-network movements and ticket acknowledgements.

PostgreSQL is the only stateful dependency (ADR-0003). There is no message broker and no cache
server. A cache is added only when a measurement shows the need (EFG §12).

Inside the package: **functional core, imperative shell**. `delphi.engine` is pure. Application
services orchestrate I/O around it. External systems sit behind **ports** (Protocol classes)
with simulated adapters (hexagonal, EFG §5).

## 2. C4 — system context

```
                       ┌──────────────────────────┐
  Judges / guests ───► │                          │ ──► Anthropic Claude API (narratives, translations)
  Investigators   ───► │   DELPHI (web app)       │
  FDE engineers   ───► │                          │ ◄── (simulated, in-process) bank core, behavioural SDK,
  Presenter       ───► │                          │      telco call status, I4C registry, DoT FRI,
                       └──────────────────────────┘      CFCFRMS, external banks
        ▲ HTTPS via Cloudflare Tunnel
```

## 3. C4 — containers (k3s namespace `delphi`)

| Container | Image | Purpose | Port |
|---|---|---|---|
| `delphi-api` (Deployment, 1 replica) | `ghcr.io/kar6677thik/delphi:sha-<git sha>` cmd `delphi api` | HTTP API + SSE + static SPA | 8000 (app), 9000 (metrics, cluster-internal) |
| `delphi-worker` (Deployment, 1 replica) | same image, cmd `delphi worker` | Jobs and simulation clock tasks | 9000 (metrics) |
| `delphi-postgres` (StatefulSet, 1) | `postgres:16` (pinned digest) | All persistent state | 5432 |
| `delphi-migrate` (Job, Argo CD PreSync hook) | same image, cmd `delphi migrate` | `alembic upgrade head`, then load KB v1 if no KB version exists | — |
| `delphi-seed` (Job, Argo CD PostSync hook) | same image, cmd `delphi seed --if-empty --profile demo` | Seeds KB v1, demo dataset, trains models (idempotent) | — |
| `delphi-pg-backup` (CronJob) | `postgres:16` | Nightly `pg_dump` to a PVC | — |
| `cloudflared` (Deployment, 1) | `cloudflare/cloudflared` (pinned) | Tunnel from `delphi.<domain>` to `delphi-api:8000` | — |

## 4. Python package and modules (bounded contexts)

```
backend/src/delphi/
  engine/        pure decision maths (doc 06)                     — no I/O
  kb/            KB model types, YAML loader, validator, versions (doc 07)
  sensing/       features, behavioural baselines, models, prior (doc 13)
  probes/        probe + evidence ports, sim adapters, outcome classifiers, question bank
  guardian/      Guardian sessions (doc 08)
  navigator/     cases, evidence, campaigns, traces, DD pack (doc 09)
  chase/         taint tracing, ranking, liens, tickets (doc 10)
  scout/         clustering, typology drafts, backtest, shadow (doc 11)
  learning/      Dirichlet updates, OPE, yield drift (doc 12)
  llm/           provider port, Anthropic adapter, template adapter, prompts, validators (doc 15)
  sim/           synthetic bank, scenarios, response models, labels, external-system sims (doc 14)
  evaluation/    harness, baselines, metrics (doc 22)
  platform/      config, db, auth, audit, jobs, clock, logging, ids, errors, SSE hub
  api/           FastAPI routers and DTOs (thin; no business logic)
  cli.py         `delphi` command (api | worker | migrate | seed | kb validate | eval ...)
```

### 4.1 Dependency rules (enforced by `import-linter` in CI)

1. `engine` imports only `numpy`, the standard library and `delphi.kb.model`.
2. `kb.model` imports only `pydantic` and the standard library.
3. Feature modules (`guardian`, `navigator`, `chase`, `scout`, `learning`, `sensing`, `probes`)
   may import `engine`, `kb`, `platform`, `llm`, and **only the public `api.py` facade** of
   another feature module. They never import its internals.
4. `api` (routers) imports feature-module facades and `platform`. Nothing imports `api`.
5. **Only** `sim` and `evaluation` may read the `sim_truth` schema. This is enforced twice:
   by an import contract on `delphi.sim.truth`, and at the database by role grants (doc 17 §2).
6. No cycles between modules.

### 4.2 Ports (Protocols) and adapters

| Port | Used by | PoC adapter | Future real adapter |
|---|---|---|---|
| `BeneficiaryIntelPort` | probes | `SimBeneficiaryIntel` (sim registry + mule scorer) | I4C Suspect Registry, MuleHunter, DPIP |
| `PhoneRiskPort` | probes | `SimPhoneRisk` | DoT FRI API |
| `DeviceTelemetryPort` | probes | `SimDeviceTelemetry` (from session telemetry rows) | Mobile SDK |
| `CallStatusPort` | probes | `SimCallStatus` | On-device / telco signal |
| `CustomerChannelPort` | guardian | `MockPhoneChannel` (SSE to the mock phone UI) or `SimCustomerChannel` (response models) | Mobile app push |
| `EvidencePort` per evidence item | navigator | SQL over the sim bank tables + `SimOutreachCall` | Core banking, CRM |
| `LienPort` | chase | `SimLienService` | Core banking lien API |
| `CfcfrmsPort` | chase | `SimCfcfrms` | CFCFRMS |
| `LlmPort` | navigator, scout, llm jobs | `AnthropicLlm`, `TemplateLlm` | — |
| `Clock` | everything except engine | `SystemClock`, `VirtualClock` (evaluation) | — |

The simulated adapters for the external systems live with their consumers and call the
samplers in `delphi.sim.external` (doc 14 §9, #57):

- `SimPhoneRisk` in `delphi.probes` (M3/M4) reads `bank.sim_phone_risk`, and `SimCfcfrms` in
  `delphi.chase` (M6) draws each ticket's acknowledgement from its own stream. Both run in the
  api as well as the worker, so they never import `delphi.sim.truth` or read `sim_truth`
  (§4.1 rule 5).
- `SimOutreachCall` (the navigator's evidence, M5) answers from the response model of the
  payment's true scenario, so like `SimCustomerChannel` it lives under `delphi.sim` and runs
  only in the worker, which holds the `delphi_sim` pool (§5.1).

Adapters are chosen in one composition root (`delphi.platform.wiring`) from configuration.
There are no hidden singletons; dependencies are passed explicitly (EFG §2 "prefer
explicitness").

## 5. Key runtime flows

### 5.1 Guardian (payment time)

```
Mock phone ──POST /guardian/sessions──► api ─► guardian.start_session
   │                                            ├─ sensing.prior(payment)            (≤ 50 ms)
   │                                            ├─ fast path? → RELEASE
   │                                            ├─ phase A: passive probes (parallel, 300 ms timeout each)
   │                                            ├─ engine.plan_next → active probe | STOP
   │◄──── SSE: probe prompt (question in customer's language) ──┘
   │──── POST /guardian/sessions/{id}/probe-responses ──► classify outcome → update → plan …
   │◄──── SSE: decision (RELEASE / HOLD_FOR_REVIEW / PROTECTIVE_HOLD / DECLINE)
                                                └─ on hold: navigator.open_case(...)
```

**Where sessions run.** Each session's steps run as one asyncio task in the process that owns
it, recorded in `guardian_sessions.runner`:

- `API`: sessions driven by a human on the mock phone. They are created through the API and
  answered through the API.
- `WORKER`: sessions answered by the simulator (background traffic, autoplay story runs, S-4
  injections). `SimCustomerChannel` must read scenario truth, so these run in the worker, which
  holds the `delphi_sim` connection pool (doc 17 §2).

The api process never touches `sim_truth`. Session state is persisted after every step. On
start-up, each process resumes the unfinished sessions it owns (FR-GDN-14).

### 5.2 Navigator (case time)

Evidence fetches are synchronous in the api process (deterministic, ≤ 2 s). LLM narratives are
enqueued as `LLM_NARRATIVE` jobs. The worker writes the narrative, and the UI receives it over
SSE.

### 5.3 Chase

`POST /chase/operations` traces taint synchronously (≤ 5 s on the demo dataset) and returns
ranked candidates. Approved liens on internal accounts are placed at once. External liens
create `cfcfrms_tickets`; the worker acknowledges them after the simulated latency, and then
places or fails the lien depending on whether the funds are still present. In demo mode, the
worker also replays the mule network's scheduled onward transfers (doc 14 §8).

### 5.4 Jobs

`jobs` table, typed job handlers, at-least-once execution. Handlers MUST be idempotent: they use
the job's `idempotency_key` and upsert their results (EFG §19). The worker runs at most 2 jobs
concurrently. `SIM_TICK` and `CHASE_RETRACE` have their own lane, so long jobs cannot starve
them.

**On-demand job types:**

| Type | Enqueued by |
|---|---|
| `GENERATE_DATASET` | `POST /admin/datasets`, seed job |
| `TRAIN_SENSING` | `POST /admin/sensing/train`, after dataset generation |
| `RUN_EVALUATION` | `POST /evaluations` |
| `RUN_SCOUT` | `POST /scout/runs`, scout auto-trigger |
| `RUN_LEARNING` | `POST /learning/runs` |
| `RUN_BACKTEST` | `POST /typology-drafts/{id}/backtest` |
| `KB_REBASE` | "Recreate on current ACTIVE" (doc 07 §1) |
| `LLM_NARRATIVE` | evidence fetch |
| `LLM_CASE_SUMMARY` | DD pack request |
| `LLM_QUESTION_VARIANTS` | `POST /question-bank/generate` |
| `LLM_TYPOLOGY_DESCRIPTION` | Scout run |
| `SIM_OUTREACH_CALL` | outreach in autoplay/simulation (doc 09 §3) |
| `DEMO_STORY` | `POST /admin/demo/scenarios/{story}/run` |
| `DEMO_RESET` | `POST /admin/demo/reset` |

**Recurring jobs.** A scheduler loop in the worker enqueues these with idempotency key
`<TYPE>:<slot start ISO>`, so a slot runs once even with restarts or two workers:

| Type | Interval | Does |
|---|---|---|
| `SIM_TICK` | 10 s real (only when `DELPHI_TIME_MODE=DEMO`) | doc 14 §8: due mule transfers, background payments, ticket acks/expiry |
| `CHASE_RETRACE` | `retrace_interval_min` time-scaled, floor 20 s | re-trace open operations; auto-close (doc 10 §5) |
| `MULE_SWEEP` | `mule_sweep_interval_min` time-scaled | recompute `account_scores`; open ACCOUNT_REVIEW cases (doc 09 §1, from M5) |
| `SESSION_TIMEOUT_SWEEP` | 30 s | resolve sessions past their timeouts whose owner process is gone |
| `SCOUT_AUTO_TRIGGER` | 5 min | enqueue `RUN_SCOUT` if ≥ `auto_trigger_new_items` new queue items |
| `LABEL_DELIVERY` | 60 s (DEMO), virtual in evaluation | deliver due rows from `sim_truth.label_schedule` (doc 14 §7) |
| `PROBE_YIELD_DAILY` | daily 00:15 IST | aggregate `probe_yield_daily`; evaluate drift alerts (doc 12 §5) |
| `RETENTION_SWEEP` | daily 01:00 IST | doc 17 §6 |
| `AUDIT_VERIFY` | daily 02:00 IST | verify the last 24 h of the audit chain; alert on failure |

### 5.5 Live updates

SSE endpoints stream events from an in-process hub. When the worker produces an event, it
writes a row to `outbox_events` and issues `NOTIFY delphi_events`. The api process `LISTEN`s
and fans events out to subscribers. This is a transactional outbox (EFG §11), so no event is
lost if the api restarts: on reconnect, the client sends `Last-Event-ID` and the server replays
from `outbox_events`.

## 6. Cross-cutting concerns

| Concern | Approach | Doc |
|---|---|---|
| AuthN/Z | Server-side sessions (httpOnly cookie) + RBAC checked in every router dependency | 19 |
| Audit | Append-only, hash-chained `audit_log` written in the same transaction as the change | 17, 19 |
| Idempotency | `Idempotency-Key` header on state-creating POSTs | 16 |
| Concurrency | Optimistic locking (`version` column + `If-Match`) on cases, typology drafts, KB promotions | 16, 17 |
| Config | `pydantic-settings`, env vars prefixed `DELPHI_`, validated at startup (fail fast) | 23 |
| Errors | RFC 9457 `application/problem+json` with stable `code` values | 16 |
| Logging | JSON structured logs with request id, session/case ids, no PII values | 20 |
| Time | `Clock` port; UTC; time scale applied only to simulated durations | 14 |

## 7. Quality attributes → design choices

| Attribute | Target (NFR) | How |
|---|---|---|
| Decision latency | Planner p95 ≤ 50 ms; passive phase p95 ≤ 400 ms | Pure numpy engine, memoised lookahead, parallel passive adapters |
| Determinism | Same seed and inputs give identical results | Seeded RNGs, no wall-clock reads in core logic, canonical KB hashing |
| Auditability | 100% of decisions have a DD pack | Everything persisted per step; hash-chained audit |
| Simplicity | One image, one database | ADR-0002, ADR-0003 |
| Evolvability | New typology without code; new probe = code + KB | KB versions (ADR-0006), ports |
