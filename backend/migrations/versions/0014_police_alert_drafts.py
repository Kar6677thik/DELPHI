"""Police alert drafts (docs/09 §6, docs/17, #106).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-19

A coercion case gets one police alert draft when it opens: a template with the victim's
pseudonym, the amount, the beneficiary, the time and (when sent) the evidence list. It stays
DRAFT until an investigator sends it (SENT_SIMULATED in the simulation) or discards it; nothing
sends it automatically (FR-NAV-10). A sent or discarded alert names who acted and when.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table and its indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.police_alert_drafts (
  id          uuid PRIMARY KEY,
  case_id     uuid NOT NULL UNIQUE REFERENCES delphi.cases (id),
  status      text NOT NULL CONSTRAINT police_alert_drafts_status_check
              CHECK (status IN ('DRAFT', 'SENT_SIMULATED', 'DISCARDED')),
  payload     jsonb NOT NULL,
  acted_by    uuid REFERENCES delphi.users (id),
  acted_at    timestamptz,
  created_at  timestamptz NOT NULL,
  -- Only a human acts on an alert: a sent or discarded one names who and when (FR-NAV-10).
  CONSTRAINT police_alert_drafts_acted_check CHECK (
    (status = 'DRAFT') = (acted_by IS NULL AND acted_at IS NULL)
  )
);
CREATE INDEX police_alert_drafts_acted_by_idx ON delphi.police_alert_drafts (acted_by);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.police_alert_drafts")
