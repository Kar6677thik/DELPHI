"""A demo story's scripted prior (docs/06 §17, docs/14 §10, #291).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-28

The trained prior gives the demo personas' story payments a few tenths of a percent of their
story's hypothesis, and that share swings ~15x between trainings, so a live story's path was a
coin flip. A story payment (S-1, S-2) now starts from its scripted prior, the docs/06 §17
fixture that ENG-S1/S2 pin; every other payment keeps the trained model's.

- ``bank.payments.story`` names the payment's demo story; only a `DEMO_STORY` payment sets it.
- ``delphi.guardian_sessions.prior_source`` records where the session's prior came from:
  ``MODEL`` (the sensing layer) or ``STORY_SCRIPT`` (the story's scripted prior), so every view
  can say so.

Expand/contract (docs/17 §1): a nullable column and a column with a default.
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE bank.payments ADD COLUMN story text CHECK (story ~ '^S[1-9]$')"
    )
    op.execute(
        "ALTER TABLE delphi.guardian_sessions ADD COLUMN prior_source text NOT NULL"
        " DEFAULT 'MODEL' CHECK (prior_source IN ('MODEL', 'STORY_SCRIPT'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE delphi.guardian_sessions DROP COLUMN prior_source")
    op.execute("ALTER TABLE bank.payments DROP COLUMN story")
