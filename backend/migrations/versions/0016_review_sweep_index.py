"""An index for the ACCOUNT_REVIEW complaint sweep.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23

`delphi.navigator.reviews.complaint_candidates` (docs/09 §1, #109) reads the complaints reported
in the sweep's lookback window, oldest first, every minute. Without an index on `reported_at` it
read every complaint ever reported (#263). Expand/contract (docs/17 §1): this migration only adds
an index. `bank.complaints` is small, so a plain `CREATE INDEX` is fine here.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX complaints_reported_at_idx ON bank.complaints (reported_at, id)")


def downgrade() -> None:
    op.execute("DROP INDEX bank.complaints_reported_at_idx")
