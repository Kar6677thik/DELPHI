"""The labels the bank has observed (docs/17, docs/05 §8, docs/14 §7–8, #159).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14

`delphi.labels` holds what the bank knows about a payment or an account, and since when: a
customer complaint, the no-complaint maturity, a registry listing, and later DELPHI's outreach
and an investigator's disposition (#106, M5). The simulator's future labels stay in
`sim_truth.label_schedule`; `LABEL_DELIVERY` copies each one here when it falls due, so the app
role learns a label only once the bank would have. At most one label per subject and source, as
in the schedule; `known_at` is when the bank learnt it, which the sensing trainers compare with
their training cutoff (docs/13 §4).

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE delphi.labels (
  id             uuid PRIMARY KEY,
  subject_type   text NOT NULL CONSTRAINT labels_subject_type_check
                 CHECK (subject_type IN ('PAYMENT', 'ACCOUNT')),
  subject_id     uuid NOT NULL,
  hypothesis_key text NOT NULL,
  label_source   text NOT NULL CONSTRAINT labels_label_source_check
                 CHECK (label_source IN ('CUSTOMER_COMPLAINT', 'NO_COMPLAINT_60D',
                                         'OUTREACH_CONFIRMED', 'INVESTIGATOR_DISPOSITION',
                                         'REGISTRY_LISTING')),
  known_at       timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT labels_subject_source_key UNIQUE (subject_type, subject_id, label_source)
);
-- The trainers read one subject type's labels known by a cutoff.
CREATE INDEX labels_subject_type_known_at_idx ON delphi.labels (subject_type, known_at);
"""


def upgrade() -> None:
    op.execute(TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE delphi.labels")
