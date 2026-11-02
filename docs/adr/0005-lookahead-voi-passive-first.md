# ADR-0005 — Depth-limited lookahead value of information, after a passive-first phase

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
The research specifies a* = argmax[VoI(a) − Cost(a)], stopping when it is ≤ 0 (research §9.2),
with greedy one-step lookahead. With realistic losses, one-step (myopic) VoI is often exactly
zero. Releasing a large payment needs a fraud probability of a few percent, and no single probe
gets there, so the myopic planner stops at once and holds everything.

The golden test (doc 06 §16) shows the distinction: VoI₁(OTP) = ₹0 against coercion is the
*desired* insight, but VoI₁ = ₹0 for every single probe in a genuine-but-unusual case (Arjun) is
a planner failure.

## Decision
1. Selection uses depth-limited lookahead: V_k(p, S) = min(R(p), min_a [C(a) + E_o V_{k−1}]),
   with k = 3 for Guardian and 2 for Navigator (doc 06 §6). Myopic VoI is still computed and
   shown, because it is the intuitive number.
2. Guardian runs every eligible passive (zero-friction) probe first, in parallel, without
   planning. Active probes are then planned.

## Alternatives considered
- Pure myopic VoI: the failure mode above.
- Entropy reduction per cost for selection, with a separate threshold stop rule: ignores the
  business loss structure. OTP would look informative, because it separates ATO even when ATO
  is irrelevant to the decision.
- Full POMDP / RL: overkill; hard to explain and to test.
- Planning passive probes too: adds complexity to save milliseconds. Passive VoI is still logged
  for analytics.

## Consequences
- Positive: correct multi-step behaviour (Arjun is released after the biometric; Kamala gets
  the spell-breaker).
- Negative: more compute, bounded by memoisation, the budget cap of 3 active probes and NFR-03.
- Pitch wording: "value of information with lookahead".
