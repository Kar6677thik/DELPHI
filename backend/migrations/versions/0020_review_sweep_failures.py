"""The ACCOUNT_REVIEW sweep's failed candidates and their backoff.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-26

`ACCOUNT_REVIEW_SWEEP` (docs/09 §1) takes its candidates oldest first, so an account whose review
fails to open every time stayed at the head of the queue and rolled back every batch (#290). The
sweep now opens each candidate in a savepoint; a failure is recorded here with its attempts and
the time of its next attempt, and the candidate is skipped until then. A successful open deletes
the row. Keyed by the account; no foreign key to `bank`, which the simulator owns, as for
`cases.subject_account_id`.

0001's default privileges give both application roles DML on the table. Expand/contract
(docs/17 §1): this migration only adds a table.
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE delphi.review_sweep_failures (
  account_id       uuid PRIMARY KEY,
  attempts         integer NOT NULL CHECK (attempts >= 1),
  last_error       text NOT NULL,
  next_attempt_at  timestamptz NOT NULL,
  created_at       timestamptz NOT NULL,
  updated_at       timestamptz NOT NULL
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE delphi.review_sweep_failures")
