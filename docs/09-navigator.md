# 09 — Navigator loop (investigation time)

Research §9.5. Module `delphi.navigator`. Decision profiles `CASE_PAYMENT_HOLD` and
`CASE_ACCOUNT_REVIEW`.

## 1. How cases are created

| Trigger | Case type | Initial posterior | Protocol |
|---|---|---|---|
| Guardian decides `HOLD_FOR_REVIEW` | PAYMENT_HOLD | Guardian final posterior | STANDARD |
| Guardian decides `PROTECTIVE_HOLD` | PAYMENT_HOLD | Guardian final posterior | COERCION |
| Mule-scorer sweep: an account in the home bank with p_mule ≥ 0.70 and credits in the last 24 h, run as a worker job every 15 minutes (scaled in demo) | ACCOUNT_REVIEW | prior from the mule scorer (doc 06 §11) | STANDARD |
| A complaint names a home-bank counterparty account with no open review | ACCOUNT_REVIEW | mule-scorer prior, updated with `LINKED_COMPLAINTS = ONE_OR_MORE` | STANDARD |
| Manual: `POST /cases` by an INVESTIGATOR | ACCOUNT_REVIEW only (PAYMENT_HOLD cases come only from Guardian holds) | mule-scorer prior | STANDARD |

Guardian probe outcomes carry into a `PAYMENT_HOLD` case as already-observed sources. They are
shown in the case timeline and are not re-planned.

## 2. Case console behaviour (per refresh / after each evidence fetch)

1. **Hypothesis bar** (current posterior) and the recommended decision with `risk_by_decision`.
2. **Next best evidence**: every eligible, unobserved evidence item, ranked by `net_value`
   (doc 06 §6, depth 2, investigator-minute cost). Each row shows the expected minutes,
   "expected to settle X% of remaining uncertainty" (doc 06 §13) and the net value in ₹.
   In the `COERCION` protocol, `CUSTOMER_OUTREACH_CALL` is pinned first with a "Protocol" badge
   until it has been done.
3. **Evidence sufficient banner**: when the planner returns STOP, show "Evidence sufficient —
   recommend {decision} (expected loss ₹X)". Fetching more is still allowed.
4. **What would change my mind** (doc 06 §12).
5. **Evidence cards**: one per fetched record, showing the outcome chip, the posterior shift
   (before → after for the top two hypotheses), the deterministic fact table, and the LLM
   narrative with citations (or the template narrative), labelled "AI-written summary" or
   "Template summary".
6. **Disposition panel**: decision buttons for the profile, the typology selector (for
   CONFIRM_FRAUD), a free-text note, and a self-reported confidence of 1–5. An override
   requires a rationale (doc 05 §4).

## 3. Evidence fetch flow

```
POST /cases/{id}/evidence-requests {source_key}
  → idempotent per (case, source_key) unless refresh=true
  → adapter (SQL over the sim bank, or SimOutreachCall) → raw facts + record ids
  → classifier → outcome → engine.update → persist EvidenceRecord (posterior before/after,
     rank and net value at request time, requested_by, fetch_ms)
  → enqueue LLM_NARRATIVE job (doc 15) → SSE evidence.narrative_ready
  → return the updated case view
```

`CUSTOMER_OUTREACH_CALL` is special. In the demo UI it opens a call script dialog. The
investigator records the result by choosing one outcome (`CONFIRMS_COERCION`, …), and its
`raw` holds the note. In simulation/autoplay, the api enqueues a `SIM_OUTREACH_CALL` job. The
worker, which may read scenario truth, samples the outcome from the response model and records
it through the same service. The UI receives it over SSE. `CONFIRMS_COERCION` and `DENIES_INITIATING` create a label with source
`OUTREACH_CONFIRMED` (the hypothesis is the case's leading fraud hypothesis consistent with the
outcome: coercion → leading APP_* hypothesis; denies → ATO).

## 4. Evidence outcome rules (deterministic)

Windows are relative to the case's `as_of` time (the time of the held payment, or of case
opening for an account review).

| Source | Rule |
|---|---|
| `BENEFICIARY_DEVICE_SHARING` | n = number of *other* accounts whose owners have logged in from any device used by the subject account's owner in the last 30 days. n ≥ 3 → SHARED_3_PLUS; 1–2 → SHARED_1_2; 0 → NOT_SHARED |
| `INBOUND_FAN_IN` | m = distinct payers into the subject account in the last 48 h that have no prior relation to it. m ≥ 10 → HIGH; 3–9 → MODERATE; else LOW |
| `PASS_THROUGH_DWELL` | median, over credits in the last 7 days, of the time until 80% of that credit's amount has left the account (FIFO). < 30 min → UNDER_30_MIN; ≤ 24 h → BETWEEN_30_MIN_24_H; else OVER_24_H (also OVER_24_H if there are no credits) |
| `ACCOUNT_PROFILE` | age < 90 days and kyc MINIMAL → NEW_MINIMAL_KYC; age < 90 days → NEW_FULL_KYC; else ESTABLISHED |
| `LINKED_COMPLAINTS` | ≥ 1 complaint naming the subject account, known as of now → ONE_OR_MORE; else NONE |
| `CRM_CONTACT_HISTORY` | SIM_SWAP_REPORTED or DEVICE_LOST_REPORTED for the payer in the last 14 days → SIM_SWAP_OR_DEVICE_LOSS_REPORTED; else NONE |
| `LOGIN_GEO_VELOCITY` | two logins in the last 24 h implying a speed > 500 km/h → IMPOSSIBLE_TRAVEL; the last login city never seen before → NEW_LOCATION; else USUAL |
| `PAYEE_RELATIONSHIP` | no earlier executed payment payer→payee → NO_PRIOR_RELATION; earlier payments all < ₹5,000 → PRIOR_SMALL_PAYMENTS; else ESTABLISHED_PAYEE |
| `CUSTOMER_OUTREACH_CALL` | the investigator's recorded result (demo) or a response-model sample (simulation) |

Every adapter returns the facts used (counts, timestamps, record ids). These are the only
material the LLM narrative may cite.

## 5. Investigator traces

Every interaction appends a `trace_events` row: `CASE_OPENED`, `CASE_VIEWED`,
`EVIDENCE_REQUESTED` (source, rank shown, net value shown, settle % shown), `EVIDENCE_VIEWED`,
`NARRATIVE_EXPANDED`, `WCMM_VIEWED`, `DISPOSITION` (decision, typology, confidence, override,
time since opened), `DD_PACK_VIEWED`. The frontend sends view events through
`POST /cases/{id}/trace-events` (batched, at most 1 request per 5 s).

Trace analytics shown on the Learning page (doc 12 §6):

- **Agreement rate**: share of evidence requests where the investigator chose the top-ranked
  item.
- **Order heatmap**: source × position at which it was requested.
- **Stalls**: cases with a gap > 10 minutes (real time) between consecutive actions, and median
  time-to-disposition by case type.
- **Uncertainty reduced per investigator minute**: Σ entropy reduction ÷ Σ evidence minutes.

## 6. Coercion protocol extras

When protocol = `COERCION`:

1. `CUSTOMER_OUTREACH_CALL` is pinned first.
2. A **police alert draft** (template: victim pseudonym, amount, beneficiary, time, evidence
   list) is created with status `DRAFT`. An INVESTIGATOR can `SEND` it, which in the sim is
   recorded as `SENT_SIMULATED`, or `DISCARD` it. Nothing is sent automatically (FR-NAV-10).
3. The SLA is 30 minutes (configurable).

## 7. Due-diligence pack

`GET /cases/{id}/dd-pack?format=json|html` (also `/guardian/sessions/{id}/dd-pack`). Sections,
all required for "complete":

1. Header: ids, subject, amount, customer pseudonym, vulnerability flag, KB version and its
   content hash.
2. Prior: p_model top 3 hypotheses, the top 5 feature contributions (doc 13 §1), anomaly
   percentile, open mass.
3. Timeline of every observation: source, why it was chosen (net value, the alternatives
   considered with their values, GREEDY/EXPLORE and propensity), raw facts, outcome,
   posterior before → after.
4. Guardrails applied (ineligible sources and reasons).
5. Decisions: the engine recommendation with its risk table; human disposition with user,
   time, confidence and any override rationale.
6. Narratives (labelled AI or template) with resolved citations.
7. Liens and tickets (if any) with recorded reasons.
8. The audit hash-chain verification result for all entries of this case.
9. Method note: "Bayesian decision engine; naive-Bayes conditional-independence assumption;
   likelihoods from KB version N".

**Completeness by subject type.** `dd_pack_complete` is true when every *required* section
is non-empty. "n/a" sections are rendered as "Not applicable" and do not count.

| Section | Guardian session (fast path) | Guardian session | Case |
|---|---|---|---|
| 1 Header | required | required | required |
| 2 Prior | required | required | required |
| 3 Observation timeline | n/a ("fast path: no observations") | required | required (including inherited Guardian observations) |
| 4 Guardrails | n/a | required (may state "none applied") | required for Guardian-originated observations, else n/a |
| 5 Decisions | engine part required | engine part required | engine part **and** human disposition required |
| 6 Narratives | n/a | n/a | required: every evidence record has a narrative (LLM or template) |
| 7 Liens/tickets | n/a | n/a | required if a chase exists for the subject, else n/a |
| 8 Audit verification | required | required | required |
| 9 Method note | required | required | required |

The flag is stored on `guardian_sessions.dd_pack_complete` and `cases.dd_pack_complete`. It is
recomputed on every change, and it feeds the "due-diligence completeness" metric. An open case
is never complete: section 5 needs a disposition.

## 8. Campaigns

Rules are in doc 05 §5. The campaign list is sorted by `rank_score` and shows the
F3 stage strip, case count, total expected loss, and victims who are vulnerable. Campaign
detail lists the cases and the shared infrastructure entities.
