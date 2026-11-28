"""S-4's stand-in becomes the prize advance-fee scenario (docs/14 §4, §5.4; #36, v18).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-28

The owner replaced the hidden UPI_COLLECT_REFUND scenario with PRIZE_ADVANCE_FEE (ground truth
APP_PRIZE_ADVANCE_FEE). The generator writes only the new names from v18. Ground truth already
stored under the old names is renamed, so the worker, which parses `scenario_type`, reads a
dataset seeded before v18 until the demo is reseeded. Both CHECKs accept both names (expand): a
worker still on v17 during the rollout can write the old name. Dropping the old name from the
CHECKs is the contract step, once every environment has been reseeded with v18.
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_SCENARIOS = (
    "'GENUINE_ROUTINE', 'GENUINE_UNUSUAL', 'ATO', 'APP_DIGITAL_ARREST', 'APP_INVESTMENT',"
    " 'MULE_FORWARDING'"
)
_TABLES = ("scenario_instances", "response_overrides")


def _check(table: str, names: str) -> None:
    op.execute(
        f"ALTER TABLE sim_truth.{table} DROP CONSTRAINT {table}_scenario_type_check,"
        f" ADD CONSTRAINT {table}_scenario_type_check CHECK (scenario_type IN ({names}))"
    )


def upgrade() -> None:
    for table in _TABLES:
        _check(table, f"{_SCENARIOS}, 'UPI_COLLECT_REFUND', 'PRIZE_ADVANCE_FEE'")
        op.execute(
            f"UPDATE sim_truth.{table} SET scenario_type = 'PRIZE_ADVANCE_FEE'"
            " WHERE scenario_type = 'UPI_COLLECT_REFUND'"
        )
    op.execute(
        "UPDATE sim_truth.label_schedule SET hypothesis_key = 'APP_PRIZE_ADVANCE_FEE'"
        " WHERE hypothesis_key = 'APP_COLLECT_REFUND'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sim_truth.label_schedule SET hypothesis_key = 'APP_COLLECT_REFUND'"
        " WHERE hypothesis_key = 'APP_PRIZE_ADVANCE_FEE'"
    )
    for table in _TABLES:
        op.execute(
            f"UPDATE sim_truth.{table} SET scenario_type = 'UPI_COLLECT_REFUND'"
            " WHERE scenario_type = 'PRIZE_ADVANCE_FEE'"
        )
        _check(table, f"{_SCENARIOS}, 'UPI_COLLECT_REFUND'")
