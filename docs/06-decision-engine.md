# 06 — Decision engine

The decision engine is the core of DELPHI. It is a **pure functional core** (ADR-0004). It does
no I/O, reads no clock and keeps no global state. It takes a belief, a KB snapshot and the
session state, and returns numbers and choices. Everything here is deterministic given the
inputs and a seeded RNG.

Python module: `delphi.engine`. Its only dependencies are `numpy` and the immutable KB model
types from `delphi.kb.model`.

## 1. Notation

| Symbol | Meaning |
|---|---|
| H | Ordered tuple of applicable hypotheses for the decision profile (KB order, `UNEXPLAINED` always last) |
| p | Belief: vector over H, p(h) ≥ 0, Σ p(h) = 1 |
| S | Set of observation sources still eligible |
| a | An observation source (probe or evidence item) |
| O_a | Ordered outcome list of source a |
| L_a[o, h] | P(o \| h, a), the likelihood |
| D | Decision set of the profile |
| Loss(d, h) | Loss in paise for decision d when h is true |
| A | Amount at stake in paise |
| C(a) | Cost of observing a, in paise |
| k | Lookahead depth |

## 2. Likelihoods

Every (source a, hypothesis h) pair has a **Dirichlet row** α_{a,h} ∈ ℝ^{|O_a|}, with every
α > 0. The engine uses the posterior-mean likelihood:

```
L_a[o, h] = α_{a,h}[o] / Σ_o' α_{a,h}[o']
```

Rules:

1. **Unexplained.** `L_a[o, UNEXPLAINED] = 1 / |O_a|` for every source and outcome. It is never
   stored, never learned and never edited.
2. **Floor.** Every stored α MUST be ≥ 0.01, and every resulting likelihood MUST be ≥ 1e-4. KB
   validation rejects anything lower. No observation can therefore drive a hypothesis to exactly
   zero.
3. **Missing rows.** If a typology does not define a row for a source, the KB loader fills in a
   uniform row with ESS = 2. A hypothesis with no opinion about a source is not moved by it.
4. **Conditional independence.** Outcomes of different sources are assumed independent given h
   (naive Bayes). This is a known simplification. It is documented in the DD pack footer and in
   the pitch Q&A.

## 3. Belief update

The update runs in log space for numerical stability:

```
log p'(h) = log p(h) + log L_a[o, h]
p' = softmax(log p')            # subtract max, exp, normalise
```

The predictive probability of an outcome is `P(o | p, a) = Σ_h p(h) · L_a[o, h]`.

Invariants (property-tested, doc 21):

- p' sums to 1 within 1e-12.
- The order of observations does not change the final posterior (to within 1e-9).
- A source whose rows are identical for every h leaves p unchanged.

## 4. Loss and Bayes risk

For each decision profile the KB defines `Loss(d, h)` through two parameters, `base_inr` and
`rate`, plus a flag `vulnerability_scaled`:

```
Loss(d, h) = (base_inr · 100 + rate · A) · m
m = vulnerability_multiplier (1.5)  if vulnerability_scaled and customer.is_vulnerable
    1.0                              otherwise
```

Results are in paise and stay float64 during computation. They are rounded to integer paise
only when persisted.

```
risk(d | p)  = Σ_h p(h) · Loss(d, h)
R(p)         = min_d risk(d | p)
d*(p)        = argmin_d risk(d | p)
```

Tie-break for d*: the decision earlier in the profile's declared decision order wins. The order
lists less intrusive decisions first, e.g. `RELEASE`, `HOLD_FOR_REVIEW`, `PROTECTIVE_HOLD`,
`DECLINE`.

## 5. Myopic value of information (reported, not used to choose)

```
VoI_1(a | p) = R(p) − Σ_o P(o | p, a) · R(p^{a,o})
```

VoI_1 ≥ 0 always (property-tested). It is persisted for every candidate as `voi_myopic_paise`,
because it is the intuitive number shown in the UI ("this check is worth ₹1,686") and it feeds
the information-yield metrics.

**Why it is not used for selection (ADR-0005).** When the best decision does not change under
any single outcome, VoI_1 is 0 even though two observations together would change it. With
realistic losses this happens all the time. Releasing a large payment needs fraud probability
below a few percent, which no single probe can reach. Pure myopic selection would stop at once
and hold everything. The planner therefore uses depth-limited lookahead.

## 6. Lookahead planner

Define the depth-limited value function over the remaining source set S:

```
V_0(p, S) = R(p)

Q_k(p, a, S) = C(a) + Σ_o P(o | p, a) · V_{k−1}(p^{a,o}, S′(a))

V_k(p, S) = min( R(p),  min_{a ∈ S} Q_k(p, a, S) )
```

S′(a) is the eligible set **re-evaluated as if a had run**. It removes a itself and the other
members of a's `exclusive_group`. It decrements the active-probe budget, and the MEDIUM+ budget
when tier(a) ≥ MEDIUM, then removes any source those budgets now exclude. Every §8 guardrail is
re-applied inside the recursion. The guardrails that are fixed for the whole session (consent,
language, the fairness cap) do not change.

The planner computes, for every eligible a ∈ S:

```
net_value(a) = R(p) − Q_k(p, a, S)
```

**Selection.** a* = argmax_a net_value(a). Two values within 1 paise of each other count as a
tie. Ties are broken by lower friction tier, then lower cost, then `source_key` in alphabetical
order.

Exact ties are normal, not an edge case. When two probes both belong to the optimal k-step
plan, their Q values are equal, because observation order does not matter. The tie-break then
runs the cheaper, less intrusive one first.

**Stopping.** The planner returns `STOP` when any of these holds:

1. `net_value(a*) ≤ stop_epsilon_paise` (default 100 paise = ₹1).
2. S is empty after guardrails (§8).
3. A budget (§8) is exhausted.

On `STOP`, the session or case takes d*(p).

**Depth.** Guardian: k = min(`lookahead_depth.guardian` (3), remaining active-probe budget).
Navigator: k = `lookahead_depth.navigator` (2); evidence has no budget. Both are configured in
the KB `engine` section.

**Performance.** The planner MUST memoise V on (rounded belief to 1e-9, frozenset S, depth). It
MUST vectorise the inner sums over hypotheses with numpy. Target: p95 ≤ 50 ms per planning
call with |S| ≤ 8, max |O| = 4, k = 3, |H| ≤ 8 on one core (NFR-03).

## 7. Costs

```
Guardian:   C(a) = friction_cost_inr[tier(a)] · 100  +  expected_latency_s(a) · delay_cost_inr_per_s · 100
Navigator:  C(a) = expected_minutes(a) · investigator_cost_inr_per_min · 100
```

The KB v1 values are in doc 07 §7. Costs do not depend on the hypothesis.

## 8. Guardrails and eligibility (applied before planning)

A source is **eligible** only if all of these are true:

| Rule | Guardian | Navigator |
|---|---|---|
| Source is `enabled` in the KB version | ✔ | ✔ |
| Source applies to the context | ✔ | ✔ |
| Source not already observed in this session or case (each source runs at most once) | ✔ | ✔ |
| Required consent present (e.g. `CALL_STATUS` needs `consent.call_status = true`) | ✔ | — |
| Language available for question probes (approved variant in the customer's language, or the English fallback) | ✔ | — |
| Active-probe budget: active probes run < `max_active_probes_per_session` (3) | ✔ | — |
| Exclusive group: no other source of the same `exclusive_group` has run in this session. KB v1 puts `OTP_STEP_UP` and `BIOMETRIC_STEP_UP` in group `STEP_UP_AUTH`: a customer is never asked for two authentication challenges | ✔ | — |
| Friction budget: probes of tier `MEDIUM` or higher run < `max_medium_plus_per_session` (2) | ✔ | — |
| Customer fairness cap: fewer than `max_active_sessions_per_customer_30d` (3) earlier sessions with ≥ 1 active probe in the rolling 30 days. If exceeded, **all** active probes are ineligible | ✔ | — |
| Passive-first ordering: active probes are only eligible in phase B (§9) | ✔ | — |

Each ineligible source is recorded in the candidates table with its `ineligible_reason`, so the
DD pack can explain why a probe was not used.

## 9. Two-phase Guardian procedure

**Phase A — passive.** Run every eligible passive probe (friction `NONE`) in parallel, each with
an adapter timeout (doc 08). Update the belief with each outcome, in KB order, for determinism.
For every passive probe, compute and log VoI_1 and net_value against the prior, for analytics.
Phase A does not use them to skip probes. Rationale: passive probes cost milliseconds and no
friction, and running them all keeps behaviour predictable (ADR-0005).

**Phase B — active.** Loop: plan → if `STOP`, decide → else issue the chosen active probe, wait
for the response or timeout, classify the outcome, update, repeat.

The Navigator has no phases. Every evidence item is planned through §6.

## 10. Exploration (Guardian phase B only)

The purpose is to log propensities so other probe policies can be evaluated off-policy (doc 12)
**without ever letting suspected fraud through**.

```
Let a* be the greedy choice (not STOP).
E = { a ∈ eligible : net_value(a) ≥ 0  and  tier(a) ≤ tier(a*) }      # a* ∈ E always
With probability ε (default 0.05): choose a uniformly from E          → selected_by = EXPLORE
Otherwise: choose a*                                                   → selected_by = GREEDY
propensity(a_chosen) = (1 − ε) · 1[a_chosen = a*] + ε / |E|
```

Rules:

- No exploration when the greedy result is `STOP`. Exploration never adds a probe the planner
  would not run, and never adds more friction than the greedy choice.
- Exploration only changes **which** probe runs next. It never changes the decision rule.
- RNG seed per session:
  `seed = int.from_bytes(sha256(f"{payment_id}:{kb.engine.exploration_seed}").digest()[:8], "big") >> 1`.
  It depends on `payment_id`, not the session id. Simulator-generated payment ids are
  deterministic (doc 14 §1), so replays and evaluation reruns are exact.
- If |E| = 1, then propensity = 1.0.

## 11. Prior construction (interface with sensing)

The sensing layer (doc 13) returns:

- `p_model(h)` for the hypotheses the sensing model was trained on (`model_hypotheses`);
- `q ∈ [0, 1]`, the anomaly percentile.

The engine builds the prior over the applicable H:

```
a_open = max(0, (q − 0.90) / 0.10)                       # 0 below the 90th percentile
u      = open_mass_base + open_mass_slope · a_open      # KB v1: 0.02 + 0.18 · a_open

for h in model_hypotheses ∩ H:    p0(h) = (1 − u) · p_model(h) / Σ_{h' ∈ model_hypotheses ∩ H} p_model(h')
for h in kb_only_hypotheses ∩ H:  p0(h) = u · w_h            # w_h = typology.open_mass_share
p0(UNEXPLAINED) = u · (1 − Σ w_h)
```

**Membership is derived, never configured:**

- `model_hypotheses` = the classes of the ACTIVE `PRIOR` model artifact ∩ H.
- `kb_only_hypotheses` = the typologies in H that are not in the artifact's classes. For
  example, a newly promoted typology before the next retrain.

Σ w_h over the kb-only hypotheses must be ≤ 0.9. This is checked when a KB version is promoted
and when an artifact is activated; a violation is refused with `KB_VALIDATION_FAILED`. Because
membership comes from the artifact, a retrained model that adds a class takes that class out of
the open-mass split as soon as it is activated, so no hypothesis is ever counted twice. For the
`CASE_ACCOUNT_REVIEW` profile, `p_model` comes from the mule scorer:
p_model(MULE) = p_mule and p_model(GENUINE) = 1 − p_mule.

All prior components (`p_model`, `q`, `u`, `w`) are persisted with the session, so backtests can
rebuild the prior under a different KB.

## 12. "What would change my mind" (Navigator)

Given the current belief p and the recommended decision d*(p):

```
For each eligible-but-unobserved evidence item a and each outcome o with P(o | p, a) ≥ 0.05:
    if d*(p^{a,o}) ≠ d*(p): it is a flip candidate, with probability P(o | p, a)
Return the flip candidate with the highest P(o | p, a)
  (ties: higher |risk change|, then source_key alphabetical)
If none: "No single remaining evidence item would change the recommendation."
```

## 13. Uncertainty-settled percentage (Navigator display)

```
entropy H(p) = −Σ p log2 p
settle%(a)   = 100 · ( H(p) − Σ_o P(o) · H(p^{a,o}) ) / H(p)       # 0 if H(p) = 0
```

This is display-only text ("expected to settle 58% of remaining uncertainty"). Ranking always
uses net_value.

## 14. Information yield (metrics)

```
info_yield_bits = Σ_h p'(h) · log2( p'(h) / p(h) )      # KL(p' ‖ p)
true_gain       = log2 p'(h_true) − log2 p(h_true)       # only once a label exists
```

## 15. Engine output contract

`plan_next(state, kb, rng) → PlanResult`:

```
PlanResult:
  action: SourceKey | "STOP"
  stop_reason: "NO_POSITIVE_VALUE" | "NO_ELIGIBLE_SOURCES" | "BUDGET_EXHAUSTED" | None
                # NO_POSITIVE_VALUE → terminal_reason STOP; the other two → BUDGET_EXHAUSTED
  selected_by: "GREEDY" | "EXPLORE" | None
  propensity: float | None
  bayes_risk_paise: float
  recommended_decision: DecisionKey
  risk_by_decision: {DecisionKey: float}
  candidates: [ {source_key, eligible, ineligible_reason, cost_paise,
                 voi_myopic_paise, net_value_paise, settle_pct} ]   # sorted by net_value desc
  lookahead_depth: int
```

`update(belief, source, outcome) → Belief` and `decide(belief, profile, amount, vulnerable) →
(decision, risk_by_decision)` are also public. Nothing else is.

## 16. Golden test — research worked example (MUST pass exactly)

This fixture reproduces research §9.3. It uses `tests/fixtures/kb_research_example.yaml`:

- H = (`GENUINE`, `APP_DIGITAL_ARREST`, `ATO`). No `UNEXPLAINED` in this fixture.
- Prior (0.55, 0.35, 0.10).
- Profile `GUARDIAN_PAYMENT`, losses from KB v1 (doc 07 §8), A = ₹4,80,000, not vulnerable.

| Fixture source | Outcomes | L(·\|GENUINE) | L(·\|APP_DA) | L(·\|ATO) |
|---|---|---|---|---|
| `OTP_STEP_UP` | PASSED / FAILED_OR_ABANDONED | 0.98 / 0.02 | 0.97 / 0.03 | 0.30 / 0.70 |
| `CALL_STATUS_BINARY` | ON_CALL_UNKNOWN_NUMBER / OTHER | 0.08 / 0.92 | 0.85 / 0.15 | 0.10 / 0.90 |
| `SPELL_BREAKER_DIGITAL_ARREST_BINARY` | HESITATION_OR_APP_SWITCH / OTHER | 0.05 / 0.95 | 0.60 / 0.40 | 0.20 / 0.80 |

Expected results (tolerance: probabilities ±1e-3, money ±₹0.10):

| Check | Expected |
|---|---|
| Posterior after OTP = PASSED | (0.593, 0.374, 0.033) |
| Posterior after CALL_STATUS_BINARY = ON_CALL_UNKNOWN_NUMBER | (0.125, 0.846, 0.028) |
| …then SPELL_BREAKER = HESITATION_OR_APP_SWITCH | (0.012, 0.977, 0.011) |
| R(prior), d*(prior) | ₹14,470.00, `PROTECTIVE_HOLD` |
| VoI_1(`OTP_STEP_UP`) at prior | **₹0.00** (OTP cannot change the decision) |
| VoI_1(`CALL_STATUS_BINARY`) at prior | **₹1,685.80** |

The loss values used (₹, A = 4,80,000):

| Decision \ h | GENUINE | APP_DIGITAL_ARREST | ATO |
|---|---|---|---|
| `RELEASE` | 0 | 4,56,000 | 4,32,000 |
| `HOLD_FOR_REVIEW` | 15,400 | 38,600 | 5,000 |
| `PROTECTIVE_HOLD` | 22,200 | 5,000 | 5,100 |
| `DECLINE` | 26,000 | 96,050 | 50 |

This table is the slide-ready proof that "DELPHI knows *before running it* that OTP has zero
information value against coercion" (research §9.3).

## 17. Spec verification note (KB v1 behaviour, checked 2026-10-31)

Before the spec was finalised, a throwaway script (not part of the repo) ran the §6 planner
with KB v1 and the losses in doc 07 §8. The demo stories rely on these behaviours, and the M1
and M4 tests must reproduce them:

These priors are fixtures (`prior_override`) for the ENG-S1/ENG-S2 tests (doc 21 §3). They
respect u ≥ 0.02. The live demo uses the trained model's prior instead, and the E2E tests check
the same story outcomes there.

- **Arjun** (prior G .69 / ATO .20 / DA .04 / INV .03 / MULE .02 / ∅ .02, ₹1,80,000, passive:
  CLEAN_KNOWN_ENTITY, LOW_OR_UNKNOWN, NEW_DEVICE, NORMAL, NOT_ON_CALL).
  - The posterior after the passive probes is G ≈ .943, ATO ≈ .050.
  - Without the `STEP_UP_AUTH` exclusive group, the depth-3 planner runs **both** OTP and
    biometric, because each is cheap relative to the residual ATO risk.
  - With the group, `BIOMETRIC_STEP_UP` and `PURPOSE_QUESTION` tie at net ≈ ₹4,656 (both are in
    the optimal plan), and biometric wins on cost (₹24 vs ₹26). Both run, then the planner
    decides `RELEASE`, with risk ≈ ₹886 vs ≈ ₹6,374 for HOLD_FOR_REVIEW.
- **Kamala** (prior G .54 / ATO .08 / DA .30 / INV .05 / MULE .01 / ∅ .02, ₹4,80,000).
  - With hesitant session behaviour *and* on a call with an unknown number, the passive probes
    alone give DA ≈ .84. The planner then correctly runs no active probe and decides
    `PROTECTIVE_HOLD`.
  - With the S-1 persona's normal typing, DA ≈ .38 after the passive probes. The planner asks
    `PURPOSE_QUESTION` (net ≈ ₹1,883 vs ₹1,836 for the spell-breaker).
  - The coached answer moves G back to ≈ .75, so the planner asks
    `SPELL_BREAKER_DIGITAL_ARREST`. Hesitation moves DA to ≈ .73, which gives
    `PROTECTIVE_HOLD`.
  - This is why S-1 uses a coached persona with normal typing.
- **Genuine elderly mirror case** (same amount, on a call with a known contact): about three
  low/medium-friction questions, then `RELEASE`. This is friction on a ₹4.8 lakh payment, but
  far less than a hold. Report it in the evaluation as "active probes per genuine payment".
