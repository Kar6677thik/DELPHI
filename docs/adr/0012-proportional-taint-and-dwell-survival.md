# ADR-0012 — Proportional taint tracing and exponential dwell-time survival for Chase

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
Chase must rank liens by expected recoverable value per hop in real time (research §9.6), and
lien only the traced amount.

## Decision
Taint propagates proportionally (haircut) over the ledger, with cash-out leakage. An
exponential survival model estimates whether funds are still present at freeze time, using an
account-specific mean dwell. Net value = ERV − expected wrongful-lien cost.

## Alternatives considered
- FIFO/LIFO tracing: common in some jurisdictions, but order-sensitive and less stable under
  splits. Proportional is the conservative middle and easy to explain.
- Temporal link prediction (GNN) for next hops: research-grade; deferred to the roadmap.

## Consequences
- Positive: simple, deterministic and property-testable (conservation).
- Negative: the dwell model is crude. This is documented, and its parameters are versioned in
  the KB.
