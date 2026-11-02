# 05 — Domain model

Tables are specified in doc 17. This document describes **aggregates, state machines and
invariants**. Code MUST enforce the invariants in the domain layer, and the database MUST back
them with constraints where possible (EFG §11 "invariant enforcement").

## 1. Simulated bank (reference data; owned by `sim`, read by everyone)

| Entity | Key fields | Notes |
|---|---|---|
| Bank | `bank_code` | `DLPH` is the home bank. External banks: `XBK1`…`XBK5` |
| Customer | id, display_name, age, language (`en`/`hi`/`te`/`ta`), is_vulnerable, segment, consent_call_status, home_city | `is_vulnerable` = age ≥ 70 or disability flag |
| Account | id, customer_id (nullable for external owners), bank_code, opened_at, kyc_level (`MINIMAL`/`FULL`), status, balance_paise | Balance is derived from `ledger_entries`; the cached column is updated in the same transaction |
| Device | id, customer_id, fingerprint, first_seen_at | |
| Payee | id, customer_id, account_id, label, added_at | A customer's saved beneficiary |
| Payment | id, payer_account_id, payee_account_id, amount_paise, channel (`UPI_PUSH`, `UPI_COLLECT_APPROVAL`, `IMPS`, `NEFT`, `CASH_OUT`), initiated_at, device_id, status, origin (`SIM_HISTORY`, `SIM_LIVE`, `DEMO`) | |
| Ledger entry | account_id, payment_id, delta_paise, balance_after_paise, at | Double entry: each executed payment writes −amount to the payer and +amount to the payee |
| Session telemetry | payment_id, typing/paste/dwell/app-switch metrics, remote-access flag, call-state | Inputs to passive probes |
| Login event | customer_id, device_id, geo (lat, lon, city), at | |
| CRM event | customer_id, type (`SIM_SWAP_REPORTED`, `DEVICE_LOST_REPORTED`, …), at | |
| Complaint | id, reported_at, victim_customer_id, payment_ids[], counterparty_account_ids[], channel (`NCRP_1930`, `BRANCH`) | Drives labels and Chase |

## 2. Guardian session (aggregate root: `GuardianSession`)

Owns its `ProbeRun`s. One session per payment (`payment_id` UNIQUE).

```
CREATED ─► PASSIVE_PROBING ─► PLANNING ─► AWAITING_CUSTOMER ─► PLANNING … ─► DECIDED
   │                             │                 │
   │ fast path                   │ STOP            │ customer cancels
   └─────────────► DECIDED ◄─────┘                 └─► CANCELLED_BY_CUSTOMER
any state ── unrecoverable error ──► DECIDED (decision = HOLD_FOR_REVIEW, terminal_reason = ENGINE_ERROR)
```

Invariants:

- `decision` is non-null **iff** status = `DECIDED`. `terminal_reason` ∈ {`STOP`, `FAST_PATH`,
  `BUDGET_EXHAUSTED`, `SESSION_TIMEOUT`, `ENGINE_ERROR`, `CUSTOMER_CANCELLED`}.
- At most one `ProbeRun` is in status `AWAITING_CUSTOMER` at a time.
- The posterior stored on the session equals the posterior after the last completed probe run.
- `kb_version_id` is fixed when the session is created and never changes.
- Payment status transitions are driven only by the session (`RELEASE` → `RELEASED`;
  `HOLD_FOR_REVIEW`/`PROTECTIVE_HOLD` → `HELD`; `DECLINE` → `DECLINED`; cancel → `CANCELLED`).

`ProbeRun` statuses: `PENDING` → (`AWAITING_CUSTOMER` for active probes) → `COMPLETED` |
`TIMED_OUT` | `ERROR`. Only `COMPLETED` runs update the belief. Exception: for `OTP_STEP_UP` and
`BIOMETRIC_STEP_UP`, a timeout is classified as the outcome `FAILED_OR_ABANDONED` and counts as
`COMPLETED`.

## 3. Payment state machine

```
PENDING_GUARDIAN ─► RELEASED ─► EXECUTED
        │        ─► DECLINED
        │        ─► CANCELLED              (customer cancelled during a probe)
        └──────► HELD ─► RELEASED ─► EXECUTED   (case RELEASE)
                      ─► CANCELLED              (case CONFIRM_FRAUD)
```

`EXECUTED` writes the ledger entries. In the simulation, `RELEASED` → `EXECUTED` is immediate.

## 4. Case (aggregate root: `Case`)

Owns its `EvidenceRecord`s and `TraceEvent`s.

| Field | Rule |
|---|---|
| type | `PAYMENT_HOLD` (subject: payment) or `ACCOUNT_REVIEW` (subject: account) |
| status | `OPEN` → `IN_PROGRESS` (first investigator action) → `DISPOSED` |
| posterior | Starts from the Guardian session's final posterior (`PAYMENT_HOLD`), or from the prior built from the mule scorer (`ACCOUNT_REVIEW`) |
| protocol | `STANDARD` or `COERCION` (set when opened from `PROTECTIVE_HOLD`) |
| sla_due_at | opened_at + SLA from `regulatory.yaml` |
| version | Optimistic lock. Incremented on every state change |

Invariants:

- A `DISPOSED` case is immutable, except for appending trace events of type `CASE_VIEWED` and
  `DD_PACK_VIEWED`.
- Disposition decisions: `RELEASE`/`CONFIRM_FRAUD` for `PAYMENT_HOLD`; `CLEAR`/`CONFIRM_MULE`
  for `ACCOUNT_REVIEW`.
- `CONFIRM_FRAUD` requires a `disposition_typology`, which must be a fraud hypothesis that
  applies to the case.
- If the disposition differs from the engine's recommended decision at the moment of
  disposition, `override_rationale` (≥ 20 characters) is required and `is_override = true`.
- Each evidence item can be fetched at most once per case (re-fetch is a separate, explicit
  `refresh` action that creates a new record and marks the old one `superseded_by`).
- One open `PAYMENT_HOLD` case per payment. One open `ACCOUNT_REVIEW` case per account.

## 5. Campaign

Campaigns are a derived grouping, recomputed by `navigator.campaigns.recompute()` after every
disposition and every new case.

- **Merge rule.** Two cases belong to the same campaign if (a) they have the same leading
  hypothesis (argmax of the posterior, excluding `GENUINE`), and (b) they share at least one
  infrastructure entity: a device fingerprint, a counterparty account, or a login /24 IPv4
  prefix. (c) Both cases must have been opened within 7 days of each other. Grouping is the
  union-find closure.
- Cases whose leading hypothesis is `GENUINE` are never in a campaign.
- Campaign fields: leading_hypothesis, case_count, f3_stage_max (furthest stage seen),
  expected_loss_paise (Σ over cases of risk(RELEASE) under the case posterior),
  vulnerability_weight (count of vulnerable victims), rank_score =
  expected_loss_paise × (1 + 0.5 · vulnerable_victim_share).
- Campaign ids are stable. When campaigns merge, the oldest id survives and the other is marked
  `merged_into`.

## 6. Chase operation (aggregate root: `ChaseOperation`)

Owns `ChaseCandidate`s, `Lien`s and `CfcfrmsTicket`s.

```
ChaseOperation: OPEN ─► CLOSED (manual, or automatically once the golden hour has elapsed and no ticket is pending)
Lien:           PROPOSED ─► APPROVED ─► PLACED | FAILED_FUNDS_MOVED                      (internal account)
                                    └─► TICKET_SENT ─► PLACED | FAILED_FUNDS_MOVED | FAILED_NO_RESPONSE (external)
                PLACED ─► RELEASED (manual, with reason)
                PROPOSED ─► DISCARDED
CfcfrmsTicket:  DRAFTED ─► SENT_SIMULATED ─► ACKNOWLEDGED | EXPIRED (ticket_expiry_min elapsed)
```

Invariants:

- `lien.amount_paise ≤ candidate.taint_paise` at the time of approval, and ≤ the account
  balance at placement. **A lien is never placed for the whole account balance unless the whole
  balance is taint** (research §9.6, FR-CHS-05).
- Every lien has non-empty `recorded_reasons` and `evidence_refs`.
- `review_due_at` = placed_at + `lien_review_interval_days`.

## 7. Knowledge base and typology lifecycle

KB version statuses are in doc 07 §1. A typology draft follows:

```
CANDIDATE (from Scout) ─► DRAFT (FDE edits) ─► BACKTESTED ─► IN_SHADOW ─► PROMOTED
                                   │               │              │
                                   └──── REJECTED ◄┴──────────────┘
```

- `BACKTESTED` requires a completed `RUN_BACKTEST` job on the current draft revision. Any edit
  after the backtest returns the draft to `DRAFT`.
- `IN_SHADOW` creates a KB version (parent = current `ACTIVE` + this typology) with status
  `SHADOW`. Only one draft can be `IN_SHADOW` at a time.
- `PROMOTED` requires ≥ `shadow_min_sessions` shadow evaluations, and an approver who has
  **never edited any revision of the draft** (four-eyes; tracked in `typology_drafts.editors`).
  It flips the shadow KB version to `ACTIVE` and retires the
  previous one.

## 8. Labels

A label is `{subject_type (PAYMENT|ACCOUNT), subject_id, hypothesis_key, label_source, known_at}`.
`hypothesis_key` is a hypothesis of the ACTIVE KB (never `UNEXPLAINED`), or the special value
`FRAUD_OTHER`, meaning confirmed fraud of a type the KB does not model (e.g. a complaint about
the hidden collect-refund scam before its typology exists). The learning loop skips
`FRAUD_OTHER` labels. Scout backtests count them as fraud.
Labels are **observations by the bank**, created by the simulation's label-arrival process or by
human actions. They are never read from `sim_truth` directly. Precedence when several labels
exist for one subject: `INVESTIGATOR_DISPOSITION` > `OUTREACH_CONFIRMED` > `CUSTOMER_COMPLAINT` >
`REGISTRY_LISTING` > `NO_COMPLAINT_60D`.

## 9. Users and roles

`User {id, username, display_name, role (VIEWER|INVESTIGATOR|FDE_ENGINEER|ADMIN), disabled}`.
The permission matrix is in doc 19 §3.
