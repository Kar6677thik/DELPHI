# ADR-0004 — Bayesian decision engine as a pure functional core; the LLM never decides

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
Holds, freezes and dispositions have legal and customer consequences. Courts require recorded,
specific reasons (research §6). RBI FREE-AI emphasises human oversight (research §17.2). LLM
output is non-deterministic and can hallucinate.

## Decision
All probe selection, belief updates and decision recommendations come from `delphi.engine`. It
is pure, deterministic, numpy-only code with explicit loss tables. Outcomes are classified by
deterministic rules. LLMs only phrase text (narratives, summaries, offline question drafts,
typology descriptions). They have no tools, their output is validated, and humans approve
anything customer-facing. Liens and police alerts need explicit human approval.

## Alternatives considered
- An LLM agent that "investigates and decides" (vendor trend, research §4.2): unauditable,
  non-reproducible, and contrary to DELPHI's core claim.
- An RL policy for probe selection: needs a lot of data, is hard to explain, and the
  value-of-information formulation is optimal under its assumptions and explainable.

## Consequences
- Positive: every decision is reproducible and explainable with numbers in the DD pack.
- Positive: the demo works with no LLM at all (NFR-12).
- Negative: the naive-Bayes independence assumption is a known simplification (documented).
