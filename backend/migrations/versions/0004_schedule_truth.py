"""Ground truth for the scheduled transfers.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07

`bank.sim_schedule` is readable by `delphi_app`, and its TRANSFER payloads used to say why the
transfer exists (`reason: MULE_FORWARDING`, the mule-ring `flow`, whether it is a mule's
session). That marked the payer as a mule (#56). The payloads now carry only what executing the
payment needs (payer, payee, amount, channel); the reason moves here, into `sim_truth`, which
`delphi_app` cannot read (docs/17 §2). 0001's default privileges give `delphi_sim` its grants.

Expand/contract (docs/17 §1): this migration only adds a table.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEDULE_TRUTH = """
CREATE TABLE sim_truth.schedule_truth (
  schedule_id uuid PRIMARY KEY REFERENCES bank.sim_schedule (id),
  reason      text NOT NULL CHECK (reason IN ('SALARY', 'MULE_FORWARDING')),
  flow        integer,
  session     boolean NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(SCHEDULE_TRUTH)


def downgrade() -> None:
    op.execute("DROP TABLE sim_truth.schedule_truth")
