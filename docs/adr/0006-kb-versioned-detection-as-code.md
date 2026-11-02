# ADR-0006 — Knowledge base as immutable, versioned bundles (detection-as-code)

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
Typologies, likelihoods, losses and guardrails change through learning runs and FDE work.
Decisions must be reproducible against exactly the configuration used (research §9.7, and the
DoorDash and Monzo patterns in research §17.5).

## Decision
The engine reads one immutable KB version (canonical JSON + SHA-256). KB v1 is YAML in `kb/`,
validated in CI. New versions are created in-app (learning, typology promotion) with lineage,
diff, shadow evaluation and four-eyes promotion. Export to YAML lets the owner commit versions
back to git manually. Probes remain code: a KB can only enable sources whose classifier exists.

## Alternatives considered
- Mutable config tables: no reproducibility.
- A git-only KB with CI-driven promotion: would need bot commits (forbidden) and slows the live
  demo loop.

## Consequences
- Positive: replays, backtests and DD packs reference an exact version.
- Negative: two homes for KB truth (git for v1, the DB for later versions), bridged by export.
