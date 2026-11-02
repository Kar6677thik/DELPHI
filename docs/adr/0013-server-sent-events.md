# ADR-0013 — Server-Sent Events for live UI updates

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
The phone, the monitors and the chase screen need server push. Client → server messages are
ordinary REST calls.

## Decision
SSE, with event ids from the transactional outbox, `Last-Event-ID` replay, 15 s heartbeats, and
`LISTEN/NOTIFY` fan-out from worker to api.

## Alternatives considered
- WebSockets: a bidirectional channel is not needed, and they add proxy and auth complexity.
- Polling: laggy demo and wasteful.

## Consequences
- Positive: works through Cloudflare; plain HTTP and cookies.
- Negative: long-lived connections use api resources (bounded; tracked by the
  `delphi_sse_clients` metric).
