# 17 — Data model (PostgreSQL 16)

## 1. General rules

- SQLAlchemy 2.0 models plus **Alembic** migrations (`backend/migrations/`). Every schema change
  is a migration. There is no `create_all` outside tests.
- Migrations follow **expand → migrate → contract** across releases, so a running old pod keeps
  working during a rolling update (EFG §6). Destructive changes (drop or rename a column) need
  two releases.
- Primary keys are `uuid` (UUIDv7, generated in the app), except append-only logs, which use
  `bigint generated always as identity`.
- Money `bigint` paise with `CHECK (x >= 0)` where applicable. Probabilities `jsonb`
  (`{key: float}`). Timestamps `timestamptz`.
- Every table has `created_at timestamptz not null default now()`, except append-only logs
  (`audit_log`, `outbox_events`, `trace_events`, …), whose `at` column is the creation time.
  Mutable aggregates have `version int not null default 1`.
- Foreign keys are always declared and indexed. Enums are PostgreSQL `text` + `CHECK (col IN …)`,
  which are easier to evolve than native enums.
- All queries are parameterised (SQLAlchemy). No string-built SQL.

## 2. Schemas and roles

| Schema | Contents | Role access |
|---|---|---|
| `bank` | simulated bank entities (customers … complaints) | app: read/write |
| `delphi` | product tables (sessions, cases, KB, jobs, audit …) | app: read/write; `audit_log`: INSERT/SELECT only |
| `sim_truth` | ground truth | `delphi_sim` role only. The app role `delphi_app` has **no grant** |

Two database roles: `delphi_app` (used by the api) and `delphi_sim` (used by the `sim` and
`evaluation` code paths in the worker, which hold a second connection pool). The DSN for each
comes from the Secrets in doc 23 §4 (the api's Secret holds only the app role's URL).

- **Who creates the roles.** In the home deployment, the Postgres init script creates both as
  LOGIN roles with passwords from Secret `delphi-postgres` (doc 23 §4). The first migration creates them as
  NOLOGIN roles only if they do not exist yet (CI, a fresh local database), then applies the
  grants. Roles are cluster-wide and are never dropped by a downgrade.
- **Who owns the schema.** Migrations run as one owner role (`DELPHI_MIGRATE_DATABASE_URL`; the
  `postgres` superuser in the home deployment), always the same one, because the default
  privileges below belong to the role that creates the tables.
- **Grants.** `delphi_app`: USAGE on `delphi` and `bank`, DML on their tables and sequences
  (via default privileges), no DDL. `delphi_sim`: the same plus `sim_truth`. Both:
  INSERT/SELECT only on `audit_log`; SELECT on `public.alembic_version` (for `/ready`).

## 3. Tables

### `bank` schema

```
customers(id, display_name, age, language, is_vulnerable, disability_flag, segment, income_band,
          consent_call_status bool, home_city, persona_key null unique, created_at)
accounts(id, customer_id null → customers, bank_code, is_merchant bool, merchant_category null,
         opened_at, kyc_level, status, balance_paise bigint, phone_number null, vpa unique null,
         created_at, version)
devices(id, customer_id → customers, fingerprint, os, first_seen_at)
payees(id, customer_id, account_id, label, added_at, unique(customer_id, account_id))
payments(id, payer_account_id, payee_account_id, amount_paise > 0, channel, initiated_at,
         device_id null, status, origin, executed_at null, scheduled_by null, created_at)
   idx: (payer_account_id, initiated_at), (payee_account_id, initiated_at), (status)
ledger_entries(id bigint identity, account_id, payment_id, delta_paise, balance_after_paise, at)
   idx: (account_id, at)
session_telemetry(payment_id pk → payments, typing_cadence_z, paste_on_account_field bool,
                  max_field_dwell_s, remote_access_active bool, screen_share_active bool,
                  on_call bool, call_counterparty_known bool null, session_duration_s)
login_events(id, customer_id, device_id, ip_prefix24, lat, lon, city, at)  idx (customer_id, at)
crm_events(id, customer_id, type, at)
complaints(id, reported_at, victim_customer_id, channel, category, payment_ids uuid[],
           counterparty_account_ids uuid[])
account_scores(account_id pk, p_mule, computed_at, model_artifact_id)
sim_registry_listings(account_id pk, listed_at)
sim_phone_risk(phone_number pk, category)
sim_schedule(id, due_at, kind (TRANSFER|TICKET_ACK|BACKGROUND_PAYMENT), payload jsonb,
             done_at null)           idx (done_at nulls first, due_at)   -- labels live in sim_truth.label_schedule
```

Migration 0002 creates these tables, with SQLAlchemy Core definitions in `delphi.sim.bank`.
Choices the list above leaves open:

- Enums are CHECKed where doc 05 fixes the values: `language`, `income_band`, `bank_code`,
  `kyc_level`, payment `channel`, `status` and `origin`, complaint `channel`, `sim_phone_risk`
  `category` (`VERY_HIGH|HIGH|MEDIUM|LOW`) and `sim_schedule.kind`. Migration 0003 adds CHECKs
  for the value sets the generator fixes (#17, `sim/config/generator.yaml`):
  - customer `segment`: `RETAIL`
  - `merchant_category`: the nine categories of doc 14 §3
  - device `os`: `ANDROID|IOS`
  - CRM `type`: `SIM_SWAP_REPORTED|DEVICE_LOST_REPORTED`
  - complaint `category`: `UNAUTHORISED_TRANSACTION|DIGITAL_ARREST_SCAM|INVESTMENT_SCAM`

  Account `status` stays plain `text`: the freeze and lien states are Chase's to define (M6).
  A new value widens its CHECK in a later migration.
- `delphi.sim.writer` streams a generated dataset in with `COPY … FROM STDIN` (psycopg 3), one
  table at a time in foreign-key order, in one transaction, as `delphi_sim` (#58). The column
  lists come from the records; columns they lack take their defaults. A failed COPY raises the
  same SQLAlchemy error an insert would, and the transaction rolls back.
- `payments.scheduled_by → sim_schedule`; `ip_prefix24` is a `cidr` /24; `balance_paise`,
  `balance_after_paise ≥ 0`; `delta_paise ≠ 0`.
- `login_events`, `crm_events` and `ledger_entries` are event logs: `at` is their creation time.
  Every other table has `created_at` (§1).
- `account_scores` is created in M3 with `delphi.model_artifacts`, which its foreign key needs
  (docs/tech-debt.md TD-7).

### `delphi` schema

```
users(id, username unique, display_name, password_hash, role, disabled bool, created_at, version)
auth_sessions(id (random 256-bit, stored hashed), user_id, created_at, expires_at, last_seen_at,
              ip, user_agent)
idempotency_keys(key, user_id, endpoint, request_sha256, response_status, response_body jsonb,
                 created_at, primary key(key, user_id))

kb_versions(id serial, status, parent_id null, content jsonb, content_sha256, change_summary,
            created_by, approved_by null, created_at, activated_at null, retired_at null,
            rollback_reason null, version)
   unique index on (status) where status = 'ACTIVE'; unique index on (status) where status = 'SHADOW'

guardian_sessions(id, payment_id unique → bank.payments, status, decision null, terminal_reason null,
                  kb_version_id, prior jsonb, p_model jsonb, anomaly_percentile, open_mass,
                  open_mass_shares jsonb, top_features jsonb, posterior jsonb, risk_by_decision jsonb,
                  unexplained bool, mode (LIVE|AUTOPLAY|EVAL), runner (API|WORKER), rng_seed bigint,
                  started_at, decided_at null, case_id null, baseline_decisions jsonb,
                  dd_pack_complete bool, version)
probe_runs(id, session_id, seq int, probe_key, mode (PASSIVE|ACTIVE), status, selected_by null,
           propensity null, eligible_set jsonb, candidates jsonb, lookahead_depth, voi_myopic_paise,
           net_value_paise, cost_paise, variant_id null, language null, raw_response jsonb,
           outcome null, posterior_before jsonb, posterior_after jsonb, info_yield_bits null,
           started_at, completed_at null, unique(session_id, seq), unique(session_id, probe_key))

cases(id, type, protocol, status, subject_payment_id null, subject_account_id null,
      guardian_session_id null, kb_version_id, prior_components jsonb, posterior jsonb,
      recommended_decision, risk_by_decision jsonb, leading_hypothesis, campaign_id null,
      amount_at_stake_paise, opened_at, sla_due_at, disposition null, disposition_typology null,
      disposition_confidence null, is_override bool, override_rationale null, disposed_by null,
      disposed_at null, dd_pack_complete bool, unexplained bool, version)
   partial unique (subject_payment_id) where status <> 'DISPOSED';
   partial unique (subject_account_id) where status <> 'DISPOSED' and type = 'ACCOUNT_REVIEW'
evidence_records(id, case_id, source_key, outcome, raw jsonb, record_refs jsonb, fetch_ms,
                 posterior_before, posterior_after, info_yield_bits, rank_at_request,
                 net_value_at_request_paise, requested_by, requested_at, narrative null,
                 narrative_source null, narrative_citations jsonb null, superseded_by null)
trace_events(id bigint identity, case_id, user_id, type, payload jsonb, at)
police_alert_drafts(id, case_id, status (DRAFT|SENT_SIMULATED|DISCARDED), payload jsonb,
                    acted_by null, acted_at null)
campaigns(id, leading_hypothesis, case_count, f3_stage_max, expected_loss_paise,
          vulnerable_victims, rank_score, merged_into null, created_at, updated_at)
labels(id, subject_type, subject_id, hypothesis_key, label_source, known_at,
       unique(subject_type, subject_id, label_source))

chase_operations(id, complaint_id null, source_payment_ids uuid[], status, reported_at,
                 reported_loss_paise, kb_version_id, golden_hour_ends_at, last_traced_at,
                 metrics jsonb, created_by, version)
chase_candidates(id, operation_id, account_id, hop, taint_paise, balance_paise, p_present, p_mule,
                 erv_paise, wrongful_cost_paise, net_paise, rank, recommended bool, traced_at)
liens(id, operation_id, candidate_id, account_id, bank_code, amount_paise, status, recorded_reasons,
      evidence_refs jsonb, approved_by, approved_at, placed_at null, review_due_at null,
      released_at null, release_reason null)
cfcfrms_tickets(id, lien_id, target_bank, status (DRAFTED|SENT_SIMULATED|ACKNOWLEDGED|EXPIRED),
                payload jsonb, sent_at, acknowledged_at null)

scout_runs(id, job_id, started_at, finished_at, n_items, n_clusters, params jsonb)
scout_clusters(id, run_id, size, member_refs jsonb, distinguishing_features jsonb,
               outcome_profile jsonb, created_at)
typology_drafts(id, cluster_id null, status, revision int, spec jsonb, last_edited_by,
                editors uuid[] (every user who ever saved a revision),
                backtest_report jsonb null, backtested_revision null, shadow_kb_version_id null,
                approved_by null, rejected_reason null, created_at, updated_at, version)
shadow_evaluations(id bigint identity, kb_version_id, subject_type, subject_id, posterior jsonb,
                   decision, would_select_first null, agrees bool, at)

learning_runs(id, job_id, parent_kb_version_id, candidate_kb_version_id, report jsonb, created_at)
learning_contributions(subject_type, subject_id, source_key, learning_run_id,
                       primary key(subject_type, subject_id, source_key))
probe_yield_daily(source_key, day, n, mean_info_yield_bits, primary key(source_key, day))

question_variants(id, probe_key, language, text, options jsonb, status, generated_by (HUMAN|LLM),
                  llm_call_id null, reviewed_by null, reviewed_at null, included_in_kb_version null)
variant_exposures(customer_id, variant_id, last_shown_at, primary key(customer_id, variant_id))

model_artifacts(id, kind, version, status, trained_at, training_cutoff, metrics jsonb,
                feature_list jsonb, blob bytea, sha256)
datasets(id, job_id unique → jobs, profile (test|demo|eval), seed, generator_version,
         status (READY), stats jsonb, seed_completed_at null, created_at)   -- migration 0005
evaluations(id, job_id, dataset_id, policies jsonb, seed, metrics jsonb, created_at)

jobs(id, type, status (QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED), params jsonb, result jsonb null,
     error text null, progress real, attempts int, max_attempts int default 3,
     idempotency_key unique, locked_by null, locked_until null, run_after timestamptz,
     created_by null, created_at, started_at null, finished_at null)
   idx (status, run_after)
outbox_events(id bigint identity, type, entity_id, payload jsonb, at)   idx (at)
ops_backup_runs(id bigint identity, finished_at, file_name, size_bytes, status)  -- written by the backup CronJob
llm_calls(id, purpose, provider, model, prompt_version, request_redacted jsonb, response jsonb null,
          status, validation jsonb, input_tokens, output_tokens, latency_ms, created_at)
audit_log(id bigint identity, at, actor_type (USER|SYSTEM|ENGINE|LLM|JOB), actor_id null, action,
          entity_type, entity_id, payload jsonb, prev_hash bytea, hash bytea)
```

### `sim_truth` schema

```
scenario_instances(id, scenario_type, hypothesis_key, params jsonb, started_at)
payment_truth(payment_id pk, scenario_instance_id, is_fraud bool, hypothesis_key, coached bool)
account_truth(account_id pk, is_mule bool, ring_id null, layer null)
response_overrides(scenario_type, source_key, coached bool, probs jsonb,
                   pk (scenario_type, source_key, coached))      -- frozen response models
label_schedule(subject_type, subject_id, hypothesis_key, label_source, due_at, delivered bool,
               pk (subject_type, subject_id, label_source))  idx (due_at) where not delivered
schedule_truth(schedule_id pk → bank.sim_schedule, reason (SALARY|MULE_FORWARDING),
               flow null, session bool)                          -- migration 0004
persona_manifests(dataset_id pk → delphi.datasets on delete cascade, manifest jsonb)  -- 0005
```

`delphi.datasets` has a row per dataset the `GENERATE_DATASET` job wrote (#19). The job writes
the bank rows, this row and the persona manifest in one transaction, so a row only exists
`READY`; `job_id` lets a retried attempt find what an earlier one committed. `stats` holds the
row counts and the doc 14 stats report. `seed_completed_at` is the marker demo reset deletes
back to (doc 14 §10). The bank tables hold one dataset (their ids are deterministic), so a
second one is refused until the bank is reset. `persona_manifests` keeps the persona manifest
(doc 14 §6) for the story runners; it is ground truth because it names the scripted rings and
the story payees. The api finds the personas themselves through `bank.customers.persona_key`.

`bank.sim_schedule` payloads carry only what executing a transfer needs (payer, payee, amount,
channel). Why a scheduled transfer exists is ground truth, kept in `schedule_truth` (#56).

Ground truth references the bank (`payment_truth.payment_id → bank.payments`,
`account_truth.account_id → bank.accounts`, `schedule_truth.schedule_id → bank.sim_schedule`)
with the default NO ACTION, so deleting a bank row
that truth still describes fails instead of silently dropping the truth. `response_overrides`
keys coached rows separately (doc 14 §5.2) and stores `probs` as `{outcome: p}`. As everywhere
(§1), every table also has `created_at`. The `delphi.sim.truth` module holds the SQLAlchemy Core
definitions; only `delphi.sim` and `delphi.evaluation` may import it (doc 04 §4.1 rule 5).

## 4. Audit hash chain

`hash = SHA-256(prev_hash || canonical_json({at, actor_type, actor_id, action, entity_type,
entity_id, payload}))`, and the genesis `prev_hash` is 32 zero bytes. Writes take a transaction
advisory lock (`pg_advisory_xact_lock(42)`), so the chain is serial. Other advisory lock keys:
43 for outbox publishing (doc 04 §5.5), 44 for the bootstrap admin. Verifying a window checks
that its first entry links to the entry just before the window (or to the genesis hash), so a
deleted or edited earlier entry is caught too. `GET
/admin/audit/verify?from=&to=` recomputes the chain. The DD pack verifies the entries for its
subject. Audited actions: every state transition of every aggregate, logins (success and
failure), role changes, KB promotions, rollbacks, lien approvals, police-alert sends and demo
resets.

## 5. Indexing and performance notes

- Feature queries depend on `payments(payer_account_id, initiated_at)` and
  `ledger_entries(account_id, at)`.
- Chase reads the ledger since t0 for the traced component only. It loads in batches by
  account and repeats until there are no new accounts.
- The `eval` profile (~1.8M payments) is processed by the evaluation harness **in memory**
  (pandas/numpy); it is not queried row by row.
- Run `EXPLAIN ANALYZE` on the five hottest queries (feature snapshot, guardian list, case list,
  chase ledger scan, metrics summary) as part of M9 (doc 25). Each must be < 50 ms on the demo
  profile.

## 6. Retention

| Data | Retention |
|---|---|
| `llm_calls.request_redacted/response` | 30 days, then nulled (`RETENTION_SWEEP` job, daily) |
| `outbox_events` | 24 h |
| `idempotency_keys` | 24 h |
| `auth_sessions` | until expiry (12 h idle / 7 days absolute), then deleted |
| Everything else | Kept until a demo reset or dataset regeneration |
