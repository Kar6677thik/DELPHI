"""SQLAlchemy Core definitions of the chase tables (migration 0016, docs/17)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData(schema="delphi")


def _ts(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


chase_operations = Table(
    "chase_operations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("complaint_id", UUID(as_uuid=True)),
    Column("source_payment_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    Column("status", Text, nullable=False),
    _ts("reported_at"),
    Column("reported_loss_paise", BigInteger, nullable=False),
    Column("kb_version_id", Integer, nullable=False),
    _ts("golden_hour_ends_at"),
    _ts("last_traced_at"),
    Column("metrics", JSONB, nullable=False),
    Column("created_by", UUID(as_uuid=True)),
    Column("version", Integer, nullable=False),
)

chase_candidates = Table(
    "chase_candidates",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operation_id", UUID(as_uuid=True), nullable=False),
    Column("account_id", UUID(as_uuid=True), nullable=False),
    Column("hop", Integer, nullable=False),
    Column("taint_paise", BigInteger, nullable=False),
    Column("balance_paise", BigInteger, nullable=False),
    Column("p_present", Float),
    Column("p_mule", Float),
    Column("erv_paise", BigInteger),
    Column("wrongful_cost_paise", BigInteger),
    Column("net_paise", BigInteger),
    Column("rank", Integer),
    Column("recommended", Boolean),
    _ts("traced_at"),
)

liens = Table(
    "liens",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operation_id", UUID(as_uuid=True), nullable=False),
    Column("candidate_id", UUID(as_uuid=True), nullable=False),
    Column("account_id", UUID(as_uuid=True), nullable=False),
    Column("bank_code", Text, nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("recorded_reasons", Text, nullable=False),
    Column("evidence_refs", JSONB, nullable=False),
    Column("approved_by", UUID(as_uuid=True), nullable=False),
    _ts("approved_at"),
    _ts("placed_at", nullable=True),
    _ts("review_due_at", nullable=True),
    _ts("released_at", nullable=True),
    Column("release_reason", Text),
)

cfcfrms_tickets = Table(
    "cfcfrms_tickets",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("lien_id", UUID(as_uuid=True), nullable=False),
    Column("target_bank", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    _ts("sent_at", nullable=True),
    _ts("acknowledged_at", nullable=True),
)
