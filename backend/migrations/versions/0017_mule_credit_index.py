"""An index for the ACCOUNT_REVIEW mule-scorer trigger.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-23

`delphi.navigator.reviews.mule_candidates` (docs/09 §1, #109) reads the executed credits a mule
suspect received in the last `mule_credit_window_h`, every minute. The payee index is on
`initiated_at`, so it read every credit the suspect ever received (#263). This partial index
serves `payee_account_id = … AND executed_at > …` on executed payments. Expand/contract
(docs/17 §1): it only adds an index, built `CONCURRENTLY` because `bank.payments` is large and
written by the simulator, so the build does not block its writes.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS payments_payee_executed_at_idx "
            "ON bank.payments (payee_account_id, executed_at) WHERE status = 'EXECUTED'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS bank.payments_payee_executed_at_idx")
