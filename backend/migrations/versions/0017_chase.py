"""Chase operations, freeze candidates, liens and CFCFRMS tickets (docs/10 §5, docs/05 §6,
docs/17, #113).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-23

A chase operation traces a reported fraud's money (docs/10 §3) and stores each account still
holding taint as a candidate, with its score (docs/10 §4, #112). A lien exists only once an
investigator approved it (FR-CHS-04): its amount is at most the traced taint (FR-CHS-05), with
the reasons and the evidence it rests on. An external bank's lien goes through a simulated
CFCFRMS ticket.

0001's default privileges give both application roles DML on the tables. Expand/contract
(docs/17 §1): this migration only adds tables and their indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE delphi.chase_operations (
  id                  uuid PRIMARY KEY,
  complaint_id        uuid REFERENCES bank.complaints (id),
  source_payment_ids  uuid[] NOT NULL CHECK (cardinality(source_payment_ids) > 0),
  status              text NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
  reported_at         timestamptz NOT NULL,
  reported_loss_paise bigint NOT NULL CHECK (reported_loss_paise >= 0),
  kb_version_id       integer NOT NULL REFERENCES delphi.kb_versions (id),
  golden_hour_ends_at timestamptz NOT NULL,
  last_traced_at      timestamptz NOT NULL,
  metrics             jsonb NOT NULL,
  created_by          uuid REFERENCES delphi.users (id),
  version             integer NOT NULL CHECK (version >= 1)
);
CREATE INDEX chase_operations_status_idx ON delphi.chase_operations (status, reported_at);
CREATE INDEX chase_operations_complaint_idx ON delphi.chase_operations (complaint_id);
CREATE INDEX chase_operations_kb_version_idx ON delphi.chase_operations (kb_version_id);
CREATE INDEX chase_operations_created_by_idx ON delphi.chase_operations (created_by);

CREATE TABLE delphi.chase_candidates (
  id                  uuid PRIMARY KEY,
  operation_id        uuid NOT NULL REFERENCES delphi.chase_operations (id),
  account_id          uuid NOT NULL REFERENCES bank.accounts (id),
  hop                 integer NOT NULL CHECK (hop >= 1),
  taint_paise         bigint NOT NULL CHECK (taint_paise >= 0),
  balance_paise       bigint NOT NULL CHECK (balance_paise >= 0),
  -- The score (docs/10 §4); null until the candidate is scored.
  p_present           double precision CHECK (p_present BETWEEN 0 AND 1),
  p_mule              double precision CHECK (p_mule BETWEEN 0 AND 1),
  erv_paise           bigint,
  wrongful_cost_paise bigint,
  net_paise           bigint,
  rank                integer CHECK (rank >= 1),
  recommended         boolean,
  traced_at           timestamptz NOT NULL,
  UNIQUE (operation_id, account_id)
);
CREATE INDEX chase_candidates_account_idx ON delphi.chase_candidates (account_id);

CREATE TABLE delphi.liens (
  id               uuid PRIMARY KEY,
  operation_id     uuid NOT NULL REFERENCES delphi.chase_operations (id),
  candidate_id     uuid NOT NULL REFERENCES delphi.chase_candidates (id),
  account_id       uuid NOT NULL REFERENCES bank.accounts (id),
  bank_code        text NOT NULL,
  amount_paise     bigint NOT NULL CHECK (amount_paise >= 0),
  status           text NOT NULL CHECK (status IN ('APPROVED', 'PLACED', 'TICKET_SENT',
                     'FAILED_FUNDS_MOVED', 'FAILED_NO_RESPONSE', 'RELEASED')),
  recorded_reasons text NOT NULL CHECK (recorded_reasons <> ''),
  evidence_refs    jsonb NOT NULL CHECK (jsonb_array_length(evidence_refs) > 0),
  -- FR-CHS-04: no lien without the investigator who approved it.
  approved_by      uuid NOT NULL REFERENCES delphi.users (id),
  approved_at      timestamptz NOT NULL,
  placed_at        timestamptz,
  review_due_at    timestamptz,
  released_at      timestamptz,
  release_reason   text,
  CONSTRAINT liens_placed_check CHECK (
    (status IN ('PLACED', 'RELEASED')) = (placed_at IS NOT NULL AND review_due_at IS NOT NULL)
  ),
  CONSTRAINT liens_released_check CHECK (
    (status = 'RELEASED') = (released_at IS NOT NULL AND release_reason IS NOT NULL)
  )
);
CREATE INDEX liens_operation_idx ON delphi.liens (operation_id);
CREATE UNIQUE INDEX liens_one_per_candidate_idx ON delphi.liens (candidate_id);
CREATE INDEX liens_account_idx ON delphi.liens (account_id);
CREATE INDEX liens_approved_by_idx ON delphi.liens (approved_by);

CREATE TABLE delphi.cfcfrms_tickets (
  id              uuid PRIMARY KEY,
  lien_id         uuid NOT NULL UNIQUE REFERENCES delphi.liens (id),
  target_bank     text NOT NULL,
  status          text NOT NULL
                  CHECK (status IN ('DRAFTED', 'SENT_SIMULATED', 'ACKNOWLEDGED', 'EXPIRED')),
  payload         jsonb NOT NULL,
  sent_at         timestamptz,
  acknowledged_at timestamptz,
  CONSTRAINT cfcfrms_tickets_sent_check CHECK ((status = 'DRAFTED') = (sent_at IS NULL)),
  CONSTRAINT cfcfrms_tickets_ack_check CHECK (
    (status = 'ACKNOWLEDGED') = (acknowledged_at IS NOT NULL)
  )
);
CREATE INDEX cfcfrms_tickets_status_idx ON delphi.cfcfrms_tickets (status, sent_at);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute(
        "DROP TABLE delphi.cfcfrms_tickets, delphi.liens, delphi.chase_candidates,"
        " delphi.chase_operations"
    )
