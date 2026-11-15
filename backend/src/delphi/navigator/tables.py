"""SQLAlchemy Core description of ``delphi.cases`` (migration 0012, docs/17). The migration is the
source of truth for constraints; a test compares these columns with the migrated schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="delphi")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


cases = Table(
    "cases",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("type", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("subject_payment_id", UUID(as_uuid=True)),
    Column("subject_account_id", UUID(as_uuid=True)),
    Column("guardian_session_id", UUID(as_uuid=True)),
    Column("kb_version_id", Integer, nullable=False),
    Column("prior_components", JSONB, nullable=False),
    Column("posterior", JSONB, nullable=False),
    Column("recommended_decision", Text),
    Column("risk_by_decision", JSONB),
    Column("leading_hypothesis", Text, nullable=False),
    Column("campaign_id", UUID(as_uuid=True)),
    Column("amount_at_stake_paise", BigInteger, nullable=False),
    _ts("opened_at"),
    _ts("sla_due_at"),
    Column("disposition", Text),
    Column("disposition_typology", Text),
    Column("disposition_confidence", SmallInteger),
    Column("is_override", Boolean, nullable=False),
    Column("override_rationale", Text),
    Column("disposed_by", UUID(as_uuid=True)),
    _ts("disposed_at", nullable=True),
    Column("dd_pack_complete", Boolean, nullable=False),
    Column("unexplained", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    _ts("created_at"),
)
