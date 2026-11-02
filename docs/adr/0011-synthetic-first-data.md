# ADR-0011 — Synthetic-first data; external datasets optional

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
No real bank data is available. The core claims (coercion probes, probe responses, selective
labels, golden-hour chase) need signals that no public dataset contains (research §17.5, the
APP-scam academic gap).

## Decision
A deterministic simulator (doc 14) is the primary data source. Its response models are
deliberately perturbed from KB v1, and it includes label noise, coached victims and a hidden
typology. Public datasets (BAF, PaySim, AMLworld) are a post-M9 stretch, for benchmarking the
sensing layer only.

## Alternatives considered
- Building around AMLworld/BAF first: schema mismatch, too large for a home server, and no probe
  responses at all.

## Consequences
- Positive: full control and reproducibility; every demo story is scriptable.
- Negative: results are only as credible as the simulator. Mitigated by the honest caveats
  (doc 22 §6) and by not tuning the KB to the evaluation stream.
