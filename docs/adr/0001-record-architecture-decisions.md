# ADR-0001 — Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
DELPHI will be built over several milestones, possibly in sessions with coding agents that have
no memory of earlier reasoning (EFG §1 #12, §33).

## Decision
Architectural decisions are recorded as ADRs in `docs/adr/`, using `0000-template.md`, numbered
sequentially. They are never deleted; a superseded ADR is marked and links its successor. A PR
that changes architecture adds or updates an ADR in the same PR.

## Alternatives considered
- Decisions only in PR descriptions: hard to find, and lost on squash.
- A wiki: lives outside the repo and drifts from the code.

## Consequences
Small writing overhead. Future readers (and agents) can see *why*, not just *what*.
