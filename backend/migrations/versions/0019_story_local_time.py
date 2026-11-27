"""A demo story payment's scripted local time (docs/14 §10, #291).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-27

A story payment is made when the presenter runs the story, but its script sets the local time
(S-1 at 23:40 IST). The prior's hour features read it from here instead of the real time; every
other part of the payment stays real. Only a `DEMO_STORY` payment sets it.

Expand/contract (docs/17 §1): this migration only adds a nullable column.
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE bank.payments ADD COLUMN story_local_time text"
        " CHECK (story_local_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE bank.payments DROP COLUMN story_local_time")
