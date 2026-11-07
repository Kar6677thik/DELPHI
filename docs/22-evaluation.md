# 22 — Evaluation: baselines, metrics, protocol

Research §11 and §12 ("results table"). Module `delphi.evaluation`. This produces demo story S-5
and the numbers for the pitch.

## 1. Protocol

1. Generate the `eval` profile (seed 7): 20,000 customers, 150 days of history under the legacy
   policy, and a 30-day evaluation stream. The dataset is held in memory while it is generated
   and written, with a peak RSS budget of < 4.5 GiB (measured 3.77 GiB, #58). That fits the
   worker's 5 Gi limit (doc 23 §4). If a change pushes it over, derive `ledger_entries` from
   the payments at write time first.
2. Train sensing on the history labels observed at day 150, over the matured window (doc 13 §4).
3. Replay the **identical** evaluation stream through each policy with a `VirtualClock`
   (discrete events, no waiting). Each policy gets its own copy of the bank state, because
   decisions change balances and downstream behaviour.
4. Customers answer probes from the response models (doc 14 §5), with RNG streams keyed by
   (payment id, probe key), so the same customer gives the same answer to the same probe under
   every policy (common random numbers, which reduces variance).
5. Held items go to a simulated investigation queue (§3).
6. Labels arrive according to doc 14 §7.
7. Compute the metrics (§4) from `sim_truth` (evaluation is allowed to read it), with 95%
   cluster-bootstrap CIs, resampling **customers** 1,000 times.
8. Persist to `evaluations.metrics`. Render the report page and export
   `docs/reports/evaluation-<date>.md` (committed by the owner).

Runtime target: ≤ 30 minutes for `eval` on the home server (4 cores), ≤ 2 minutes for `test`.
Engine work is parallelised across customers with `multiprocessing` (4 workers). Results must
not depend on the number of workers (per-customer RNG streams).

## 2. Policies

| ID | Name | Rule |
|---|---|---|
| **B1** | Static rules (legacy) | DECLINE if the payee is LISTED_SUSPECT. HOLD (→ investigation) if amount ≥ ₹50,000 and the payee is new; or the device is new and amount ≥ ₹10,000; or channel `UPI_COLLECT_APPROVAL` and amount ≥ ₹25,000. Else RELEASE |
| **B2** | Score + fixed friction | r = 1 − p0(GENUINE) from the same sensing prior. r < 0.20 → RELEASE; 0.20 ≤ r < 0.60 → OTP_STEP_UP, then RELEASE if PASSED, else HOLD; r ≥ 0.60 → HOLD |
| **DELPHI** | This system | KB v1 (fixed during the run; no learning promotion inside the run, so the comparison stays fair) |
| DELPHI-myopic (ablation) | Myopic VoI selection (k = 1) | Shows why lookahead matters (ADR-0005) |
| DELPHI-no-active (ablation) | Passive probes only | Shows the value of active probes |

Wherever this table says HOLD, the decision recorded is `HOLD_FOR_REVIEW`. B1's DECLINE is
recorded as `DECLINE`.

## 3. Simulated investigation queue (same capacity for every policy)

- 2 investigators × 8 h/day (09:00–17:00 IST), FIFO for B1/B2; DELPHI orders by
  `sla_due_at`, then expected loss.
- B1/B2 handling: a fixed checklist of all 9 evidence items (Σ 13.5 min) + 3 min to dispose.
  The outcomes are computed the same way, and the disposition is the Bayes decision under KB v1
  with all evidence (an idealised investigator).
- DELPHI handling: follow the top-ranked evidence until the planner says STOP, + 3 min to
  dispose. Accept the recommendation with probability 0.95, otherwise take the other decision
  (models human disagreement).
- `CUSTOMER_OUTREACH_CALL` answers come from the response model. They yield `OUTREACH_CONFIRMED`
  labels in every policy that runs it.

## 4. Metrics (definitions are normative)

Let F be the fraud payments in the eval stream (sim truth; the `UPI_COLLECT_REFUND` scenario,
ground truth `APP_COLLECT_REFUND`, counts as fraud), G the genuine ones. "Stopped" = final payment status ≠ EXECUTED.

| Metric | Definition |
|---|---|
| Fraud ₹ prevented | Σ amount over F that were stopped |
| Fraud recall (count / ₹) | stopped F ÷ F (by count and by ₹) |
| **Coercion catch rate** | stopped ÷ total over APP_DIGITAL_ARREST + APP_INVESTMENT payments **whose OTP would pass** (determined from the response model, per payment) |
| **Genuine-hold rate** | G with any hold/decline ÷ G |
| Genuine-hold rate (vulnerable) | the same, restricted to vulnerable customers (fairness) |
| **Median hold resolution time** | median over held payments of (final disposition time − hold time). Guardian in-session resolution counts as 0 holds |
| p90 hold resolution time | |
| Friction cost per fraud ₹ prevented | Σ probe friction costs (₹, doc 07 §7) over all payments ÷ fraud ₹ prevented |
| Active probes per genuine payment | mean count |
| **Label coverage (held)** | doc 12 §4, within 7 days |
| **Information yield per probe** | mean info_yield_bits per probe key (DELPHI only) |
| **Uncertainty reduced per investigator minute** | Σ entropy reduction from evidence ÷ Σ evidence minutes |
| Due-diligence completeness | share of DELPHI decisions with `dd_pack_complete` |
| Golden-hour recovery ratio, tracing delay | Chase run on every *executed* fraud with a complaint, with automatic approval of recommended liens after 2 sim-minutes (DELPHI) vs a manual baseline: liens on first-hop accounts only, for the full reported amount, after 30 sim-minutes |
| Wrongful liens | liens on accounts with `is_mule = false` |
| Typology attribution accuracy | argmax hypothesis = true hypothesis, among stopped F (KB v1 hypotheses only) |

## 5. Pre-registered targets (DELPHI vs best baseline)

| Target | Threshold |
|---|---|
| Genuine-hold rate | ≥ 50% lower than B1, at fraud ₹ prevented ≥ B1 |
| Coercion catch rate | ≥ 2× B2 |
| Median hold resolution time | ≤ 1/10 of B1 |
| Label coverage (held) | ≥ 80% (vs B1 expected ≤ 30%) |
| Golden-hour recovery ratio | ≥ 2× the manual baseline |
| Due-diligence completeness | 100% |

Targets are hypotheses, not guarantees. The report MUST show the actual values, CIs, and a plain
statement for every target that is missed. KB v1 numbers MUST NOT be tuned on the evaluation
stream. Tuning is only allowed on a separate `eval` seed (8), and every change must be logged.

## 6. Report layout (`/evaluation` and the markdown export)

1. Headline results table: rows = metrics, columns = B1, B2, DELPHI (value [95% CI]); the
   ablations in a secondary table.
2. Charts: genuine-hold rate vs fraud recall (one point per policy), hold resolution time
   distribution, information yield per probe (bar), label coverage.
3. Caveats box: synthetic data; oversampled fraud; the response models are perturbations of KB
   v1 (so the KB is "roughly right" by construction, and a real deployment must elicit); naive
   Bayes assumption.
4. Reproducibility block: dataset id, generator version, seeds, KB version + hash, git SHA.

## 7. Live KPIs (`GET /metrics/summary`, Overview page)

The same definitions computed over live demo data where truth is not available: decisions by
type, median Guardian session duration, hold resolution time, active probes per session,
information yield per probe, open cases and SLA breaches, label coverage (observed labels),
golden-hour ratio of the last chase, DD-pack completeness.
