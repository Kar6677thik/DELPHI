# ADR-0003 — PostgreSQL as the only datastore

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
We need relational data, a job queue, event fan-out for SSE, model artifact storage and an
append-only audit log. Every additional stateful component on a home server adds backup, upgrade
and failure surface.

## Decision
PostgreSQL 16 holds everything: domain data, the `jobs` queue (`FOR UPDATE SKIP LOCKED`), the
transactional outbox + `LISTEN/NOTIFY` for SSE, model artifacts (`bytea`), and the hash-chained
audit log. No Redis, Kafka, object storage or vector database.

## Alternatives considered
- Redis for queue/cache/pub-sub: unnecessary at demo scale; would also need its own durability
  story.
- MinIO for artifacts: models are a few MB.
- Neo4j for the Chase graph (research §12 optional): graph sizes per chase are small; Python
  tracing over ledger rows is enough, and the UI renders the graph.

## Consequences
- Positive: one backup covers everything; transactions span the job, the outbox and the domain
  writes.
- Negative: throughput ceilings are far above the demo's needs. Revisit if the Guardian exceeds
  ~200 sessions/s.
