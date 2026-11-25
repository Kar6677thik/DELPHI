# 09 — Navigator loop (investigation time)

Research §9.5. Module `delphi.navigator`. Decision profiles `CASE_PAYMENT_HOLD` and
`CASE_ACCOUNT_REVIEW`.

## 1. How cases are created

| Trigger | Case type | Initial posterior | Protocol |
|---|---|---|---|
| Guardian decides `HOLD_FOR_REVIEW` | PAYMENT_HOLD | Guardian final posterior | STANDARD |
| Guardian decides `PROTECTIVE_HOLD` | PAYMENT_HOLD | Guardian final posterior | COERCION |
| Mule-scorer sweep: an account in the home bank with p_mule ≥ 0.70 and credits in the last 24 h (`mule_credit_window_h`), checked by the `ACCOUNT_REVIEW_SWEEP` worker job every 60 s | ACCOUNT_REVIEW | prior from the mule scorer (doc 06 §11) | STANDARD |
| A complaint names a home-bank counterparty account with no open review | ACCOUNT_REVIEW | mule-scorer prior, updated with `LINKED_COMPLAINTS = ONE_OR_MORE` | STANDARD |
| Manual: `POST /cases` by an INVESTIGATOR | ACCOUNT_REVIEW only (PAYMENT_HOLD cases come only from Guardian holds) | mule-scorer prior | STANDARD |

Guardian probe outcomes carry into a `PAYMENT_HOLD` case as already-observed sources. They are
shown in the case timeline and are not re-planned.

**Implementation (M4 stub, #97).** `delphi.navigator.api.open_case` opens the PAYMENT_HOLD cases
of the first two rows: HOLD_FOR_REVIEW → STANDARD with `hold_review_sla_minutes`, PROTECTIVE_HOLD
→ COERCION with `protective_sla_minutes`, both from the KB version the session was pinned to
(scaled by that version's `demo_time_scale` in demo time, doc 07). The initial posterior is the
session's final posterior, and the leading hypothesis is its argmax (ties in KB order). Opening
writes the `case.opened` audit entry and publishes `case.opened` (doc 16 §3) in the caller's
transaction. Guardian passes plain values, so the Navigator never imports Guardian; Guardian links
the session to the case (#94). A retried call for the same session returns the case it opened;
an open case from another session is refused (`409 INVALID_STATE_TRANSITION`). The account-review
triggers and the console come with M5 (#103, #109).

**Implementation (complaints trigger, #109).** `ACCOUNT_REVIEW_SWEEP` (every minute, doc 04 §5.4) runs
`delphi.navigator.reviews.open_complaint_reviews` under the ACTIVE KB:
- It finds the home-bank (`DLPH`) accounts named in `counterparty_account_ids` by complaints
  reported in the last `DELPHI_REVIEW_COMPLAINT_LOOKBACK_DAYS` (30; an older complaint
  opens no review), in SQL, oldest complaint first, with `NOT EXISTS` against the account's
  reviews and a `LIMIT`.
- It is bounded, as `LABEL_DELIVERY` is (#263): one transaction per batch of
  `DELPHI_REVIEW_SWEEP_BATCH` reviews (25), each under a 30 s statement timeout and 16 MB
  `work_mem`, and at most `DELPHI_REVIEW_SWEEP_MAX_PER_RUN` reviews (100) per run; the next run
  carries on. A run that finds another one sweeping (advisory lock 50) skips.
- It opens an ACCOUNT_REVIEW case (STANDARD, `account_review_sla_minutes`) for each, unless the
  account has an open review or a review opened after its latest complaint. So it is idempotent
  per account, and a disposed review reopens only for a newer complaint.
- The prior is doc 06 §11's, with p_model(MULE) = p_mule from `account_scores`. Without a score
  (no mule scorer yet, #91) it uses doc 13 §6's fallback (`features.mule_score_fallback`, 0.10),
  logged as a WARNING and stored in `prior_components.p_mule_source`.
- An account review has no anomaly model, so q = 0 and the open mass is the profile's base.
- `LINKED_COMPLAINTS = ONE_OR_MORE` is then stored as the case's first evidence record, through
  the evidence fetch, so it shows in the timeline and is not planned again. The case stays OPEN,
  since this is not an investigator action.
- The amount at stake is the account's executed credits over the last 7 days (doc 02).

**The mule-scorer trigger (#109 PR 2)** runs in the same sweep, which is now
`ACCOUNT_REVIEW_SWEEP`. It opens an ACCOUNT_REVIEW case for each home-bank account whose stored
score (`account_scores`, written by `MULE_SWEEP`, doc 13 §6) is ≥ `mule_sweep_threshold` (0.70)
and that had an executed credit in the last `navigator.mule_credit_window_h` (24 h). The prior is
the mule-scorer prior alone.
- It skips an account with an open review, or one whose latest review opened after its latest
  credit, so only new money reopens a disposed review.
- The candidates are chosen in SQL, oldest credit first, with `NOT EXISTS` against the account's
  reviews and a `LIMIT`; the credit filter uses the partial index `payments (payee_account_id,
  executed_at) WHERE status = 'EXECUTED'` (migration 0017).
- Complaints run first, so an account that is both complained about and a suspect gets the
  complaint's update. Both triggers share each batch and the per-run cap.
- `mule_sweep_interval_min` (15) is the interval at which `MULE_SWEEP` rescores accounts (doc 13
  §6); this sweep only reads the stored scores, every 60 s.
- `mule_credit_window_h` is a KB model default (24), left out of KB v1's canonical content so its
  hash stays the same.

**The stored recommendation (#208).** The case row keeps `recommended_decision` and
`risk_by_decision` current from the start, not only at disposition. Opening stores them for the
initial posterior. Each evidence record stores them for the new posterior, in the same
transaction as the posterior update. The disposition stores them as before. They are computed as
the console computes them: the Bayes decision under the case profile's losses, with the
vulnerable-payer weighting (doc 06 §4). The queue shows the recommended decision's risk as the
expected loss and can sort by it (doc 16).

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

**Implementation** (#103):
- `delphi.navigator.console` computes items 1–4 from the case's posterior with the planner
  (Phase NAVIGATOR, the profile's depth, investigator-minute cost) and the doc 06 §12 WCMM. The
  rows of item 2 keep the planner's order: eligible first, by net value. In the COERCION
  protocol, `CUSTOMER_OUTREACH_CALL` is moved to the top with `pinned_by_protocol` until it has been
  observed. The loss weighting uses the payer's vulnerability.
- `GET /cases/{id}` returns these with the case. Evidence records (item 5) and the observed set
  come with the evidence fetch (#102); until then the console plans with nothing observed.
- `delphi.navigator.disposition.dispose` (`POST /cases/{id}/disposition`, item 6) checks:
  - the `If-Match` version (412) and DISPOSED immutability (409);
  - the decision against the profile, and that CONFIRM_FRAUD names one of the case's fraud
    typologies;
  - confidence 1–5;
  - that an override of the recommendation, computed at that moment, has a rationale of at
    least 20 characters.

  It then moves the held payment (RELEASE executes it when the payer can fund it, else it stays
  RELEASED; CONFIRM_FRAUD cancels it, doc 05 §3), records the label the bank now knows
  (`INVESTIGATOR_DISPOSITION`: the typology, MULE, or GENUINE; doc 14 §7), and writes the audit
  entry `case.disposed` and the event `case.updated`. The free-text note is kept in the audit
  entry.

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

**Implementation** (#102): `delphi.navigator.evidence.request_evidence` locks the case and
refuses a disposed one, returns the current record for a source already fetched (unless
`refresh`, which supersedes it), ranks the source as the console would at that moment, runs the
adapter (`delphi.navigator.evidence_adapters`, as the app role) and the rule, updates the case
posterior with the KB likelihoods (the case moves to IN_PROGRESS), stores the record, and writes
the audit entry `evidence.recorded` and the event `case.updated`. The console plans with the
case's observed sources. Each new record queues `LLM_NARRATIVE` (`navigator.jobs.NarrativeJob`):
the worker builds the doc 15 §7 payload (the adapter's facts, an `ACC-…` pseudonym of the
source's subject account, the case's `as_of`), gets the text from `LlmService` (the model, or the
source's template), stores it with its source and citations, and publishes
`evidence.narrative_ready`. `{simulate: true}` on `CUSTOMER_OUTREACH_CALL` queues
`SIM_OUTREACH_CALL` (202): the worker's sim handler samples the answer (docs/14 §9) and records
it through the same `request_evidence`, as the app role, with no requester.

`CUSTOMER_OUTREACH_CALL` is special. In the demo UI it opens a call script dialog. The
investigator records the result by choosing one outcome (`CONFIRMS_COERCION`, …), and its
`raw` holds the note. In simulation/autoplay, the api enqueues a `SIM_OUTREACH_CALL` job. The
worker, which may read scenario truth, samples the outcome from the response model and records
it through the same service. The UI receives it over SSE. The answer is drawn from the dataset's own seed (`datasets.seed`, doc 14 §9), so every policy hears the same answer. The api refuses a simulated call (503) unless the deployment runs the simulator (`DELPHI_SIMULATION_ENABLED`, a non-secret flag the api and the worker both get; the api never holds the sim database URL), after checking the case itself (404, 409). A worker started with the flag but without `DELPHI_SIM_DATABASE_URL` refuses to start. A call that ends without a record (no ground truth for the payment) can be asked for again, and one for a case disposed in the meantime ends without recording anything. `CONFIRMS_COERCION` and `DENIES_INITIATING` create a label with source
`OUTREACH_CONFIRMED` (the hypothesis is the case's leading fraud hypothesis consistent with the
outcome: coercion → leading APP_* hypothesis; denies → ATO).

A `refresh` replaces the source's observation; it does not add a second one. The superseded
record's likelihood is divided out of the case posterior before the new outcome's is applied, so
the same outcome again leaves the posterior unchanged, and the new record's `posterior_before`
is the case without the old observation. A refreshed outreach call replaces its
`OUTREACH_CONFIRMED` label (or removes it when the new outcome gives none). A source disabled in
the case's KB version cannot be fetched.

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

The rules are `delphi.probes.evidence` (pure; thresholds from `kb/engine.yaml` `classifiers:`,
the look-back windows named in the module). In `PASS_THROUGH_DWELL`, a credit that is not yet
80% gone by `as_of` is *still dwelling*: its dwell is unknown but at least its age, so the rule
counts it as longer than any measured dwell (+∞). It pushes the median up and is never classified
as if its age were a measured dwell; one fresh, unmoved credit gives `OVER_24_H`, not
`UNDER_30_MIN`. The narrative's `median_dwell_minutes` (doc 15 §7) is the same median, or null
when that median falls on a credit still dwelling; the facts also carry `still_dwelling`, the
number of such credits, so the narrative can say so.

The adapters (`delphi.navigator.evidence_adapters`) read the bank as of `as_of`:

- Payments count only once `EXECUTED`, by `executed_at < as_of` (`INBOUND_FAN_IN` on both the
  window and the "prior relation" side, `PAYEE_RELATIONSHIP`). A held, declined or pending
  payment, including the case's own held payment, never credited the account.
- `BENEFICIARY_DEVICE_SHARING` counts only accounts opened by `as_of`.
- `ACCOUNT_PROFILE` for an account this bank does not hold is `ESTABLISHED` with null facts: an
  unknown age is not evidence of a new account.
- `LOGIN_GEO_VELOCITY`'s "last login" is the customer's last login at or before `as_of`, which
  may be older than the 24 h window; its city is new if no earlier login (at any time) was from
  it, and a first-ever login is not a new city. The speed uses only the logins in the window.

## 5. Investigator traces

Every interaction appends a `trace_events` row: `CASE_OPENED`, `CASE_VIEWED`,
`EVIDENCE_REQUESTED` (source, rank shown, net value shown, settle % shown), `EVIDENCE_VIEWED`,
`NARRATIVE_EXPANDED`, `WCMM_VIEWED`, `DISPOSITION` (decision, typology, confidence, override,
time since opened), `DD_PACK_VIEWED`. The frontend sends view events through
`POST /cases/{id}/trace-events` (batched, at most 1 request per 5 s).

**Implementation** (#108): `delphi.navigator.traces`, table `trace_events` (migration 0015). The
server writes the actions in the action's own transaction:
- `CASE_OPENED` (`open_case`; no user, `{protocol, decision}`);
- `EVIDENCE_REQUESTED` (`request_evidence`, a new record only, not an idempotent replay;
  `{source_key, evidence_record_id, rank_shown, net_value_shown_paise, settle_pct_shown,
  refresh}`, ranked as the console showed it);
- `DISPOSITION` (`dispose`; `{decision, typology, confidence, is_override,
  seconds_since_opened}`).

`DD_PACK_VIEWED` is recorded when `GET /cases/{id}/dd-pack` is read, for the viewer, with
`{format, complete}`. The console may send only the four view
events. `EVIDENCE_VIEWED` and `NARRATIVE_EXPANDED` name an evidence record of the case, and no
other field is stored from a browser. A batch with one bad event is refused whole, and times are
kept between the case's opening and now, since a browser's clock may be off.

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

**Implementation** (#106): `open_case` drafts the alert (`delphi.navigator.police_alert`,
`police_alert_drafts`) in the case's own transaction: the victim's pseudonym, the amount and
channel, the beneficiary account and the time. `POST /cases/{id}/police-alert/send|discard`
(`case:write`) is the only way to act on it. SEND records SENT_SIMULATED with the case's
evidence list at that moment; either action names the investigator and the time, is audited
(`police_alert.sent` / `.discarded`) and is final (409 afterwards). `GET /cases/{id}` shows the
alert. The outreach outcomes' `OUTREACH_CONFIRMED` labels are recorded by the evidence fetch
(§3).

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

For a Guardian session, "every change" ends at the decision: the flag is computed once, when the
session is decided, and is not recomputed afterwards. If the audit chain is later found broken,
the stored flag stays true while `GET /guardian/sessions/{id}/dd-pack` (computed on read)
reports `complete: false`; the endpoint is authoritative. A failure while computing the flag
leaves it false and never blocks the decision. The last planning step (the one that stopped)
has no probe run: its candidates are kept in the `guardian.session.decided` audit entry and
appear in sections 3 and 4.

**Implementation, case pack** (#108): `delphi.navigator.ddpack`, served by
`GET /cases/{id}/dd-pack`. The pack's shape (sections, the completeness rule, the printable page)
is `delphi.platform.ddpack`, shared with the Guardian session's pack. The Navigator may not import
Guardian, so a case's inherited trail (the session pack's sections 2, 3, 4 and 8) is passed in by
the api and the worker from `guardian.api.case_trail`.

The case's sections:
1. Header: the case, its subject, amount, channel, the customer's pseudonym, vulnerability and
   the KB version with its hash.
2. Prior: the session's prior section, or for a case without a session what `prior_components`
   stored (no feature contributions).
3. Timeline: the session's steps (origin `GUARDIAN`), then the case's current evidence records
   (origin `NAVIGATOR`), with rank and net value at request, facts, outcome and posterior before
   and after.
4. Guardrails: the session's, required only when the case has Guardian-originated steps.
5. Decisions: the engine's recommendation and risks, and the disposition with user, time,
   confidence, typology and any override rationale. It is empty until the case is DISPOSED.
6. Narratives: every current record's narrative, labelled "AI-written summary" or "Template
   summary", with its citations resolved to the cited records' sources. It is empty while any
   narrative is still being written. A case decided without evidence says "No evidence was
   fetched for this case."
7. Liens and tickets: not applicable until the chase (M6).
8. Audit: the case's chain check, plus the session's.
9. Method note.

`cases.dd_pack_complete` is recomputed after a disposition (in its transaction) and whenever a
narrative is written, which can complete section 6 after the decision. Both recomputations take the case row lock first (the narrative job before it writes, in the
fetch's order: case, then evidence), so a disposition racing the last narrative is serialised
with it and the later of the two stores the complete flag (#229). The recomputation runs in
a savepoint: a failure is logged, leaves the flag as it was and never blocks the change. The flag
is derived, so it does not bump the case's version. Reading it records `DD_PACK_VIEWED`
(§5).

## 8. Campaigns

Rules are in doc 05 §5. The campaign list is sorted by `rank_score` and shows the
F3 stage strip, case count, total expected loss, and victims who are vulnerable. Campaign
detail lists the cases and the shared infrastructure entities.
