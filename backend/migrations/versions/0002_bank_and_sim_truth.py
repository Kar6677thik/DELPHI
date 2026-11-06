"""The simulated bank and the quarantined ground truth.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05

Creates the `bank` tables (the synthetic bank's core system, readable by the app) and the
`sim_truth` tables (ground truth, readable only by `delphi_sim`) of docs/17 §3. 0001 already
created both schemas and their default privileges, so the new tables get the right grants by
being created here, by the same owner role. `delphi_app` has no grant at all on `sim_truth`
(docs/17 §2, FR-SIM-07).

Expand/contract (docs/17 §1): this migration only adds tables, so the previous release keeps
working.

Value sets the spec does not fix yet (account `status`, customer `segment`, complaint
`category`, `merchant_category`, device `os`, CRM `type`) are plain `text`; the generator
(#17) fixes them and adds their CHECKs. `bank.account_scores` needs `delphi.model_artifacts`
for its foreign key, so it is created with that table in M3 (docs/tech-debt.md TD-7).
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

BANK = """
CREATE TABLE bank.customers (
  id                  uuid PRIMARY KEY,
  display_name        text NOT NULL,
  age                 smallint NOT NULL CHECK (age BETWEEN 18 AND 120),
  language            text NOT NULL CHECK (language IN ('en', 'hi', 'te', 'ta')),
  is_vulnerable       boolean NOT NULL,
  disability_flag     boolean NOT NULL DEFAULT false,
  segment             text NOT NULL,
  income_band         text NOT NULL CHECK (income_band IN ('LOW', 'MID', 'HIGH')),
  consent_call_status boolean NOT NULL,
  home_city           text NOT NULL,
  persona_key         text UNIQUE,
  created_at          timestamptz NOT NULL DEFAULT now()
);

-- Accounts at the home bank and at the five external banks. External owners have no customer.
CREATE TABLE bank.accounts (
  id                uuid PRIMARY KEY,
  customer_id       uuid REFERENCES bank.customers (id),
  bank_code         text NOT NULL
                    CHECK (bank_code IN ('DLPH', 'XBK1', 'XBK2', 'XBK3', 'XBK4', 'XBK5')),
  is_merchant       boolean NOT NULL DEFAULT false,
  merchant_category text,
  opened_at         timestamptz NOT NULL,
  kyc_level         text NOT NULL CHECK (kyc_level IN ('MINIMAL', 'FULL')),
  status            text NOT NULL,
  balance_paise     bigint NOT NULL CHECK (balance_paise >= 0),
  phone_number      text,
  vpa               text UNIQUE,
  created_at        timestamptz NOT NULL DEFAULT now(),
  version           integer NOT NULL DEFAULT 1
);
CREATE INDEX accounts_customer_id_idx ON bank.accounts (customer_id);

CREATE TABLE bank.devices (
  id            uuid PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES bank.customers (id),
  fingerprint   text NOT NULL,
  os            text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX devices_customer_id_idx ON bank.devices (customer_id);

CREATE TABLE bank.payees (
  id          uuid PRIMARY KEY,
  customer_id uuid NOT NULL REFERENCES bank.customers (id),
  account_id  uuid NOT NULL REFERENCES bank.accounts (id),
  label       text NOT NULL,
  added_at    timestamptz NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, account_id)
);
CREATE INDEX payees_account_id_idx ON bank.payees (account_id);

-- Simulator work items (mule transfers, CFCFRMS acks, background payments). Never labels: those
-- live in sim_truth.label_schedule, so the app role cannot see future labels (docs/14 §8).
CREATE TABLE bank.sim_schedule (
  id         uuid PRIMARY KEY,
  due_at     timestamptz NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('TRANSFER', 'TICKET_ACK', 'BACKGROUND_PAYMENT')),
  payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
  done_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sim_schedule_done_at_due_at_idx ON bank.sim_schedule (done_at NULLS FIRST, due_at);

CREATE TABLE bank.payments (
  id               uuid PRIMARY KEY,
  payer_account_id uuid NOT NULL REFERENCES bank.accounts (id),
  payee_account_id uuid NOT NULL REFERENCES bank.accounts (id),
  amount_paise     bigint NOT NULL CHECK (amount_paise > 0),
  channel          text NOT NULL
                   CHECK (channel IN ('UPI_PUSH', 'UPI_COLLECT_APPROVAL', 'IMPS', 'NEFT',
                                      'CASH_OUT')),
  initiated_at     timestamptz NOT NULL,
  device_id        uuid REFERENCES bank.devices (id),
  status           text NOT NULL
                   CHECK (status IN ('PENDING_GUARDIAN', 'RELEASED', 'EXECUTED', 'DECLINED',
                                     'CANCELLED', 'HELD')),
  origin           text NOT NULL CHECK (origin IN ('SIM_HISTORY', 'SIM_LIVE', 'DEMO')),
  executed_at      timestamptz,
  scheduled_by     uuid REFERENCES bank.sim_schedule (id),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payments_payer_account_id_initiated_at_idx
  ON bank.payments (payer_account_id, initiated_at);
CREATE INDEX payments_payee_account_id_initiated_at_idx
  ON bank.payments (payee_account_id, initiated_at);
CREATE INDEX payments_status_idx ON bank.payments (status);
CREATE INDEX payments_device_id_idx ON bank.payments (device_id);
CREATE INDEX payments_scheduled_by_idx ON bank.payments (scheduled_by);

-- Double entry, append-only: `at` is the creation time (docs/17 §1).
CREATE TABLE bank.ledger_entries (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id          uuid NOT NULL REFERENCES bank.accounts (id),
  payment_id          uuid NOT NULL REFERENCES bank.payments (id),
  delta_paise         bigint NOT NULL CHECK (delta_paise <> 0),
  balance_after_paise bigint NOT NULL CHECK (balance_after_paise >= 0),
  at                  timestamptz NOT NULL
);
CREATE INDEX ledger_entries_account_id_at_idx ON bank.ledger_entries (account_id, at);
CREATE INDEX ledger_entries_payment_id_idx ON bank.ledger_entries (payment_id);

CREATE TABLE bank.session_telemetry (
  payment_id              uuid PRIMARY KEY REFERENCES bank.payments (id),
  typing_cadence_z        double precision NOT NULL,
  paste_on_account_field  boolean NOT NULL,
  max_field_dwell_s       double precision NOT NULL CHECK (max_field_dwell_s >= 0),
  remote_access_active    boolean NOT NULL,
  screen_share_active     boolean NOT NULL,
  on_call                 boolean NOT NULL,
  call_counterparty_known boolean,
  session_duration_s      double precision NOT NULL CHECK (session_duration_s >= 0),
  created_at              timestamptz NOT NULL DEFAULT now()
);

-- Event logs: `at` is the creation time.
CREATE TABLE bank.login_events (
  id          uuid PRIMARY KEY,
  customer_id uuid NOT NULL REFERENCES bank.customers (id),
  device_id   uuid NOT NULL REFERENCES bank.devices (id),
  ip_prefix24 cidr NOT NULL CHECK (masklen(ip_prefix24) = 24),
  lat         double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon         double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
  city        text NOT NULL,
  at          timestamptz NOT NULL
);
CREATE INDEX login_events_customer_id_at_idx ON bank.login_events (customer_id, at);
CREATE INDEX login_events_device_id_idx ON bank.login_events (device_id);

CREATE TABLE bank.crm_events (
  id          uuid PRIMARY KEY,
  customer_id uuid NOT NULL REFERENCES bank.customers (id),
  type        text NOT NULL,
  at          timestamptz NOT NULL
);
CREATE INDEX crm_events_customer_id_at_idx ON bank.crm_events (customer_id, at);

CREATE TABLE bank.complaints (
  id                       uuid PRIMARY KEY,
  reported_at              timestamptz NOT NULL,
  victim_customer_id       uuid NOT NULL REFERENCES bank.customers (id),
  channel                  text NOT NULL CHECK (channel IN ('NCRP_1930', 'BRANCH')),
  category                 text NOT NULL,
  payment_ids              uuid[] NOT NULL DEFAULT '{}',
  counterparty_account_ids uuid[] NOT NULL DEFAULT '{}',
  created_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX complaints_victim_customer_id_idx ON bank.complaints (victim_customer_id);

-- The simulated I4C suspect registry and DoT FRI (docs/14 §9).
CREATE TABLE bank.sim_registry_listings (
  account_id uuid PRIMARY KEY REFERENCES bank.accounts (id),
  listed_at  timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bank.sim_phone_risk (
  phone_number text PRIMARY KEY,
  category     text NOT NULL CHECK (category IN ('VERY_HIGH', 'HIGH', 'MEDIUM', 'LOW')),
  created_at   timestamptz NOT NULL DEFAULT now()
);
"""

# Ground truth. Foreign keys into `bank` use the default NO ACTION, so deleting a bank row that
# truth still describes fails instead of silently removing the truth.
SIM_TRUTH = """
CREATE TABLE sim_truth.scenario_instances (
  id             uuid PRIMARY KEY,
  scenario_type  text NOT NULL
                 CHECK (scenario_type IN ('GENUINE_ROUTINE', 'GENUINE_UNUSUAL', 'ATO',
                                          'APP_DIGITAL_ARREST', 'APP_INVESTMENT',
                                          'MULE_FORWARDING', 'UPI_COLLECT_REFUND')),
  hypothesis_key text NOT NULL,
  params         jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at     timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sim_truth.payment_truth (
  payment_id           uuid PRIMARY KEY REFERENCES bank.payments (id),
  scenario_instance_id uuid NOT NULL REFERENCES sim_truth.scenario_instances (id),
  is_fraud             boolean NOT NULL,
  hypothesis_key       text NOT NULL,
  coached              boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payment_truth_scenario_instance_id_idx
  ON sim_truth.payment_truth (scenario_instance_id);

CREATE TABLE sim_truth.account_truth (
  account_id uuid PRIMARY KEY REFERENCES bank.accounts (id),
  is_mule    boolean NOT NULL,
  ring_id    uuid,
  layer      smallint CHECK (layer >= 1),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- The frozen response models (docs/14 §5.2). Coached rows are stored separately, so `coached`
-- is part of the key. `probs` is {outcome: probability}.
CREATE TABLE sim_truth.response_overrides (
  scenario_type text NOT NULL
                CHECK (scenario_type IN ('GENUINE_ROUTINE', 'GENUINE_UNUSUAL', 'ATO',
                                         'APP_DIGITAL_ARREST', 'APP_INVESTMENT',
                                         'MULE_FORWARDING', 'UPI_COLLECT_REFUND')),
  source_key    text NOT NULL,
  coached       boolean NOT NULL DEFAULT false,
  probs         jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scenario_type, source_key, coached)
);

-- Future labels, delivered by LABEL_DELIVERY (docs/14 §7–8). At most one label per subject and
-- source, as in delphi.labels.
CREATE TABLE sim_truth.label_schedule (
  subject_type   text NOT NULL CHECK (subject_type IN ('PAYMENT', 'ACCOUNT')),
  subject_id     uuid NOT NULL,
  hypothesis_key text NOT NULL,
  label_source   text NOT NULL
                 CHECK (label_source IN ('CUSTOMER_COMPLAINT', 'NO_COMPLAINT_60D',
                                         'OUTREACH_CONFIRMED', 'INVESTIGATOR_DISPOSITION',
                                         'REGISTRY_LISTING')),
  due_at         timestamptz NOT NULL,
  delivered      boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subject_type, subject_id, label_source)
);
CREATE INDEX label_schedule_due_at_idx ON sim_truth.label_schedule (due_at) WHERE NOT delivered;
"""

# Reverse dependency order; the schemas themselves belong to 0001.
TRUTH_TABLES = (
    "label_schedule",
    "response_overrides",
    "account_truth",
    "payment_truth",
    "scenario_instances",
)
BANK_TABLES = (
    "sim_phone_risk",
    "sim_registry_listings",
    "complaints",
    "crm_events",
    "login_events",
    "session_telemetry",
    "ledger_entries",
    "payments",
    "sim_schedule",
    "payees",
    "devices",
    "accounts",
    "customers",
)


def upgrade() -> None:
    op.execute(BANK)
    op.execute(SIM_TRUTH)


def downgrade() -> None:
    for table in TRUTH_TABLES:
        op.execute(f"DROP TABLE sim_truth.{table}")
    for table in BANK_TABLES:
        op.execute(f"DROP TABLE bank.{table}")
