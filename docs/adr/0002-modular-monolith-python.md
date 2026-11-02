# ADR-0002 — Modular monolith in Python (FastAPI), one image, api + worker processes

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
One developer, a single home server, a PoC timeline, and a heavy numerical/ML core (numpy,
LightGBM, scikit-learn). The research PoC plan (research §12) assumes Python + FastAPI.

## Decision
One Python 3.12 package `delphi` with strict module boundaries (doc 04 §4), enforced by
import-linter. One container image, run as `api` (FastAPI/uvicorn, also serving the SPA) and
`worker` (PostgreSQL-backed job runner).

## Alternatives considered
- Microservices per loop (Guardian/Navigator/Chase): distribution overhead with no independent
  scaling or team need (EFG §34 "premature microservices").
- .NET or Node backend: weaker ML ecosystem for this workload; the engine and models would need
  a Python sidecar anyway.
- Celery + Redis for jobs: a second stateful system with no requirement for it (ADR-0003).

## Consequences
- Positive: simple build, deploy and debugging; shared types; transactional consistency.
- Negative: module discipline must be enforced by tooling (import-linter), or boundaries will
  erode.
- Negative: CPU-heavy jobs share the worker. Bounded by the worker's job concurrency (2).
