# 12 — Learning loop

Research §9.8, §10 (L5). Module `delphi.learning`.

## 1. What learns and what does not

| Learns | How |
|---|---|
| Likelihood rows (Dirichlet α) | Bayesian count updates from labelled sessions and cases (§2) |
| Sensing models | Periodic retrain on available labels (doc 13 §7) |
| The hypothesis set | Scout + FDE typology lifecycle (doc 11) |

| Does not learn automatically | Why |
|---|---|
| Loss tables, costs, guardrails | Business and regulatory policy: they change only by human edit in a new KB version |
| Probe catalogue | Probes are code |

## 2. Likelihood update (`RUN_LEARNING` job)

**Input:** all sessions and cases with `kb_version_id` in the lineage of the current ACTIVE
version that (a) have a label known at run time, and (b) are not already counted (tracked in
`learning_contributions`, keyed by subject id + source key, so the job is idempotent).

For each labelled subject with hypothesis h* (`UNEXPLAINED` is never a label) and each completed
observation (source a, outcome o) on it, where a has a row for h*:

```
α_new[a][h*][o] = decay · α_old[a][h*][o] + w
w = 1.0  (all label sources; configurable per label_source)
decay = 1.0 by default (no forgetting); 0.98 available for drift experiments
```

Then rescale: if Σ α_new[a][h*] > max_ess (500), multiply the row by 500 / Σ. The floor
α ≥ 0.01 always applies.

**Output:** a new KB version with status `CANDIDATE`, `change_summary` = "Learning run {id}:
{n} labelled subjects, {m} observations", and an attached **diff report**:

- per (source, hypothesis) row: old mean, new mean, KL(new ‖ old) in bits, observations added;
- the 20 rows that changed most;
- **held-out log loss**: the last 20% of labelled subjects by time are excluded from the update
  and scored. Score = mean −log P(h* | observations) under parent vs candidate, both using the
  stored prior components. Lower is better.

**Promotion:** manual, FDE_ENGINEER/ADMIN. CANDIDATE → ACTIVE directly is allowed for learning
runs; shadow is optional. Guard: promotion is blocked if the held-out log loss is worse than the
parent's by more than 0.02 nats, unless the promoter adds an override reason.

**Selection-bias note (shown in the diff report):** likelihoods are conditional on h, so the
probe-selection policy does not bias them. *Which* subjects get labels does depend on the
decisions taken. Label coverage on held decisions (§4) is what keeps that bias small.

## 3. Exploration logs and off-policy evaluation (OPE)

Every Guardian phase-B probe run logs `selected_by`, `propensity` and the eligible set `E`
(doc 06 §10).

OPE scope: the **first active probe** of each session (a contextual-bandit view).

```
context x_i: the session state just before the first active probe (belief, eligible set)
action a_i: the probe chosen; propensity π0(a_i | x_i)
reward r_i: true_gain of that probe = log2 p'(h_true) − log2 p(h_true)   (labelled sessions only)
target policies π:
  deterministic: "greedy under candidate KB", "myopic VoI greedy", "OTP-first (if eligible) else greedy"
  stochastic:    "uniform over E"  (π(a|x) = 1/|E_i|)
w_i = π(a_i | x_i) / π0(a_i | x_i)
SNIPS estimate V̂(π) = Σ w_i r_i / Σ w_i
effective sample size = (Σ w_i)² / Σ w_i²
```

**Support.** π0 is positive only on the logged exploration set E_i. A deterministic target
whose choice lies outside E_i has no support in that session: the session gets w_i = 0 and
counts in `support_gap`. The report shows `support_coverage` = 1 − support_gap / n. If
coverage < 50%, or Σ w_i = 0, the estimate is shown as "unsupported by logged data" instead of a
number. "OTP-first" will often be unsupported, because OTP is rarely in E. The report says so
rather than hiding it.

The Learning page reports V̂ for the logged policy (on-policy mean) and for each target policy,
with ESS and a 95% bootstrap CI (1,000 resamples). An ESS < 30 is shown as "insufficient data".

## 4. Label coverage (selective-labels fix)

```
label_coverage(held) = # sessions decided HOLD_FOR_REVIEW / PROTECTIVE_HOLD / DECLINE that have
                       a label known within 7 days ÷ # such sessions
```

Labels on held items come from `OUTREACH_CONFIRMED` and `INVESTIGATOR_DISPOSITION` (DELPHI
resolves holds), versus complaint-only labelling in the baselines. Reported per policy in the
evaluation (doc 22).

## 5. Probe yield drift ("coaching adaptation" detector)

For each source, daily mean `info_yield_bits` over completed observations. Alert
`PROBE_YIELD_DEGRADED` when the rolling 7-day mean < 0.7 × the rolling previous-28-day mean and
the 7-day count ≥ 30. Alerts appear on the Learning page and in the metrics endpoint
(`delphi_probe_yield_degraded{source}`). The FDE response (rotate variants, re-elicit) is manual.
The simulator can inject coaching adaptation (doc 14 §5.3), so the detector can be demonstrated.

## 6. Investigator trace analytics

Doc 09 §5 metrics are computed by the `RUN_LEARNING` job and on page load (cached 60 s).
**Imitation learning from traces is out of scope** for the PoC (doc 01 §4). The traces are
stored in a form that supports it later (ordered events with the shown ranks).
