"""Cases (docs/05 §4, docs/09 §1, docs/17, #97: the M4 case stub).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14

`delphi.cases` holds the Navigator's cases. In M4 a Guardian hold opens one (`open_case`):
PAYMENT_HOLD with the session's final posterior, protocol COERCION for PROTECTIVE_HOLD. The
evidence, dispositions and the console come with #103 (M5). The docs/05 §4 invariants a row can
show are constraints:

- a PAYMENT_HOLD case names its payment, and an ACCOUNT_REVIEW case its account;
- a DISPOSED case has its disposition and disposition time, and an open one has neither; the
  disposition is RELEASE / CONFIRM_FRAUD for a payment hold, CLEAR / CONFIRM_MULE for a review;
- at most one open case per payment, and one open ACCOUNT_REVIEW per account (partial unique
  indexes).

`recommended_decision` and `risk_by_decision` stay null until the case console evaluates the case
under its decision profile (#103). `campaign_id` has no foreign key yet: `campaigns` comes with
M5. This migration also adds the `guardian_sessions.case_id → cases.id` foreign key that 0010
left out, because the two tables reference each other.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table, indexes and a foreign key on a column that is
still always null.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.cases (
  id                     uuid PRIMARY KEY,
  type                   text NOT NULL CONSTRAINT cases_type_check
                         CHECK (type IN ('PAYMENT_HOLD', 'ACCOUNT_REVIEW')),
  protocol               text NOT NULL CONSTRAINT cases_protocol_check
                         CHECK (protocol IN ('STANDARD', 'COERCION')),
  status                 text NOT NULL CONSTRAINT cases_status_check
                         CHECK (status IN ('OPEN', 'IN_PROGRESS', 'DISPOSED')),
  subject_payment_id     uuid REFERENCES bank.payments (id),
  subject_account_id     uuid REFERENCES bank.accounts (id),
  guardian_session_id    uuid REFERENCES delphi.guardian_sessions (id),
  kb_version_id          integer NOT NULL REFERENCES delphi.kb_versions (id),
  prior_components       jsonb NOT NULL,
  posterior              jsonb NOT NULL,
  recommended_decision   text,
  risk_by_decision       jsonb,
  leading_hypothesis     text NOT NULL,
  campaign_id            uuid,
  amount_at_stake_paise  bigint NOT NULL CONSTRAINT cases_amount_check
                         CHECK (amount_at_stake_paise >= 0),
  opened_at              timestamptz NOT NULL,
  sla_due_at             timestamptz NOT NULL,
  disposition            text CONSTRAINT cases_disposition_check
                         CHECK (disposition IN ('RELEASE', 'CONFIRM_FRAUD', 'CLEAR', 'CONFIRM_MULE')),
  disposition_typology   text,
  disposition_confidence text,
  is_override            boolean NOT NULL DEFAULT false,
  override_rationale     text,
  disposed_by            uuid REFERENCES delphi.users (id),
  disposed_at            timestamptz,
  dd_pack_complete       boolean NOT NULL DEFAULT false,
  unexplained            boolean NOT NULL DEFAULT false,
  version                integer NOT NULL DEFAULT 1,
  created_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT cases_subject_check CHECK (
    (type = 'PAYMENT_HOLD' AND subject_payment_id IS NOT NULL)
    OR (type = 'ACCOUNT_REVIEW' AND subject_account_id IS NOT NULL)
  ),
  CONSTRAINT cases_disposed_check CHECK (
    (status = 'DISPOSED') = (disposition IS NOT NULL AND disposed_at IS NOT NULL)
  ),
  CONSTRAINT cases_disposition_type_check CHECK (
    disposition IS NULL
    OR (type = 'PAYMENT_HOLD' AND disposition IN ('RELEASE', 'CONFIRM_FRAUD'))
    OR (type = 'ACCOUNT_REVIEW' AND disposition IN ('CLEAR', 'CONFIRM_MULE'))
  )
);
-- docs/05 §4: one open case per payment, one open review per account.
CREATE UNIQUE INDEX cases_open_payment_key ON delphi.cases (subject_payment_id)
  WHERE status <> 'DISPOSED';
CREATE UNIQUE INDEX cases_open_account_review_key ON delphi.cases (subject_account_id)
  WHERE status <> 'DISPOSED' AND type = 'ACCOUNT_REVIEW';
-- The case queue (docs/17 hot queries): by status, soonest SLA first.
CREATE INDEX cases_status_sla_due_at_idx ON delphi.cases (status, sla_due_at, id);
CREATE INDEX cases_opened_at_idx ON delphi.cases (opened_at, id);
-- Foreign keys are indexed (docs/17 §1).
CREATE INDEX cases_subject_payment_id_idx ON delphi.cases (subject_payment_id);
CREATE INDEX cases_subject_account_id_idx ON delphi.cases (subject_account_id);
CREATE INDEX cases_guardian_session_id_idx ON delphi.cases (guardian_session_id);
CREATE INDEX cases_kb_version_id_idx ON delphi.cases (kb_version_id);
CREATE INDEX cases_disposed_by_idx ON delphi.cases (disposed_by);

ALTER TABLE delphi.guardian_sessions
  ADD CONSTRAINT guardian_sessions_case_id_fkey FOREIGN KEY (case_id) REFERENCES delphi.cases (id);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE delphi.guardian_sessions DROP CONSTRAINT guardian_sessions_case_id_fkey"
    )
    op.execute("DROP TABLE delphi.cases")
