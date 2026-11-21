"""Investigator trace events (docs/09 §5, docs/17, #108).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-21

One append-only row per investigator interaction with a case: the server writes CASE_OPENED,
EVIDENCE_REQUESTED, DISPOSITION and DD_PACK_VIEWED with the action itself; the console sends the
view events (CASE_VIEWED, EVIDENCE_VIEWED, NARRATIVE_EXPANDED, WCMM_VIEWED) through
`POST /cases/{id}/trace-events`. The Learning page's trace analytics read them (docs/12 §6).
`user_id` is null for an event no person caused (a case opened by the engine).

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table and its indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.trace_events (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id  uuid NOT NULL REFERENCES delphi.cases (id),
  user_id  uuid REFERENCES delphi.users (id),
  type     text NOT NULL CONSTRAINT trace_events_type_check CHECK (type IN (
             'CASE_OPENED', 'CASE_VIEWED', 'EVIDENCE_REQUESTED', 'EVIDENCE_VIEWED',
             'NARRATIVE_EXPANDED', 'WCMM_VIEWED', 'DISPOSITION', 'DD_PACK_VIEWED')),
  payload  jsonb NOT NULL,
  at       timestamptz NOT NULL
);
CREATE INDEX trace_events_case_id_at_idx ON delphi.trace_events (case_id, at);
CREATE INDEX trace_events_user_id_idx ON delphi.trace_events (user_id);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.trace_events")
