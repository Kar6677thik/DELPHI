# 07 — Knowledge base (KB)

The KB is everything the decision engine reads: hypotheses, observation sources, likelihoods,
decision profiles, losses, costs, guardrails, the question bank and the regulatory parameters.
It is **detection-as-code** (research §9.7, ADR-0006).

## 1. Versioning model

- A **KB version** is an immutable, fully resolved bundle. It is stored as canonical JSON in
  `kb_versions.content`, together with `content_sha256`.
  Canonical JSON means UTF-8, keys sorted, no insignificant whitespace, and floats written with
  `repr()` round-trip precision.
- **KB v1** is authored as YAML under `kb/` in the repo. `delphi migrate`, after the schema
  upgrade, loads, validates and inserts it as version 1 with status `ACTIVE` when no KB version
  exists yet, so it is idempotent and runs on every deploy (doc 23 §4). It is audited as
  `kb.version.created`. The api and the worker read the ACTIVE version from the database, never
  the YAML; reading a version re-checks its `content_sha256`.
- Later versions are created **inside the app**, by the learning loop (doc 12) and by typology
  promotion (doc 11). Each has a `parent_id`, a `change_summary` and `created_by`.
- `GET /api/v1/kb/versions/{id}/export` returns the version as the same YAML tree (zip). The
  owner can commit it back to `kb/`. This step is manual and is not automated in CI (no bot
  commits).
- Status machine:

```
             ┌──────────── shadow ───────────► SHADOW ──── promote ────┐
CANDIDATE ───┤                                   │                     ▼
             ├──────────── promote (*) ─────────────────────────────► ACTIVE ── superseded ──► RETIRED
             └── reject ──► REJECTED ◄── reject ─┘                                               │
                                                             ACTIVE ◄──────── rollback ──────────┘
(*) CANDIDATE → ACTIVE directly is allowed only for versions created by learning runs,
    question-bank approvals and sensing-class updates. Versions that add or change a typology
    MUST pass through SHADOW (doc 11 §5–6).
```

- Producers of new versions, all with status CANDIDATE except where noted:
  - learning run (doc 12 §2);
  - question-bank approval batch (doc 07 §9);
  - typology draft entering shadow (created directly as SHADOW, doc 11 §5);
  - manual FDE edit of losses/costs/guardrails (`POST /kb/versions` from the KB page's
    editor, which edits a copy of ACTIVE).
- **Stale-parent rule.** Promotion (to SHADOW or ACTIVE) requires `parent_id` = the current
  ACTIVE id. Otherwise the API returns `409 KB_STALE_PARENT`, and the UI offers **"Recreate on
  current ACTIVE"**. That action re-runs the producing operation against the current ACTIVE
  (re-run the learning update, re-apply the question approvals, re-apply the typology draft, or
  re-apply the manual edit) and creates a new CANDIDATE/SHADOW. The stale version is marked
  REJECTED with the reason `superseded by rebase`. Changes are therefore never lost silently.
  When a SHADOW version is promoted, any other CANDIDATE versions become stale by this rule.
- Invariants, enforced by partial unique indexes: exactly one `ACTIVE`, at most one `SHADOW`.
- Every session, case, evidence record and chase operation stores the `kb_version_id` it used.
- Promotion rules and roles are in doc 11 §6 and doc 19 §3.

## 2. Repository layout

```
kb/
  kb.yaml                       # manifest: kb_format_version: 1, name, description
  engine.yaml                   # engine constants, guardrails, costs, time
  regulatory.yaml               # RBI draft SOP windows, golden hour
  hypotheses/                   # one file per hypothesis (GENUINE + typologies)
    GENUINE.yaml
    ATO.yaml
    APP_DIGITAL_ARREST.yaml
    APP_INVESTMENT.yaml
    MULE.yaml
  sources/
    probes/<SOURCE_KEY>.yaml    # outcomes, mode, tier, applicability (no likelihoods)
    evidence/<SOURCE_KEY>.yaml
  decision_profiles/<PROFILE>.yaml
  question_bank/<SOURCE_KEY>.yaml
  schema/*.schema.json          # generated from the Pydantic models; checked in CI
```

Likelihood rows live **in the hypothesis file**, so adding a typology means adding one file:

```yaml
# kb/hypotheses/APP_DIGITAL_ARREST.yaml
key: APP_DIGITAL_ARREST
display_name: "Coerced victim — digital arrest"
kind: TYPOLOGY                # GENUINE | TYPOLOGY
contexts: [OUTBOUND_PAYMENT]
f3_stages: [INITIAL_ACCESS, POSITIONING, EXECUTION]
color: "#C2410C"
open_mass_share: 0.5          # only used while the sensing model lacks this class (doc 06 §11)
loss:                         # one column per applicable decision profile (cells: base_inr / rate / vulnerability_scaled)
  GUARDIAN_PAYMENT: { RELEASE: [0, 0.95, true], HOLD_FOR_REVIEW: [200, 0.08, true], PROTECTIVE_HOLD: [200, 0.01, false], DECLINE: [50, 0.20, true] }
  CASE_PAYMENT_HOLD: { RELEASE: [0, 0.95, true], CONFIRM_FRAUD: [50, 0, false] }
description: >
  A genuine customer, coached live by a fraudster posing as police/CBI/customs, pays from
  their own device to a fraudster-controlled account.
likelihoods:
  CALL_STATUS: { probs: [0.85, 0.03, 0.12], ess: 20 }
  # … one entry per source (order of probs = order of outcomes in the source file)
```

**Manifest (`kb.yaml`)**: `kb_format_version: 1`, `name`, `description`,
`hypothesis_order` (the H order; `UNEXPLAINED` is never listed) and `source_order` (the order
in which phase-A passive outcomes are applied and candidates are listed). Both lists must name
exactly the files present.

**Source files** share `key`, `kind` (PROBE | EVIDENCE), `outcomes`, `contexts`, `enabled`,
`classifier` (the registered classifier name) and `description`.

- Probes add `mode` (PASSIVE | ACTIVE), `friction_tier` (passive ⇒ NONE), `channels` (null =
  any channel), `exclusive_group`, `requires_consent`, `question` and `answer_options` (both set
  or both unset), and `expected_latency_s`.
- Evidence adds `subject` (COUNTERPARTY | PAYER | PAYER_TO_COUNTERPARTY) and
  `expected_minutes`.

**Decision profile files** have `key`, `context`, `source_kind` (which sources the profile
plans over), `amount_basis` (PAYMENT_AMOUNT | CREDITS_7D), `decisions` (less intrusive first),
and `loss` (GENUINE and UNEXPLAINED cells only).

**YAML gotcha:** the loader uses YAML 1.1 (PyYAML), where bare `YES`, `NO`, `ON` and `OFF` are
booleans. Quote such keys (`"YES"`). The schema rejects booleans where keys are expected; it
never silently stringifies them (tested).

## 3. Validation rules (`delphi kb validate`, run in CI and on every version creation)

Every issue has a stable `code`. Beyond the numbered rules below, the loader also reports
`SCHEMA`, `YAML_ERROR`, `EMPTY_FILE`, `KEY_FILENAME_MISMATCH`, `DUPLICATE_KEY`,
`SOURCE_KIND_DIR`, `ORDER_MISMATCH`/`ORDER_DUPLICATE`, `GENUINE_DEFINITION`,
`PROFILE_NO_GENUINE`, `PROFILE_LOSS_KEYS`, `LOSS_ON_GENUINE`, `LOSS_PROFILE_MISSING`,
`LOSS_PROFILE_UNEXPECTED`, `QUESTION_BANK_UNKNOWN_PROBE`, `QUESTION_VARIANT_DUPLICATE_ID` and
`QUESTION_OPTIONS_MISMATCH` (every variant's option keys = the probe's `answer_options`).

1. Every key is `UPPER_SNAKE_CASE` and unique within its kind.
2. Every probability list matches its source's outcome count, has every value ≥ 1e-4, and
   sums to 1 within 1e-6. Each row's ESS is in [2, 500].
3. α = probs × ess. Every α ≥ 0.01.
4. Each decision profile defines a loss for every (decision, applicable hypothesis) pair,
   including `UNEXPLAINED`. The loss cells of a typology live in its hypothesis file's `loss`
   block: one column per decision profile whose context the typology applies to. `GENUINE` and
   `UNEXPLAINED` cells live in `decision_profiles/*.yaml`. The resolved profile tables must be
   complete.
5. Every typology has `open_mass_share` in (0, 0.9]. The runtime Σ ≤ 0.9 check is in
   doc 06 §11.
6. `UNEXPLAINED` is implicit. It MUST NOT appear as a hypothesis file.
7. Every question-probe source has at least one `APPROVED` English variant.
8. Every source references an outcome classifier that exists in code (`delphi.probes.classifiers`
   registry). A KB can only *enable* sources whose code already exists.
9. Validation errors are reported as a list of `{path, code, message}`, and the version is
   rejected.

## 4. Hypotheses in KB v1

Order (this is the H order used everywhere; `UNEXPLAINED` is appended last):

| # | Key | Display name | Contexts | F3 stages | Colour |
|---|---|---|---|---|---|
| 1 | `GENUINE` | Genuine | OUTBOUND_PAYMENT, ACCOUNT_REVIEW | — | `#15803D` |
| 2 | `ATO` | Account takeover | OUTBOUND_PAYMENT | INITIAL_ACCESS, POSITIONING, EXECUTION | `#7C3AED` |
| 3 | `APP_DIGITAL_ARREST` | Coerced victim — digital arrest | OUTBOUND_PAYMENT | INITIAL_ACCESS, POSITIONING, EXECUTION | `#C2410C` |
| 4 | `APP_INVESTMENT` | Coerced victim — investment scam | OUTBOUND_PAYMENT | INITIAL_ACCESS, POSITIONING, EXECUTION | `#B45309` |
| 5 | `MULE` | Mule account | OUTBOUND_PAYMENT, ACCOUNT_REVIEW | MONETIZATION | `#1D4ED8` |
| — | `UNEXPLAINED` | Unexplained (∅) | all | — | `#6B7280` |

Semantics: in `OUTBOUND_PAYMENT`, `MULE` means *the paying account* is a mule forwarding funds.
In `ACCOUNT_REVIEW`, `MULE` means the reviewed account is a mule, and `GENUINE` means it is a
legitimate account (e.g. a merchant).

Decision profile → applicable hypotheses:

| Profile | H |
|---|---|
| `GUARDIAN_PAYMENT` | GENUINE, ATO, APP_DIGITAL_ARREST, APP_INVESTMENT, MULE, UNEXPLAINED (+ active KB-only typologies with context OUTBOUND_PAYMENT) |
| `CASE_PAYMENT_HOLD` | same as `GUARDIAN_PAYMENT` |
| `CASE_ACCOUNT_REVIEW` | GENUINE, MULE, UNEXPLAINED |

## 5. Observation sources in KB v1

### 5.1 Probes (Guardian)

| Key | Mode | Tier | Outcomes (in order) | Applicability | Expected latency | Enabled |
|---|---|---|---|---|---|---|
| `BENEFICIARY_INTEL` | passive | NONE | LISTED_SUSPECT, HIGH_MULE_SCORE, CLEAN_KNOWN_ENTITY, CLEAN_UNKNOWN | OUTBOUND_PAYMENT | 0.1 s | ✔ |
| `COUNTERPARTY_PHONE_RISK` | passive | NONE | VERY_HIGH, MEDIUM_HIGH, LOW_OR_UNKNOWN | OUTBOUND_PAYMENT | 0.1 s | ✔ |
| `DEVICE_POSTURE` | passive | NONE | REMOTE_ACCESS_ACTIVE, NEW_DEVICE, KNOWN_DEVICE | OUTBOUND_PAYMENT | 0.1 s | ✔ |
| `SESSION_BEHAVIOUR` | passive | NONE | HESITANT_OR_PASTED, CADENCE_MISMATCH, NORMAL | OUTBOUND_PAYMENT | 0.1 s | ✔ |
| `CALL_STATUS` | passive (consent) | NONE | ON_CALL_UNKNOWN_NUMBER, ON_CALL_KNOWN_CONTACT, NOT_ON_CALL | OUTBOUND_PAYMENT | 0.1 s | ✔ |
| `OTP_STEP_UP` | active | LOW | PASSED, FAILED_OR_ABANDONED | OUTBOUND_PAYMENT; exclusive group `STEP_UP_AUTH` | 25 s | ✔ |
| `BIOMETRIC_STEP_UP` | active | LOW | PASSED, FAILED_OR_ABANDONED | OUTBOUND_PAYMENT; exclusive group `STEP_UP_AUTH` | 8 s | ✔ |
| `PURPOSE_QUESTION` | active | LOW | PLAUSIBLE_NORMAL_TIMING, HIGH_RISK_PURPOSE, TIMING_ANOMALOUS, BELIEVES_RECEIVING_MONEY | OUTBOUND_PAYMENT | 12 s | ✔ |
| `SPELL_BREAKER_DIGITAL_ARREST` | active | MEDIUM | HESITATION_OR_APP_SWITCH, ANSWERED_YES, ANSWERED_NO_NORMAL | OUTBOUND_PAYMENT | 15 s | ✔ |
| `SPELL_BREAKER_INVESTMENT` | active | MEDIUM | HESITATION_OR_APP_SWITCH, ANSWERED_YES, ANSWERED_NO_NORMAL | OUTBOUND_PAYMENT | 15 s | ✔ |
| `COOLING_OFF` | active | MEDIUM | PERSISTED_OR_ESCALATED, CANCELLED_BY_CUSTOMER, WAITED_NORMALLY | OUTBOUND_PAYMENT | 600 s | ✔ |
| `COLLECT_INTENT_CHECK` | active | LOW | EXPECTS_TO_RECEIVE, UNDERSTANDS_PAYING | OUTBOUND_PAYMENT, channel = `UPI_COLLECT_APPROVAL` | 10 s | ✘ (enabled in demo story S-4) |

### 5.2 Evidence items (Navigator)

"Subject" says which entity the item inspects. *Counterparty* is the beneficiary account of the
held payment; *payer* is the paying customer or account; in `ACCOUNT_REVIEW` both resolve to the
reviewed account.

| Key | Subject | Outcomes (in order) | Contexts | Minutes |
|---|---|---|---|---|
| `BENEFICIARY_DEVICE_SHARING` | counterparty | SHARED_3_PLUS, SHARED_1_2, NOT_SHARED | both | 0.5 |
| `INBOUND_FAN_IN` | counterparty | HIGH, MODERATE, LOW | both | 0.5 |
| `PASS_THROUGH_DWELL` | counterparty | UNDER_30_MIN, BETWEEN_30_MIN_24_H, OVER_24_H | both | 1.0 |
| `ACCOUNT_PROFILE` | counterparty | NEW_MINIMAL_KYC, NEW_FULL_KYC, ESTABLISHED | both | 0.5 |
| `LINKED_COMPLAINTS` | counterparty | ONE_OR_MORE, NONE | both | 1.0 |
| `CRM_CONTACT_HISTORY` | payer | SIM_SWAP_OR_DEVICE_LOSS_REPORTED, NONE | both | 1.0 |
| `LOGIN_GEO_VELOCITY` | payer | IMPOSSIBLE_TRAVEL, NEW_LOCATION, USUAL | both | 0.5 |
| `PAYEE_RELATIONSHIP` | payer→counterparty | NO_PRIOR_RELATION, PRIOR_SMALL_PAYMENTS, ESTABLISHED_PAYEE | PAYMENT_HOLD only | 0.5 |
| `CUSTOMER_OUTREACH_CALL` | payer | CONFIRMS_COERCION, CONFIRMS_GENUINE_PURPOSE, DENIES_INITIATING, UNREACHABLE | both | 8.0 |

The exact outcome thresholds (e.g. what counts as `HIGH` fan-in) are in doc 09 §4 and
doc 08 §5.

## 6. Likelihood tables in KB v1 (seed elicitation, ESS = 20 for every row)

These are the team's **placeholder elicitation**, to be replaced by real investigator
elicitation in FDE workshops. Columns follow the outcome order in §5.
G = GENUINE, ATO, DA = APP_DIGITAL_ARREST, INV = APP_INVESTMENT, MULE.

### Probes

| Source | G | ATO | DA | INV | MULE |
|---|---|---|---|---|---|
| BENEFICIARY_INTEL | .005 .025 .55 .42 | .15 .30 .05 .50 | .25 .35 .02 .38 | .20 .35 .05 .40 | .20 .45 .05 .30 |
| COUNTERPARTY_PHONE_RISK | .01 .04 .95 | .10 .20 .70 | .30 .30 .40 | .25 .30 .45 | .20 .30 .50 |
| DEVICE_POSTURE | .01 .07 .92 | .25 .65 .10 | .30 .05 .65 | .15 .05 .80 | .05 .35 .60 |
| SESSION_BEHAVIOUR | .08 .02 .90 | .15 .60 .25 | .55 .05 .40 | .40 .05 .55 | .45 .10 .45 |
| CALL_STATUS | .08 .12 .80 | .10 .05 .85 | .85 .03 .12 | .45 .05 .50 | .15 .10 .75 |
| OTP_STEP_UP | .98 .02 | .30 .70 | .97 .03 | .98 .02 | .99 .01 |
| BIOMETRIC_STEP_UP | .97 .03 | .15 .85 | .96 .04 | .97 .03 | .97 .03 |
| PURPOSE_QUESTION | .88 .06 .05 .01 | .40 .10 .49 .01 | .35 .30 .34 .01 | .30 .45 .24 .01 | .60 .10 .29 .01 |
| SPELL_BREAKER_DIGITAL_ARREST | .05 .01 .94 | .20 .01 .79 | .60 .15 .25 | .25 .02 .73 | .15 .01 .84 |
| SPELL_BREAKER_INVESTMENT | .05 .01 .94 | .20 .01 .79 | .25 .02 .73 | .55 .20 .25 | .15 .01 .84 |
| COOLING_OFF | .05 .10 .85 | .60 .05 .35 | .55 .20 .25 | .40 .25 .35 | .50 .05 .45 |
| COLLECT_INTENT_CHECK | .02 .98 | .02 .98 | .05 .95 | .10 .90 | .02 .98 |

### Evidence

| Source | G | ATO | DA | INV | MULE |
|---|---|---|---|---|---|
| BENEFICIARY_DEVICE_SHARING | .02 .08 .90 | .30 .20 .50 | .40 .25 .35 | .35 .25 .40 | .55 .25 .20 |
| INBOUND_FAN_IN | .03 .12 .85 | .30 .30 .40 | .45 .30 .25 | .50 .30 .20 | .65 .25 .10 |
| PASS_THROUGH_DWELL | .03 .17 .80 | .40 .30 .30 | .50 .30 .20 | .45 .30 .25 | .70 .20 .10 |
| ACCOUNT_PROFILE | .05 .15 .80 | .35 .25 .40 | .40 .25 .35 | .35 .30 .35 | .55 .25 .20 |
| LINKED_COMPLAINTS | .01 .99 | .25 .75 | .35 .65 | .30 .70 | .45 .55 |
| CRM_CONTACT_HISTORY | .02 .98 | .45 .55 | .02 .98 | .02 .98 | .03 .97 |
| LOGIN_GEO_VELOCITY | .005 .095 .90 | .35 .45 .20 | .02 .08 .90 | .02 .10 .88 | .10 .30 .60 |
| PAYEE_RELATIONSHIP | .45 .25 .30 | .90 .08 .02 | .85 .13 .02 | .55 .43 .02 | .70 .25 .05 |
| CUSTOMER_OUTREACH_CALL | .01 .85 .02 .12 | .02 .03 .80 .15 | .60 .15 .05 .20 | .50 .25 .05 .20 | .02 .38 .05 .55 |

In `ACCOUNT_REVIEW`, only the G and MULE rows are used.

## 7. Engine, guardrails, costs (`kb/engine.yaml`)

```yaml
engine:
  lookahead_depth: { guardian: 3, navigator: 2 }
  stop_epsilon_paise: 100
  exploration_epsilon: 0.05
  exploration_seed: 20261031
  vulnerability_multiplier: 1.5
  vulnerable_age_threshold: 70
  open_mass_base: 0.02
  open_mass_slope: 0.18
  unexplained_routing_threshold: 0.35     # terminal P(UNEXPLAINED) ≥ this → Scout queue
  fast_path: { min_p_genuine: 0.995, max_amount_inr: 10000, max_anomaly_percentile: 0.90 }  # on p_model, doc 08 §3
guardrails:
  max_active_probes_per_session: 3
  max_medium_plus_per_session: 2
  max_active_sessions_per_customer_30d: 3
  passive_adapter_timeout_ms: 300
  active_probe_response_timeout_s: 60     # human answer time; NOT time-scaled
  cooling_off_extra_timeout_s: 30         # COOLING_OFF timeout = scaled window + this
  session_max_duration_s: 900             # wall time excluding the COOLING_OFF window; NOT time-scaled
costs:
  friction_cost_inr: { NONE: 0, LOW: 20, MEDIUM: 150, HIGH: 600 }
  delay_cost_inr_per_s: 0.5
  investigator_cost_inr_per_min: 15
classifiers:                              # doc 08 §5, doc 09 §4
  mule_score_high: 0.50
  established_payee_min_payments: 3
  established_payee_min_distinct_customers: 3
  established_payee_min_age_days: 90
  new_device_days: 7
  paste_or_dwell_max_field_dwell_s: 20
  cadence_mismatch_z: 3.0
  purpose_fast_s: 1.5
  purpose_slow_s: 15
  spell_breaker_hesitation_s: 8
  otp_max_attempts: 3
  cooling_off_window_s: 600               # time-scaled
  cooling_off_send_now_presses: 2
  fan_in_high: 10
  fan_in_moderate: 3
  dwell_fast_min: 30
  dwell_slow_h: 24
  new_account_days: 90
  crm_lookback_days: 14
  impossible_travel_kmh: 500
  small_payment_inr: 5000
navigator:
  mule_sweep_threshold: 0.70
  mule_sweep_interval_min: 15             # time-scaled
  campaign_window_days: 7
chase:
  min_trace_inr: 500
  freeze_latency_internal_s: 60           # time-scaled
  freeze_latency_external_min: 30         # time-scaled
  dwell_fallback_mule_min: 20             # time-scaled
  dwell_fallback_other_h: 24              # time-scaled
  lawful_hold_kappa: 0.30
  wrongful_base_inr: 5000
  wrongful_rate: 0.02
  max_liens_per_round: 10
  retrace_interval_min: 2                 # time-scaled, floor 20 s real time
  ticket_expiry_min: 120                  # time-scaled
  visibility_lag_min: { demo: 0, eval: 5 }
scout:
  min_cluster_size: 5
  min_samples: 3
  lookback_days: 30
  auto_trigger_new_items: 10
  shadow_min_sessions: 200
  shadow_min_sessions_demo: 10            # used when DELPHI_TIME_MODE=DEMO
time:
  demo_time_scale: 0.0333333333           # 1/30: time-scaled durations shrink 30× when DELPHI_TIME_MODE=DEMO
elicitation:
  default_ess: 20
  confidence_ess: { LOW: 10, MEDIUM: 20, HIGH: 50 }
```

**Time scaling rule.** Keys marked *time-scaled* are simulated or external durations: customer
behaviour windows, external latencies, dwell times, SLAs, golden hour, sweep intervals. They are
multiplied by `demo_time_scale` when the single switch `DELPHI_TIME_MODE=DEMO` is set (with
`REAL`, the factor is 1). Under the evaluation's `VirtualClock`, the factor is also 1, and
simulated customers answer after their sampled latency in virtual time, so nothing waits. Human
interaction timeouts (`active_probe_response_timeout_s`, `session_max_duration_s`) are never
scaled. `DELPHI_ENV` is only a label for logs.

Derived probe costs in KB v1 (₹): passive 0.05; OTP 32.50; BIOMETRIC 24.00; PURPOSE 26.00;
each SPELL_BREAKER 157.50; COOLING_OFF 450.00; COLLECT_INTENT_CHECK 25.00.

## 8. Decision profiles and loss tables (`kb/decision_profiles/`)

Loss(d, h) = (base_inr·100 + rate·A) × m, where m = 1.5 if the cell is vulnerability-scaled
(**V**) and the customer is vulnerable. Cells are written `base / rate`.

### `GUARDIAN_PAYMENT`
Decision order: `RELEASE`, `HOLD_FOR_REVIEW`, `PROTECTIVE_HOLD`, `DECLINE`. A = payment amount.

| d \ h | GENUINE | ATO | APP_DIGITAL_ARREST | APP_INVESTMENT | MULE | UNEXPLAINED |
|---|---|---|---|---|---|---|
| RELEASE | 0 / 0 | 0 / .90 **V** | 0 / .95 **V** | 0 / .95 **V** | 0 / .50 | 0 / .60 **V** |
| HOLD_FOR_REVIEW | 1000 / .03 | 200 / .01 | 200 / .08 **V** | 200 / .06 **V** | 200 / .01 | 500 / .02 |
| PROTECTIVE_HOLD | 3000 / .04 | 300 / .01 | 200 / .01 | 200 / .02 | 1000 / .01 | 500 / .02 |
| DECLINE | 2000 / .05 | 50 / 0 | 50 / .20 **V** | 50 / .15 **V** | 50 / .02 | 100 / .05 |

Meaning of each decision:

- `RELEASE`: execute the payment.
- `HOLD_FOR_REVIEW`: payment stays pending. A `PAYMENT_HOLD` case opens with SLA
  `hold_review_sla_minutes` (240).
- `PROTECTIVE_HOLD`: payment stays pending. A `PAYMENT_HOLD` case opens with SLA
  `protective_sla_minutes` (30), with the coercion protocol: `CUSTOMER_OUTREACH_CALL` is pinned
  to the top of the evidence list, the customer sees the scam-education screen, and a police
  alert is drafted. The alert is **sent only after an investigator confirms** (FR-NAV-10).
- `DECLINE`: payment cancelled; the customer is told to contact the bank.
- The typology columns of this table are stored in each hypothesis file's `loss` block
  (§2), so a new typology brings its own columns.

### `CASE_PAYMENT_HOLD`
Decision order: `RELEASE`, `CONFIRM_FRAUD`. A = payment amount.

| d \ h | GENUINE | ATO | APP_DIGITAL_ARREST | APP_INVESTMENT | MULE | UNEXPLAINED |
|---|---|---|---|---|---|---|
| RELEASE | as GUARDIAN_PAYMENT RELEASE row |||||||
| CONFIRM_FRAUD | 2000 / .05 | 50 / 0 | 50 / 0 | 50 / 0 | 50 / 0 | 200 / 0 |

### `CASE_ACCOUNT_REVIEW`
Decision order: `CLEAR`, `CONFIRM_MULE`. A = sum of credits to the account in the last 7 days.

| d \ h | GENUINE | MULE | UNEXPLAINED |
|---|---|---|---|
| CLEAR | 0 / 0 | 0 / .50 | 0 / .30 |
| CONFIRM_MULE | 5000 / .05 | 100 / 0 | 1000 / .01 |

## 9. Question bank (`kb/question_bank/`)

Each question probe has variants:
`{variant_id, language: en|hi|te|ta, status: APPROVED|PENDING_REVIEW|REJECTED, text, options: [{key, text}]}`.
KB v1 ships three approved English variants per probe.

- **Telugu for demo story S-1.** Variants for `PURPOSE_QUESTION` and
  `SPELL_BREAKER_DIGITAL_ARREST` are written and reviewed by a native speaker *outside the
  app* (Pre-M0 item P-5). They are committed into `kb/question_bank/*.yaml` with
  `status: APPROVED` before M4 closes, so they are part of KB v1 and survive demo resets and
  fresh datasets. Hindi and Tamil may follow the same route.
- **In-app route (M5+).** Variants generated by `LLM_QUESTION_VARIANTS` are stored in
  `question_variants` as `PENDING_REVIEW`. "Approve batch" on the question-bank page creates a
  CANDIDATE KB version (parent = ACTIVE) containing the approved variants. An FDE promotes it
  (doc 07 §1). Until then, the variants are never served. To keep them across demo resets,
  export the version and commit it to `kb/`.

Canonical English (variant 1) and option keys:

| Probe | Text | Options |
|---|---|---|
| `PURPOSE_QUESTION` | "What is this payment for?" | FAMILY_OR_FRIEND, BILLS_OR_SHOPPING, MEDICAL, EDUCATION, PROPERTY_OR_LOAN, INVESTMENT, GOVT_POLICE_OR_COURT_FEE, SAFE_ACCOUNT_TRANSFER, CUSTOMS_OR_PARCEL, RECEIVING_REFUND_OR_PRIZE, OTHER |
| `SPELL_BREAKER_DIGITAL_ARREST` | "Has anyone claiming to be from the police, CBI, customs, a court or the RBI told you that you are under investigation, asked you to stay on a call or video call, or told you to keep this payment secret?" | YES, NO |
| `SPELL_BREAKER_INVESTMENT` | "Has someone promised you guaranteed or very high returns, added you to a trading group, or asked you to pay a fee or tax before you can withdraw your profits?" | YES, NO |
| `COLLECT_INTENT_CHECK` | "Are you expecting to RECEIVE money from this request, e.g. a refund, cashback or prize?" | YES_RECEIVING, NO_I_AM_PAYING |

Variants 2 and 3 rephrase the same question with the same option keys. Variant rotation:
for each customer, pick the approved variant in the customer's language that was shown to
this customer least recently (ties: lowest `variant_id`).

## 10. Regulatory parameters (`kb/regulatory.yaml`)

```yaml
profile: RBI_DRAFT_MULE_SOP_2026_09        # draft, not final (research §6)
account_review:
  customer_response_window_days: 20
  bank_decision_window_days: 10
  max_debit_hold_days: 60
case_sla:                                 # all time-scaled
  hold_review_sla_minutes: 240
  protective_sla_minutes: 30
  account_review_sla_minutes: 14400        # 10 days = bank decision window
chase:
  golden_hour_minutes: 60
  lien_review_interval_days: 10
```
