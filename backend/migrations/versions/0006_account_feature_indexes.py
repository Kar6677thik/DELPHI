"""Indexes for the online account features.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10

`delphi.sensing.history.load_account_snapshot` (docs/13 §6, #87) finds the complaints that name
an account as a counterparty, and the other devices that share a fingerprint with the account
holder's. Neither had an index. Expand/contract (docs/17 §1): this migration only adds indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX complaints_counterparty_account_ids_idx "
        "ON bank.complaints USING gin (counterparty_account_ids)"
    )
    op.execute("CREATE INDEX devices_fingerprint_idx ON bank.devices (fingerprint)")


def downgrade() -> None:
    op.execute("DROP INDEX bank.devices_fingerprint_idx")
    op.execute("DROP INDEX bank.complaints_counterparty_account_ids_idx")
