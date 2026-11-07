"""CHECKs for the bank value sets the generator now fixes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

0002 left some `bank` value sets as plain `text` until the generator (#17) fixed them
(docs/17 §3). These are now fixed by `sim/config/generator.yaml`:
- customer `segment`
- account `merchant_category`
- device `os`
- CRM event `type` (docs/05 §1)
- complaint `category`

A new value is added by a later migration that widens its CHECK.

Account `status` stays plain `text`: the generator opens every account `ACTIVE`, but the
states that freezing and liens need are Chase's to define (M6, docs/10).

Expand/contract (docs/17 §1): constraints on columns the previous release already writes
with these values only.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (table, column, allowed values); NULL passes a CHECK, so nullable columns stay nullable.
CHECKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("customers", "segment", ("RETAIL",)),
    (
        "accounts",
        "merchant_category",
        (
            "HOSPITAL",
            "GROCERY",
            "UTILITY",
            "RETAIL",
            "FUEL",
            "TELECOM",
            "EDUCATION",
            "WEDDING_VENDOR",
            "PROPERTY",
        ),
    ),
    ("devices", "os", ("ANDROID", "IOS")),
    ("crm_events", "type", ("SIM_SWAP_REPORTED", "DEVICE_LOST_REPORTED")),
    (
        "complaints",
        "category",
        ("UNAUTHORISED_TRANSACTION", "DIGITAL_ARREST_SCAM", "INVESTMENT_SCAM"),
    ),
)


def _name(table: str, column: str) -> str:
    return f"{table}_{column}_check"


def upgrade() -> None:
    for table, column, values in CHECKS:
        allowed = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"ALTER TABLE bank.{table} ADD CONSTRAINT {_name(table, column)} "
            f"CHECK ({column} IN ({allowed}))"
        )


def downgrade() -> None:
    for table, column, _values in CHECKS:
        op.execute(f"ALTER TABLE bank.{table} DROP CONSTRAINT {_name(table, column)}")
