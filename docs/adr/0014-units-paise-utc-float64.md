# ADR-0014 — Money as integer paise, time as UTC, probabilities as float64

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
Rounding errors in money and time-zone bugs are classic correctness failures.

## Decision
- **Money** is stored and transported as integer paise (`*_paise`). Inside the engine it is
  computed in float64 and rounded (banker's rounding) only when persisted.
- **Time** is `timestamptz` in UTC, shown in IST.
- **Probabilities** are float64, with a 1e-4 likelihood floor and log-space updates. JSON
  carries probabilities with 6 decimals.

## Alternatives considered
- Decimal rupees: slower in numpy, and invites mixed units.

## Consequences
- Positive: no float rupees in APIs; consistent formatting via `MoneyText`.
